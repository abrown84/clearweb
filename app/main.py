"""Clearweb — Tavily-compatible multi-engine web search + extract API."""

from __future__ import annotations

import collections
import contextlib
import ipaddress
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import logmask
from .answer import gateway_reachable, generate_answer
from .auth import AuthContext, authenticate, extract_raw_key, generate_api_key, hash_key
from .config import settings
from .db import Database
from .extract_engine import cache_key_for_url, extract_many
import asyncio

from .search_engine import cache_key_for_search, multi_engine_search_async, relevant_chunks

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("clearweb")
logmask.install()

db = Database(settings.db_path)
@contextlib.asynccontextmanager
async def _lifespan(_app):
    threading.Thread(target=_prune_loop, name="clearweb-prune", daemon=True).start()
    yield


app = FastAPI(title="Clearweb", version="1.1.0", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)

_admin_hits: Dict[str, collections.deque] = collections.defaultdict(collections.deque)
_admin_lock = threading.Lock()


def _is_local(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _admin_guard(request: Request) -> None:
    """/admin is localhost-only (also enforced in nginx) and rate-limited per client."""
    if not _is_local(request):
        raise HTTPException(status_code=404, detail="Not Found")
    ip = request.client.host if request.client else "?"
    now = time.time()
    with _admin_lock:
        dq = _admin_hits[ip]
        while dq and dq[0] < now - 60:
            dq.popleft()
        if len(dq) >= settings.admin_rpm:
            raise HTTPException(status_code=429, detail="Admin rate limit exceeded")
        dq.append(now)


def _prune_loop() -> None:
    while True:
        try:
            removed = db.prune(settings.usage_retention_days)
            logger.info("prune: %s", removed)
        except Exception as exc:  # pragma: no cover
            logger.warning("prune failed: %s", exc)
        time.sleep(3600)




class SearchBody(BaseModel):
    api_key: Optional[str] = None
    query: str
    max_results: int = 5
    search_depth: str = "basic"
    topic: str = "general"
    days: Optional[int] = None
    include_answer: bool = False
    include_raw_content: bool = False
    include_images: bool = False


class ExtractBody(BaseModel):
    api_key: Optional[str] = None
    urls: List[str] = Field(default_factory=list)
    include_images: bool = False


class CreateKeyBody(BaseModel):
    label: str = ""
    rpm: Optional[int] = None
    daily_quota: Optional[int] = None
    prefix: str = "cw"


def _auth(request: Request, authorization: Optional[str], body_key: Optional[str], admin: bool = False) -> AuthContext:
    raw = extract_raw_key(request, authorization, body_key)
    return authenticate(db, settings, raw, allow_admin=admin)


def _strip_base(path: str) -> str:
    # support mounting under /labs/clearweb without caring about prefix
    base = settings.public_base_path
    if base and path.startswith(base):
        return path[len(base) :] or "/"
    return path


@app.middleware("http")
async def normalize_prefix(request: Request, call_next):
    # Allow both /search and /labs/clearweb/search
    path = request.scope.get("path") or ""
    base = settings.public_base_path
    if base and path.startswith(base + "/"):
        request.scope["path"] = path[len(base) :] or "/"
    elif base and path == base:
        request.scope["path"] = "/"
    return await call_next(request)


@app.get("/")
def root():
    return {
        "name": "Clearweb",
        "version": "1.1.0",
        "description": "Clean multi-engine web search + extract for AI agents (Tavily-compatible).",
        "endpoints": ["/health", "/search", "/extract"],
    }


@app.get("/health")
def health():
    """Public liveness probe: minimal on purpose."""
    return {"status": "ok"}


@app.get("/ready")
def ready(request: Request):
    """Private readiness probe (localhost only): DB, engines, answer gateway."""
    if not _is_local(request):
        raise HTTPException(status_code=404, detail="Not Found")
    checks: Dict[str, Any] = {}
    try:
        checks["db"] = db.ping()
    except Exception:
        checks["db"] = False
    checks["engines"] = {"duckduckgo": True, "exa": bool(settings.exa_api_key), "brave": bool(settings.brave_api_key)}
    checks["answer_gateway"] = gateway_reachable() if settings.answer_enabled else None
    ok = bool(checks["db"]) and (checks["answer_gateway"] is not False)
    return JSONResponse(status_code=200 if ok else 503, content={"status": "ok" if ok else "degraded", "checks": checks})


@app.post("/search")
async def search(body: SearchBody, request: Request, authorization: Optional[str] = Header(default=None)):
    t0 = time.time()
    auth = await asyncio.to_thread(_auth, request, authorization, body.api_key)
    query = (body.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")

    limit = max(1, min(int(body.max_results or 5), settings.max_results_cap))
    topic = (body.topic or "general").lower()
    depth = (body.search_depth or "basic").lower()
    include_answer = bool(body.include_answer)

    ck = cache_key_for_search(query, limit, depth, topic, body.days, include_answer)
    cached = db.get_cache("search_cache", ck)
    if cached is not None:
        # if answer requested but cache lacks it and we can generate, refresh answer only
        if include_answer and not cached.get("answer") and settings.answer_enabled:
            ans = await asyncio.to_thread(generate_answer, query, cached.get("results") or [], settings.answer_model)
            if ans:
                cached = {**cached, "answer": ans}
                ttl = settings.news_cache_ttl_sec if topic == "news" else settings.search_cache_ttl_sec
                db.set_cache("search_cache", ck, cached, ttl)
        latency = int((time.time() - t0) * 1000)
        db.log_usage(auth.key_id, "search", 200, latency, cache_hit=True, query_hash=ck)
        cached = {**cached, "response_time": round(latency / 1000.0, 3)}
        return cached

    results = await multi_engine_search_async(query, limit=limit, settings=settings, topic=topic, days=body.days)
    enrich = topic == "news" or depth == "advanced"
    if enrich and results:
        take = 2 if topic == "news" else 3
        enrich_urls = [
            r["url"]
            for r in results[:take]
            if not (r.get("engine") == "exa" and (r.get("content") or "").strip())
        ]
        if enrich_urls:
            parts = await asyncio.gather(*(asyncio.to_thread(extract_many, [u], settings) for u in enrich_urls))
            extracted = {"results": [r for p in parts for r in p["results"]]}
        else:
            extracted = {"results": []}
        by_url = {r["url"]: r.get("raw_content") or "" for r in extracted.get("results") or []}
        for r in results[:take]:
            if r.get("engine") == "exa" and (r.get("content") or "").strip():
                continue
            extra = by_url.get(r["url"]) or ""
            if not extra:
                continue
            chunks = relevant_chunks(query, extra)
            if len(chunks) > len(r.get("content") or ""):
                r["content"] = chunks

    answer = None
    if include_answer and settings.answer_enabled and results:
        answer = await asyncio.to_thread(generate_answer, query, results, settings.answer_model)

    payload = {
        "query": query,
        "follow_up_questions": None,
        "answer": answer,
        "images": [],
        "results": [
            {
                "title": r.get("title") or "",
                "url": r.get("url") or "",
                "content": r.get("content") or "",
                "score": r.get("score") or 0.0,
                "published_date": r.get("published_date"),
                "raw_content": None,
            }
            for r in results
        ],
        "response_time": 0.0,
    }

    # Don't long-cache empty result sets — engines flake under rate limits.
    if results:
        ttl = settings.news_cache_ttl_sec if topic == "news" else settings.search_cache_ttl_sec
        db.set_cache("search_cache", ck, payload, ttl)
    else:
        db.set_cache("search_cache", ck, payload, 30)
    latency = int((time.time() - t0) * 1000)
    payload["response_time"] = round(latency / 1000.0, 3)
    db.log_usage(auth.key_id, "search", 200, latency, cache_hit=False, query_hash=ck)
    return payload


@app.post("/extract")
def extract(body: ExtractBody, request: Request, authorization: Optional[str] = Header(default=None)):
    t0 = time.time()
    auth = _auth(request, authorization, body.api_key)
    urls = [u.strip() for u in (body.urls or []) if isinstance(u, str) and u.strip()]
    if not urls:
        raise HTTPException(status_code=400, detail="urls is required")
    if len(urls) > 10:
        raise HTTPException(status_code=400, detail="max 10 urls per request")

    results: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    cache_hits = 0

    for url in urls:
        ck = cache_key_for_url(url)
        cached = db.get_cache("extract_cache", ck)
        if cached is not None:
            cache_hits += 1
            if cached.get("error"):
                failed.append({"url": url, "error": cached["error"]})
            else:
                results.append(
                    {
                        "url": cached.get("url") or url,
                        "title": cached.get("title") or "",
                        "raw_content": cached.get("raw_content") or "",
                    }
                )
            continue

        one = extract_many([url], settings)
        if one["results"]:
            item = one["results"][0]
            db.set_cache("extract_cache", ck, item, settings.extract_cache_ttl_sec)
            results.append(item)
        else:
            err = (one["failed_results"][0] if one["failed_results"] else {"url": url, "error": "extract failed"})
            db.set_cache(
                "extract_cache",
                ck,
                {"url": url, "title": "", "raw_content": "", "error": err.get("error", "extract failed")},
                settings.failed_extract_ttl_sec,
            )
            failed.append({"url": url, "error": err.get("error", "extract failed")})

    latency = int((time.time() - t0) * 1000)
    db.log_usage(
        auth.key_id,
        "extract",
        200,
        latency,
        cache_hit=cache_hits == len(urls),
        query_hash="",
    )
    return {"results": results, "failed_results": failed, "response_time": round(latency / 1000.0, 3)}


@app.post("/admin/keys")
def create_key(body: CreateKeyBody, request: Request, authorization: Optional[str] = Header(default=None)):
    _admin_guard(request)
    auth = _auth(request, authorization, None, admin=True)
    if not auth.is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    raw = generate_api_key(prefix=(body.prefix or "cw").strip() or "cw")
    kid = db.create_key(
        key_hash=hash_key(raw),
        key_prefix=raw[:10],
        label=body.label or "",
        rpm=int(body.rpm or settings.default_rpm),
        daily_quota=int(body.daily_quota or settings.default_daily_quota),
    )
    return {
        "id": kid,
        "api_key": raw,
        "label": body.label or "",
        "rpm": int(body.rpm or settings.default_rpm),
        "daily_quota": int(body.daily_quota or settings.default_daily_quota),
        "note": "Store this key now; it will not be shown again.",
    }


@app.get("/admin/keys")
def list_keys(request: Request, authorization: Optional[str] = Header(default=None)):
    _admin_guard(request)
    auth = _auth(request, authorization, None, admin=True)
    if not auth.is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    return {"keys": db.list_keys()}


@app.post("/admin/keys/{key_id}/revoke")
def revoke_key(key_id: int, request: Request, authorization: Optional[str] = Header(default=None)):
    _admin_guard(request)
    auth = _auth(request, authorization, None, admin=True)
    if not auth.is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    ok = db.revoke_key(key_id)
    if not ok:
        raise HTTPException(status_code=404, detail="key not found or already revoked")
    return {"revoked": True, "id": key_id}


@app.get("/admin/usage")
def usage(request: Request, hours: int = 24, authorization: Optional[str] = Header(default=None)):
    _admin_guard(request)
    auth = _auth(request, authorization, None, admin=True)
    if not auth.is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    return db.usage_summary(hours=max(1, min(hours, 24 * 30)))


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    # Tavily-ish error body
    detail = exc.detail
    if isinstance(detail, str):
        body = {"detail": {"error": detail}}
    else:
        body = {"detail": detail}
    return JSONResponse(status_code=exc.status_code, content=body)
