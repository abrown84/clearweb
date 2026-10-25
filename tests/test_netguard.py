import httpx
import pytest

from app.netguard import BlockedURL, is_blocked_ip, resolve_public, safe_fetch

BLOCKED = [
    "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "0.0.0.0",
    "100.64.0.1", "100.127.255.254",            # CGNAT
    "198.18.0.1", "224.0.0.1", "255.255.255.255",
    "::1", "::", "fe80::1", "fc00::1", "fd12::1",
    "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254", "::ffff:100.64.0.1",  # v4-mapped
    "::127.0.0.1",                              # v4-compatible
    "64:ff9b::a9fe:a9fe", "64:ff9b::7f00:1",    # NAT64 embedding 169.254.169.254 / 127.0.0.1
    "2002:7f00:1::1", "2002:a9fe:a9fe::",       # 6to4 embedding private v4
    "2001:0:4136:e378:8000:63bf:80ff:fffe",     # Teredo (blocked range; embeds 127.0.0.1)
    "not-an-ip",
]
ALLOWED = ["93.184.215.14", "1.1.1.1", "8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8"]


@pytest.mark.parametrize("ip", BLOCKED)
def test_blocked(ip):
    assert is_blocked_ip(ip)


@pytest.mark.parametrize("ip", ALLOWED[:-1])
def test_allowed(ip):
    assert not is_blocked_ip(ip)


def test_mapped_public_still_blocked_conservatively():
    # ::ffff:0:0/96 is never a legitimate DNS answer for an outbound fetch
    assert is_blocked_ip("::ffff:8.8.8.8")


def fake_resolver(table):
    def r(host, port):
        return table[host]
    return r


@pytest.mark.parametrize("url", [
    "http://localhost/", "http://foo.localhost/", "file:///etc/passwd", "gopher://x/",
    "http://127.0.0.1:3055/admin/keys", "http://[::ffff:127.0.0.1]/", "http://user:pw@example.com/",
    "http://metadata.google.internal/",
])
def test_resolve_rejects(url):
    with pytest.raises(BlockedURL):
        resolve_public(url, fake_resolver({"example.com": ["93.184.215.14"]}))


def test_any_bad_record_rejects():
    with pytest.raises(BlockedURL):
        resolve_public("http://mixed.test/", fake_resolver({"mixed.test": ["93.184.215.14", "10.0.0.5"]}))


def test_resolve_ok_pins_ip():
    host, ip, port = resolve_public("https://example.com/x", fake_resolver({"example.com": ["93.184.215.14"]}))
    assert (host, ip, port) == ("example.com", "93.184.215.14", 443)


def _transport(seen, routes):
    def handler(request: httpx.Request):
        seen.append((request.url.host, request.headers.get("host"), request.extensions.get("sni_hostname")))
        return routes[(request.url.host, request.url.path)]
    return httpx.MockTransport(handler)


def test_connects_to_validated_ip_with_host_and_sni():
    seen = []
    t = _transport(seen, {("93.184.215.14", "/a"): httpx.Response(200, html="<p>hi</p>")})
    r = safe_fetch("https://example.com/a", timeout=5, max_bytes=1000, headers={},
                   resolver=fake_resolver({"example.com": ["93.184.215.14"]}), transport=t)
    assert r.status == 200 and r.body == b"<p>hi</p>"
    assert seen == [("93.184.215.14", "example.com", "example.com")]


def test_redirect_to_internal_blocked_before_request():
    seen = []
    t = _transport(seen, {("93.184.215.14", "/r"): httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})})
    with pytest.raises(BlockedURL):
        safe_fetch("http://example.com/r", timeout=5, max_bytes=1000, headers={},
                   resolver=fake_resolver({"example.com": ["93.184.215.14"]}), transport=t)
    assert len(seen) == 1  # the internal hop was never requested


def test_redirect_to_host_resolving_internal_blocked():
    seen = []
    t = _transport(seen, {("93.184.215.14", "/r"): httpx.Response(301, headers={"location": "https://evil.test/x"})})
    with pytest.raises(BlockedURL):
        safe_fetch("http://example.com/r", timeout=5, max_bytes=1000, headers={},
                   resolver=fake_resolver({"example.com": ["93.184.215.14"], "evil.test": ["100.64.1.1"]}), transport=t)
    assert len(seen) == 1


def test_public_redirect_followed_and_relative():
    seen = []
    t = _transport(seen, {
        ("93.184.215.14", "/r"): httpx.Response(302, headers={"location": "https://other.test/next"}),
        ("1.1.1.1", "/next"): httpx.Response(302, headers={"location": "/final"}),
        ("1.1.1.1", "/final"): httpx.Response(200, text="ok"),
    })
    r = safe_fetch("http://example.com/r", timeout=5, max_bytes=1000, headers={},
                   resolver=fake_resolver({"example.com": ["93.184.215.14"], "other.test": ["1.1.1.1"]}), transport=t)
    assert r.final_url == "https://other.test/final" and r.body == b"ok"


def test_too_many_redirects():
    t = httpx.MockTransport(lambda req: httpx.Response(302, headers={"location": "/loop"}))
    with pytest.raises(BlockedURL):
        safe_fetch("http://example.com/loop", timeout=5, max_bytes=1000, headers={}, max_redirects=3,
                   resolver=fake_resolver({"example.com": ["93.184.215.14"]}), transport=t)


def test_max_bytes_cap():
    t = httpx.MockTransport(lambda req: httpx.Response(200, content=b"x" * 10_000))
    r = safe_fetch("http://example.com/", timeout=5, max_bytes=100, headers={},
                   resolver=fake_resolver({"example.com": ["93.184.215.14"]}), transport=t)
    assert 100 < len(r.body) <= 10_000
