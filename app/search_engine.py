"""Multi-engine search + ranking for Clearweb."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

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

_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with", "by", "at", "from", "is", "are",
    "was", "be", "it", "its", "as", "how", "what", "why", "when", "which", "who", "does", "do", "did", "can",
    "vs", "about", "into", "my", "your", "best", "top", "guide", "this", "that", "use", "using",
}
# Words that describe intent rather than topic: they never count as a match on their own.
_INTENT_WORDS = {"latest", "new", "newest", "current", "today", "recent", "update", "updates", "news", "2025", "2026"}

# Known low-quality / content-farm / SEO-spam domains (penalized, not banned).
_LOW_QUALITY_DOMAINS = {
    "expertbeacon.com", "besthub.dev", "note.com", "pinterest.com", "quora.com", "scribd.com", "slideshare.net",
    "coursehero.com", "brainly.com", "answers.com", "ask.com", "ehow.com", "wikihow.com", "softonic.com",
    "uptodown.com", "filehippo.com", "ithome.com.tw", "msn.com", "yahoo.com",
}
_SPAM_TLDS = (".xyz", ".top", ".click", ".buzz", ".icu", ".monster", ".cyou", ".rest", ".quest", ".sbs", ".cfd")
# Authoritative reference sources (boosted).
_HIGH_QUALITY_DOMAINS = {
    "github.com", "stackoverflow.com", "developer.mozilla.org", "wikipedia.org", "python.org", "rust-lang.org",
    "sqlite.org", "nginx.org", "docs.nginx.com", "kernel.org", "postgresql.org", "readthedocs.io",
    "arxiv.org", "lwn.net", "fastapi.tiangolo.com", "go.dev", "nodejs.org", "w3.org", "ietf.org", "rfc-editor.org",
}
_TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$|ref_src$|igshid$|featured_on$|hl$)", re.I)


def normalize_url(url: str) -> str:
    """Canonical form for dedupe: lower host, no www, no tracking params/fragment/trailing slash, http==https."""
    try:
        p = urlparse(url.strip())
    except Exception:
        return url.strip()
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m.") or host.startswith("amp."):
        host = host.split(".", 1)[1]
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False) if not _TRACKING_PARAMS.match(k)]
    path = re.sub(r"/(amp|index\.html?)$", "", p.path or "").rstrip("/")
    return urlunparse(("", host, path, "", urlencode(sorted(q)), ""))


def _domain_matches(domain: str, entries: set) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in entries)


def query_terms(query: str) -> List[str]:
    toks = [t.lower() for t in _TOKEN_RE.findall(query or "")]
    return [t for t in dict.fromkeys(toks) if t not in _STOPWORDS]


def _term_hit(term: str, tokens: set) -> bool:
    if term in tokens:
        return True
    # cheap stemming: release/releases, work/works/working
    return any(t.startswith(term) or term.startswith(t) for t in tokens if len(t) >= 4 and len(term) >= 4 and abs(len(t) - len(term)) <= 3)


def relevance(query: str, title: str, content: str, url: str = "") -> float:
    """0..1: coverage of the query's topic terms in title/body/url, title-weighted, with a phrase bonus."""
    terms = query_terms(query)
    topic = [t for t in terms if t not in _INTENT_WORDS] or terms
    if not topic:
        return 0.5
    tt, bt = _tokens(title), _tokens(content)
    ut = _tokens(re.sub(r"[^a-z0-9]+", " ", url.lower()))
    title_cov = sum(_term_hit(t, tt) for t in topic) / len(topic)
    body_cov = sum(_term_hit(t, bt | tt | ut) for t in topic) / len(topic)
    phrase = 0.0
    low = f"{title} {content}".lower()
    for a, b in zip(topic, topic[1:]):
        if f"{a} {b}" in low:
            phrase += 0.1
    return min(1.0, 0.5 * title_cov + 0.45 * body_cov + min(0.2, phrase))


