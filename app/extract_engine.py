"""URL extraction with SSRF protections."""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import socket
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from .config import Settings

logger = logging.getLogger("clearweb.extract")


_PRIVATE_NETS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_blocked_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast:
        return True
    for net in _PRIVATE_NETS:
        if addr in net:
            return True
    return False


def validate_public_url(url: str) -> Tuple[bool, str]:
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "invalid url"
    if parsed.scheme not in {"http", "https"}:
        return False, "only http/https allowed"
    host = parsed.hostname
    if not host:
        return False, "missing host"
    if host.lower() in {"localhost"}:
        return False, "localhost blocked"
    # Resolve all addresses
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        return False, "dns resolution failed"
    if not infos:
        return False, "dns resolution empty"
    for info in infos:
        ip = info[4][0]
        if _is_blocked_ip(ip):
            return False, f"blocked address {ip}"
    return True, ""


def _html_to_text(html: str, url: str) -> Tuple[str, str]:
    title = ""
    text = ""
    try:
        import trafilatura

        extracted = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
        )
        if extracted:
            text = extracted.strip()
        meta = trafilatura.extract_metadata(html)
        if meta and getattr(meta, "title", None):
            title = meta.title or ""
    except Exception as exc:
        logger.debug("trafilatura failed: %s", exc)

    if not text:
        soup = BeautifulSoup(html, "lxml")
        if soup.title and soup.title.string:
            title = title or soup.title.string.strip()
        for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "aside"]):
            tag.decompose()
        text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))

    if not title:
        soup = BeautifulSoup(html, "lxml")
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
    return title, text


def extract_one(url: str, settings: Settings) -> Dict[str, Any]:
    ok, reason = validate_public_url(url)
    if not ok:
        return {"url": url, "title": "", "raw_content": "", "error": reason}

    headers = {
        "User-Agent": settings.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        with httpx.Client(
            timeout=settings.request_timeout_sec,
            follow_redirects=True,
            headers=headers,
            max_redirects=5,
        ) as client:
            # Re-validate after redirects by checking final URL host
            with client.stream("GET", url) as resp:
                final = str(resp.url)
                ok2, reason2 = validate_public_url(final)
                if not ok2:
                    return {"url": url, "title": "", "raw_content": "", "error": f"redirect blocked: {reason2}"}
                if resp.status_code >= 400:
                    return {
                        "url": url,
                        "title": "",
                        "raw_content": "",
                        "error": f"http {resp.status_code}",
                    }
                ctype = (resp.headers.get("content-type") or "").lower()
                if "html" not in ctype and "text/" not in ctype and "xml" not in ctype and ctype:
                    # still try if empty content-type
                    if ctype and not any(x in ctype for x in ("html", "text", "xml", "json")):
                        return {
                            "url": final,
                            "title": "",
                            "raw_content": "",
                            "error": f"unsupported content-type {ctype}",
                        }
                chunks = []
                total = 0
                for chunk in resp.iter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > settings.extract_max_bytes:
                        break
                raw = b"".join(chunks)
        html = raw.decode("utf-8", errors="replace")
        title, text = _html_to_text(html, final)
        if len(text) > settings.extract_max_chars:
            text = text[: settings.extract_max_chars] + "\n\n[truncated by Clearweb]"
        if not text.strip():
            return {"url": final, "title": title, "raw_content": "", "error": "no extractable content"}
        return {"url": final, "title": title, "raw_content": text}
    except Exception as exc:
        logger.warning("extract failed %s: %s", url, exc)
        return {"url": url, "title": "", "raw_content": "", "error": str(exc)[:240]}


def extract_many(urls: List[str], settings: Settings) -> Dict[str, List[Dict[str, Any]]]:
    results = []
    failed = []
    for url in urls:
        item = extract_one(url, settings)
        if item.get("error"):
            failed.append({"url": url, "error": item["error"]})
        else:
            results.append(
                {
                    "url": item.get("url") or url,
                    "title": item.get("title") or "",
                    "raw_content": item.get("raw_content") or "",
                }
            )
    return {"results": results, "failed_results": failed}


def cache_key_for_url(url: str) -> str:
    return hashlib.sha256(url.strip().encode()).hexdigest()
