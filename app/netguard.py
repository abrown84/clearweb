"""SSRF guard: address classification, pinned DNS, manual redirect handling."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Callable, Iterator, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

_EXTRA_BLOCKED = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
        "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16",
        "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
        "255.255.255.255/32",
        "::/128", "::1/128", "::/96", "::ffff:0:0/96", "64:ff9b::/96", "64:ff9b:1::/48",
        "100::/64", "2001::/32", "2001:db8::/32", "2002::/16", "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8",
    )
]

REDIRECT_CODES = {301, 302, 303, 307, 308}


class BlockedURL(Exception):
    pass


def embedded_ipv4(addr: ipaddress.IPv6Address) -> List[ipaddress.IPv4Address]:
    """IPv4 addresses embedded in an IPv6 address (mapped, compat, 6to4, Teredo, NAT64)."""
    out: List[ipaddress.IPv4Address] = []
    if addr.ipv4_mapped:
        out.append(addr.ipv4_mapped)
    if addr.sixtofour:
        out.append(addr.sixtofour)
    if addr.teredo:
        out.extend(addr.teredo)
    packed = addr.packed
    if packed[:12] == b"\x00" * 12 and packed[12:] != b"\x00\x00\x00\x00":  # IPv4-compatible ::a.b.c.d
        out.append(ipaddress.IPv4Address(packed[12:]))
    if addr in ipaddress.ip_network("64:ff9b::/96") or addr in ipaddress.ip_network("64:ff9b:1::/48"):
        out.append(ipaddress.IPv4Address(packed[12:]))
    return out


def is_blocked_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address):
        for v4 in embedded_ipv4(addr):
            if is_blocked_ip(str(v4)):
                return True
    if any(addr in net for net in _EXTRA_BLOCKED if net.version == addr.version):
        return True
    return not addr.is_global or addr.is_multicast


Resolver = Callable[[str, int], List[str]]


def system_resolver(host: str, port: int) -> List[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(i[4][0] for i in infos))


def resolve_public(url: str, resolver: Resolver = system_resolver) -> Tuple[str, str, int]:
    """Validate URL; return (host, pinned_ip, port). Raises BlockedURL."""
    try:
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise BlockedURL("invalid url")
    if parts.scheme not in {"http", "https"}:
        raise BlockedURL("only http/https allowed")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise BlockedURL("missing host")
    if parts.username or parts.password:
        raise BlockedURL("credentials in url not allowed")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".internal") or host.endswith(".local"):
        raise BlockedURL("local hostname blocked")
    try:
        ips = [host] if _is_ip_literal(host) else resolver(host, port)
    except (socket.gaierror, UnicodeError):
        raise BlockedURL("dns resolution failed")
    if not ips:
        raise BlockedURL("dns resolution empty")
    for ip in ips:  # any bad answer => reject (defeats mixed-record tricks)
        if is_blocked_ip(ip):
            raise BlockedURL(f"blocked address {ip}")
    return host, ips[0], port


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def pinned_request(client: httpx.Client, url: str, host: str, ip: str, headers: dict) -> httpx.Request:
    """Build a request that connects to `ip` but keeps Host header + TLS SNI/cert check on `host`."""
    parts = urlsplit(url)
    ip_host = f"[{ip}]" if ":" in ip else ip
    netloc = ip_host + (f":{parts.port}" if parts.port else "")
    target = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
    hdrs = dict(headers)
    hdrs["Host"] = parts.netloc.rsplit("@", 1)[-1]
    ext = {"sni_hostname": host} if parts.scheme == "https" else {}
    return client.build_request("GET", target, headers=hdrs, extensions=ext)


@dataclass
class FetchResult:
    final_url: str
    status: int
    content_type: str
    body: bytes


def safe_fetch(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    headers: dict,
    max_redirects: int = 5,
    resolver: Resolver = system_resolver,
    transport: Optional[httpx.BaseTransport] = None,
) -> FetchResult:
    """GET with every hop validated and the TCP connection pinned to the validated IP."""
    current = url
    with httpx.Client(timeout=timeout, follow_redirects=False, transport=transport, trust_env=False) as client:
        for _ in range(max_redirects + 1):
            host, ip, _port = resolve_public(current, resolver)
            req = pinned_request(client, current, host, ip, headers)
            resp = client.send(req, stream=True)
            try:
                if resp.status_code in REDIRECT_CODES and resp.headers.get("location"):
                    current = urljoin(current, resp.headers["location"])
                    continue
                chunks: List[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > max_bytes:
                        break
                return FetchResult(current, resp.status_code, (resp.headers.get("content-type") or "").lower(), b"".join(chunks))
            finally:
                resp.close()
    raise BlockedURL("too many redirects")
