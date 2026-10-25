"""Grounded answer generation via One Gateway (OpenAI-compatible, local)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from .config import settings

logger = logging.getLogger("clearweb.answer")


def gateway_reachable(timeout: float = 2.0) -> bool:
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as c:
            r = c.get(settings.gateway_url.rstrip("/").removesuffix("/v1") + "/")
            return r.status_code < 500
    except Exception:
        return False


def select_answer_sources(results: List[Dict[str, Any]], top_n: int, min_relevance: float = 0.35) -> List[Dict[str, Any]]:
    """Top-ranked, on-topic results only (results arrive already ranked)."""
    picked = [r for r in results if r.get("relevance") is None or r.get("relevance", 0) >= min_relevance]
    return (picked or results)[: max(1, top_n)]


def generate_answer(query: str, results: List[Dict[str, Any]], model: Optional[str] = None) -> Optional[str]:
    """Return a short grounded answer or None on any failure."""
    results = select_answer_sources(results, settings.answer_top_n)
    sources = [
        f"[{i}] {(r.get('title') or '')[:80]} ({r.get('url','')[:80]})\n{(r.get('content') or '')[:700]}"
        for i, r in enumerate(results[:5], 1)
    ]
    prompt = (
        "Answer the user question using ONLY the sources below. "
        "2-4 sentences, cite like [1]; if sources are insufficient say so.\n\n"
        f"Question: {query}\n\nSources:\n" + "\n\n".join(sources)
    )
    headers = {"Content-Type": "application/json", "User-Agent": "Clearweb/1.1"}
    if settings.gateway_key:
        headers["Authorization"] = f"Bearer {settings.gateway_key}"
    body = {
        "model": model or settings.answer_model,
        "messages": [
            {"role": "system", "content": "You are Clearweb. Answer only from provided sources. No tools."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 1024,
        "reasoning_effort": "low",  # keep thinking tokens from eating the output budget
        "temperature": 0.2,
    }
    url = settings.gateway_url.rstrip("/") + "/chat/completions"
    try:
        with httpx.Client(timeout=45.0, trust_env=False) as client:
            resp = client.post(url, headers=headers, json=body)
        if resp.status_code >= 400:
            logger.warning("answer: gateway http %s", resp.status_code)
            return None
        data = resp.json()
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        return text.strip() or None
    except Exception as exc:
        logger.warning("answer generation failed: %s", type(exc).__name__)
        return None
