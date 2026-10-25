"""URL extraction with SSRF protections."""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx
from bs4 import BeautifulSoup

from .config import Settings
from .netguard import BlockedURL, is_blocked_ip, resolve_public, safe_fetch

logger = logging.getLogger("clearweb.extract")


def _is_blocked_ip(ip: str) -> bool:
    return is_blocked_ip(ip)


def validate_public_url(url: str) -> Tuple[bool, str]:
    try:
        resolve_public(url)
    except BlockedURL as exc:
        return False, str(exc)
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
        try:
            fr = safe_fetch(
                url,
                timeout=settings.request_timeout_sec,
                max_bytes=settings.extract_max_bytes,
                headers=headers,
            )
        except BlockedURL as exc:
            return {"url": url, "title": "", "raw_content": "", "error": f"blocked: {exc}"}
        final = fr.final_url
        if fr.status >= 400:
            return {"url": url, "title": "", "raw_content": "", "error": f"http {fr.status}"}
        ctype = fr.content_type
        if ctype and not any(x in ctype for x in ("html", "text", "xml", "json")):
            return {"url": final, "title": "", "raw_content": "", "error": f"unsupported content-type {ctype}"}
        raw = fr.body
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