def domain_quality(domain: str, query: str) -> float:
    """-0.5..+0.25: content-farm/spam penalty, authority/official-site boost."""
    score = 0.0
    if _domain_matches(domain, _LOW_QUALITY_DOMAINS):
        score -= 0.25
    if domain.endswith(_SPAM_TLDS) or domain.count("-") >= 3 or re.search(r"\d{4,}", domain):
        score -= 0.25
    if _domain_matches(domain, _HIGH_QUALITY_DOMAINS) or domain.startswith("docs.") or ".docs." in domain:
        score += 0.12
    # official project site: a topic term is the registrable name (rust-lang.org, sqlite.org, nginx.com)
    base = domain.split(".")[-2] if domain.count(".") >= 1 else domain
    if any(t in _tokens(base.replace("-", " ")) for t in query_terms(query) if t not in _INTENT_WORDS and len(t) >= 3):
        score += 0.13
    return max(-0.5, min(0.25, score))


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
    per_domain: int = 2,
) -> List[Dict[str, Any]]:
    fresh = _wants_fresh(query, topic, days)
    this_year = datetime.now(timezone.utc).year
    ranked: List[Tuple[float, Dict[str, Any]]] = []
    best_by_key: Dict[str, int] = {}
    seen_titles: Dict[str, int] = {}

    for item in items:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        key = normalize_url(url)
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

        rel = relevance(query, title, content, url)
        quality = domain_quality(domain, query)
        engine_score = _ENGINE_WEIGHT.get(engine, 0.7)
        https_score = 1.0 if url.startswith("https://") else 0.4
        low = f"{domain} {title} {content}".lower()
        spam_penalty = 0.35 if any(h in low for h in _SPAM_HINTS) else 0.0
        length_score = min(1.0, len(content) / 240.0)
        news_boost = 0.06 if engine == "ddg-news" else 0.0
        if topic == "news" or fresh:
            if any(x in domain for x in ("reuters", "apnews", "bbc.", "nytimes", "wsj.", "coindesk", "theverge", "techcrunch", "cnbc", "bloomberg", "bls.gov")):
                news_boost += 0.08
        hub_penalty = 0.4 if (fresh and _is_hub(url)) else 0.0
        years = {int(y) for y in _YEAR_RE.findall(f"{url} {title} {content}")}
        recency = 0.0
        if fresh:
            if this_year in years:
                recency += 0.1
            elif years and max(years) < this_year - 1:
                recency -= 0.25
        offtopic_penalty = 0.3 if rel < 0.35 else 0.0

        score = (
            0.55 * rel
            + 0.12 * engine_score
            + 0.05 * https_score
            + 0.05 * length_score
            + quality
            + news_boost
            + recency
            - spam_penalty
            - hub_penalty
            - offtopic_penalty
        )
        score = max(0.01, min(0.99, score))
        entry = {**item, "score": round(score, 4), "relevance": round(rel, 3), "domain": domain, "published_date": published}

        # dedupe: same canonical URL or same normalized title on the same domain -> keep the better one
        tkey = f"{domain}|{re.sub(r'[^a-z0-9]+', ' ', title.lower()).strip()[:80]}"
        dup_idx = best_by_key.get(key, seen_titles.get(tkey))
        if dup_idx is not None:
            old_score, old = ranked[dup_idx]
            if score > old_score:
                if len(content) < len(old.get("content") or ""):
                    entry["content"] = old.get("content")
                ranked[dup_idx] = (score, entry)
            elif len(content) > len(old.get("content") or ""):
                old["content"] = content
            continue
        best_by_key[key] = len(ranked)
        seen_titles[tkey] = len(ranked)
        ranked.append((score, entry))

    ranked.sort(key=lambda x: x[0], reverse=True)
    out: List[Dict[str, Any]] = []
    per: Dict[str, int] = {}
    overflow: List[Dict[str, Any]] = []
    for _, item in ranked:
        if fresh and _is_hub(item.get("url") or ""):
            overflow.append(item)
            continue
        d = item["domain"]
        if per.get(d, 0) >= per_domain:
            overflow.append(item)
            continue
        per[d] = per.get(d, 0) + 1
        out.append(item)
    return out + overflow  # diversity first; capped/hub items only fill remaining slots


async def search_exa_async(
    client: httpx.AsyncClient,
    query: str,
    limit: int,
    api_key: str,
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
    resp = await client.post(
        "https://api.exa.ai/search", headers={"x-api-key": api_key, "Content-Type": "application/json"}, json=payload
    )
    if resp.status_code != 200:
        logger.warning("exa search http %s", resp.status_code)
        return []
    return _parse_exa(resp.json())


def _parse_exa(data: Dict[str, Any]) -> List[Dict[str, Any]]:
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
        if url and title:
            out.append({"title": title, "url": url, "content": content,
                        "published_date": item.get("publishedDate") or item.get("published_date"), "engine": "exa"})
    return out


async def multi_engine_search_async(
    query: str,
    limit: int,
    settings: Settings,
    topic: str = "general",
    days: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Query all engines concurrently; each engine gets its own deadline so one slow engine can't stall the rest."""
    fetch_n = min(max(10, limit * 3), settings.max_results_cap * 2)
    fresh = _wants_fresh(query, topic, days)
    eff_days = days if days is not None else (7 if fresh else None)
    window = _timelimit(eff_days)
    deadline = min(settings.request_timeout_sec, settings.engine_timeout_sec)

    async with httpx.AsyncClient(timeout=deadline, trust_env=False) as client:
        jobs: Dict[str, Any] = {
            "ddg": asyncio.to_thread(search_duckduckgo, query, fetch_n, deadline, news=False, timelimit=window),
        }
        if fresh:
            jobs["ddg-news"] = asyncio.to_thread(search_duckduckgo, query, fetch_n, deadline, news=True, timelimit=window)
        if settings.exa_api_key:
            jobs["exa"] = search_exa_async(client, query, fetch_n, settings.exa_api_key, news=fresh, days=eff_days)
        if settings.brave_api_key:
            jobs["brave"] = asyncio.to_thread(search_brave, query, fetch_n, settings.brave_api_key, deadline)

        async def run(name: str, coro: Any) -> List[Dict[str, Any]]:
            t0 = time.perf_counter()
            try:
                res = await asyncio.wait_for(coro, timeout=deadline + 1)
                logger.info("engine %s returned %d in %.0fms", name, len(res), (time.perf_counter() - t0) * 1000)
                return res
            except Exception as exc:
                logger.warning("engine %s failed after %.0fms: %s", name, (time.perf_counter() - t0) * 1000, type(exc).__name__)
                return []

        chunks = await asyncio.gather(*(run(n, c) for n, c in jobs.items()))

    results = [r for chunk in chunks for r in chunk]
    return rank_results(query, results, topic=topic, days=days)[:limit]


def multi_engine_search(
    query: str,
    limit: int,
    settings: Settings,
    topic: str = "general",
    days: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Sync wrapper (scripts/tests)."""
    return asyncio.run(multi_engine_search_async(query, limit, settings, topic, days))


def cache_key_for_search(query: str, max_results: int, depth: str, topic: str, days: Optional[int], include_answer: bool) -> str:
    raw = f"v6|{query.strip().lower()}|{max_results}|{depth}|{topic}|{days}|{int(include_answer)}"
    return hashlib.sha256(raw.encode()).hexdigest()
