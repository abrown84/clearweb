"""Multi-engine search + ranking for Clearweb."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from .config import Settings

logger = logging.getLogger("clearweb.search")

_TOKEN_RE = re.compile(r"[a-z0-9]{2,}", re.I)

_SPAM_HINTS = (
    "porn",
    "xxx",
    "casino",
    "betting",
    "clickbait",
    "free-vbucks",
)

_ENGINE_WEIGHT = {
    "exa": 1.05,
    "brave": 1.0,
    "ddg-news": 0.95,
    "ddg": 0.85,
}

_YEAR_RE = re.compile(r"20[12]\d")


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text or "")}


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, len(a))


def _is_hub(url: str) -> bool:
    path = urlparse(url).path or "/"
    parts = [p for p in path.split("/") if p and p.lower() not in {"index.html", "index.htm"}]
    if not parts:
        return True
    hubs = {
        "world",
        "technology",
        "tech",
        "markets",
        "market",
        "news",
        "business",
        "politics",
        "economy",
        "home",
        "investing",
        "personal-finance",
    }
    if len(parts) == 1 and parts[0].lower() in hubs:
        return True
    return False


def _timelimit(days: Optional[int]) -> Optional[str]:
    if days is None:
        return None
    if days <= 1:
        return "d"
    if days <= 7:
        return "w"
    if days <= 31:
        return "m"
    return "y"


def _wants_fresh(query: str, topic: str, days: Optional[int]) -> bool:
    if topic == "news" or days is not None:
        return True
    low = (query or "").lower()
    return any(word in low for word in ("latest", "today", "breaking", "this week"))


def _domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def search_duckduckgo(
    query: str,
    limit: int,
    timeout: float,
    *,
    news: bool = False,
    timelimit: Optional[str] = None,
) -> List[Dict[str, Any]]:
    try:
        from ddgs import DDGS
    except Exception:
        try:
            from duckduckgo_search import DDGS
        except Exception as exc:  # pragma: no cover
            logger.warning("ddgs import failed: %s", exc)
            return []

    out: List[Dict[str, Any]] = []
    try:
        with DDGS(timeout=timeout) as ddgs:
            kwargs = {"max_results": limit}
            if timelimit:
                kwargs["timelimit"] = timelimit
            finder = ddgs.news if news and hasattr(ddgs, "news") else ddgs.text
            for item in finder(query, **kwargs):
                url = (item.get("href") or item.get("link") or item.get("url") or "").strip()
                title = (item.get("title") or "").strip()
                body = (item.get("body") or item.get("snippet") or "").strip()
                date = (item.get("date") or "").strip()
                if date and date not in body:
                    body = f"{date}. {body}".strip()
                if not url or not title:
                    continue
                out.append(
                    {
                        "title": title,
                        "url": url,
                        "content": body,
                        "published_date": date or None,
                        "engine": "ddg-news" if news else "ddg",
                    }
                )
    except Exception as exc:
        logger.warning("ddg search failed news=%s: %s", news, exc)
    return out


def search_brave(query: str, limit: int, api_key: str, timeout: float) -> List[Dict[str, Any]]:
    if not api_key:
        return []
    url = "https://api.search.brave.com/res/v1/web/search"
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": api_key,
    }
    params = {"q": query, "count": min(limit, 20)}
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, headers=headers, params=params)
            if resp.status_code != 200:
                logger.warning("brave search http %s", resp.status_code)
                return []
            data = resp.json()
    except Exception as exc:
        logger.warning("brave search failed: %s", exc)
        return []

    out: List[Dict[str, Any]] = []
    for item in (data.get("web") or {}).get("results") or []:
        u = (item.get("url") or "").strip()
        title = (item.get("title") or "").strip()
        desc = (item.get("description") or "").strip()
        if not u or not title:
            continue
        out.append({"title": title, "url": u, "content": desc, "engine": "brave"})
    return out


def search_exa(
    query: str,
    limit: int,
    api_key: str,
    timeout: float,
    *,
    news: bool = False,
    days: Optional[int] = None,
) -> List[Dict[str, Any]]:
    if not api_key:
        return []
    payload: Dict[str, Any] = {
        "query": query,
        "type": "fast",
        "numResults": min(max(limit, 1), 10),
        "contents": {"highlights": True},
        "userLocation": "US",
    }
    if news:
        payload["category"] = "news"
    if days is not None:
        start = datetime.now(timezone.utc) - timedelta(days=days)
        payload["startPublishedDate"] = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                "https://api.exa.ai/search",
                headers={"x-api-key": api_key, "Content-Type": "application/json"},
                json=payload,
            )
            if resp.status_code != 200:
                logger.warning("exa search http %s", resp.status_code)
                return []
            data = resp.json()
    except Exception as exc:
        logger.warning("exa search failed: %s", exc)
        return []

    out: List[Dict[str, Any]] = []
    for item in data.get("results") or []:
        url = (item.get("url") or "").strip()
        title = (item.get("title") or "").strip()
        highlights = item.get("highlights") or []
        if isinstance(highlights, str):
            highlights = [highlights]
        content = " [...] ".join(str(h).strip() for h in highlights if str(h).strip())
        if not content:
            content = (item.get("text") or item.get("summary") or "").strip()[:1800]
        published = item.get("publishedDate") or item.get("published_date") or None
        if not url or not title:
            continue
        out.append(
            {
                "title": title,
                "url": url,
                "content": content,
                "published_date": published,
                "engine": "exa",
            }
        )
    return out


def _parse_published(raw: str) -> Optional[datetime]:
    text = (raw or "").strip()
    if not text:
        return None
    rel = re.match(r"(\d+)\s+(minute|hour|day|week)s?\s+ago", text, re.I)
    if rel:
        n = int(rel.group(1))
        unit = rel.group(2).lower()
        delta = {
            "minute": timedelta(minutes=n),
            "hour": timedelta(hours=n),
            "day": timedelta(days=n),
            "week": timedelta(weeks=n),
        }[unit]
        return datetime.now(timezone.utc) - delta
    iso = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    found = re.search(r"(20\d{2}-\d{2}-\d{2})", text)
    if found:
        return datetime.strptime(found.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text[:32], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def relevant_chunks(query: str, text: str, n: int = 2, size: int = 700) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if len(cleaned) <= size:
        return cleaned
    q = _tokens(query)
    parts = re.split(r"(?<=[.!?])\s+", cleaned)
    windows: List[str] = []
    buf = ""
    for part in parts:
        if buf and len(buf) + len(part) + 1 > size:
            windows.append(buf)
            buf = part
        else:
            buf = f"{buf} {part}".strip()
    if buf:
        windows.append(buf)
    if not windows:
        return cleaned[: size * n]
    ranked = sorted(windows, key=lambda w: _overlap(q, _tokens(w)), reverse=True)
    picked: List[str] = []
    for window in ranked:
        if any(window[:60] in prior or prior[:60] in window for prior in picked):
            continue
        picked.append(window)
        if len(picked) >= n:
            break
    return " [...] ".join(picked)[:1800]


def rank_results(
    query: str,
    items: List[Dict[str, Any]],
    topic: str = "general",
    days: Optional[int] = None,
) -> List[Dict[str, Any]]:
    q_tokens = _tokens(query)
    fresh = _wants_fresh(query, topic, days)
    this_year = datetime.now(timezone.utc).year
    ranked: List[Tuple[float, Dict[str, Any]]] = []
    seen = set()

    for item in items:
        url = (item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        title = item.get("title") or ""
        content = item.get("content") or ""
        published = item.get("published_date") or None
        parsed = _parse_published(str(published or "")) or _parse_published(content[:80])
        if days is not None and parsed is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            if parsed < cutoff:
                continue
        if parsed and not published:
            published = parsed.date().isoformat()
        engine = item.get("engine") or "ddg"
        domain = _domain(url)

        title_score = _overlap(q_tokens, _tokens(title))
        body_score = _overlap(q_tokens, _tokens(content))
        engine_score = _ENGINE_WEIGHT.get(engine, 0.7)
        https_score = 1.0 if url.startswith("https://") else 0.4
        spam_penalty = 0.0
        low = f"{domain} {title} {content}".lower()
        if any(h in low for h in _SPAM_HINTS):
            spam_penalty = 0.35
        length_score = min(1.0, len(content) / 240.0)
        news_boost = 0.08 if engine == "ddg-news" else 0.0
        if topic == "news" or fresh:
            if any(x in domain for x in ("reuters", "apnews", "bbc.", "nytimes", "wsj.", "coindesk", "theverge", "techcrunch", "cnbc", "bloomberg", "bls.gov")):
                news_boost += 0.12
        hub_penalty = 0.4 if (fresh and _is_hub(url)) else 0.0
        years = {int(y) for y in _YEAR_RE.findall(f"{url} {title} {content}")}
        recency = 0.0
        if fresh:
            if this_year in years:
                recency += 0.12
            elif years and max(years) < this_year - 1:
                recency -= 0.28

        score = (
            0.28 * engine_score
            + 0.30 * title_score
            + 0.22 * body_score
            + 0.08 * https_score
            + 0.07 * length_score
            + news_boost
            + recency
            - spam_penalty
            - hub_penalty
        )
        score = max(0.01, min(0.99, score))
        ranked.append((score, {**item, "score": round(score, 4), "domain": domain, "published_date": published}))

    ranked.sort(key=lambda x: x[0], reverse=True)
    if fresh:
        articles = [item for _, item in ranked if not _is_hub(item.get("url") or "")]
        if len(articles) >= 3:
            return articles
    return [item for _, item in ranked]


def multi_engine_search(
    query: str,
    limit: int,
    settings: Settings,
    topic: str = "general",
    days: Optional[int] = None,
) -> List[Dict[str, Any]]:
    fetch_n = min(max(limit * 2, limit), settings.max_results_cap * 2)
    fresh = _wants_fresh(query, topic, days)
    window = _timelimit(days if days is not None else (7 if fresh else None))
    tasks = {}
    results: List[Dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=4) as pool:
        if fresh:
            tasks[
                pool.submit(
                    search_duckduckgo,
                    query,
                    fetch_n,
                    settings.request_timeout_sec,
                    news=True,
                    timelimit=window,
                )
            ] = "ddg-news"
        tasks[
            pool.submit(
                search_duckduckgo,
                query,
                fetch_n,
                settings.request_timeout_sec,
                news=False,
                timelimit=window,
            )
        ] = "ddg"
        if settings.exa_api_key:
            tasks[
                pool.submit(
                    search_exa,
                    query,
                    fetch_n,
                    settings.exa_api_key,
                    settings.request_timeout_sec,
                    news=fresh,
                    days=days if days is not None else (7 if fresh else None),
                )
            ] = "exa"
        if settings.brave_api_key:
            tasks[
                pool.submit(
                    search_brave,
                    query,
                    fetch_n,
                    settings.brave_api_key,
                    settings.request_timeout_sec,
                )
            ] = "brave"

        for fut in as_completed(tasks):
            engine = tasks[fut]
            try:
                chunk = fut.result()
                results.extend(chunk)
                logger.info("engine %s returned %d", engine, len(chunk))
            except Exception as exc:
                logger.warning("engine %s error: %s", engine, exc)

    ranked = rank_results(query, results, topic=topic, days=days)
    return ranked[:limit]


def cache_key_for_search(query: str, max_results: int, depth: str, topic: str, days: Optional[int], include_answer: bool) -> str:
    raw = f"v5|{query.strip().lower()}|{max_results}|{depth}|{topic}|{days}|{int(include_answer)}"
    return hashlib.sha256(raw.encode()).hexdigest()
