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


def generate_answer(query: str, results: List[Dict[str, Any]], model: Optional[str] = None) -> Optional[str]:
    """Return a short grounded answer or None on any failure."""
    sources = [
        f"[{i}] {r.get('title','')}\nURL: {r.get('url','')}\n{(r.get('content') or '')[:500]}"
        for i, r in enumerate(results[:5], 1)
    ]
    prompt = (
        "Answer the user question using ONLY the sources below. "
        "Be concise (2-5 sentences) and cite sources like [1]. If sources are insufficient, say so briefly.\n\n"
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
        "max_tokens": 400,
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
