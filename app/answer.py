"""Optional answer generation via OpenAI Codex OAuth (Hermes auth.json)."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("clearweb.answer")


def _load_codex_token(
    synced_path: Path = Path("/var/lib/clearweb/codex.json"),
    auth_path: Path = Path("/root/.hermes/auth.json"),
) -> Optional[Dict[str, str]]:
    # Prefer the root-synced token file readable by the clearweb service user.
    for path in (synced_path, auth_path):
        try:
            if not path.exists():
                continue
            data = json.loads(path.read_text())
        except Exception:
            continue
        # synced shape
        if data.get("access_token"):
            return {
                "token": data["access_token"],
                "base_url": (data.get("base_url") or "https://chatgpt.com/backend-api/codex").rstrip("/"),
                "account_id": data.get("account_id") or "",
            }
        # raw Hermes auth.json shape
        pool = (data.get("credential_pool") or {}).get("openai-codex") or []
        if not pool:
            continue
        entry = pool[0]
        token = entry.get("access_token")
        if not token:
            continue
        base = (entry.get("base_url") or "https://chatgpt.com/backend-api/codex").rstrip("/")
        acct = ""
        try:
            parts = token.split(".")
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            acct = (payload.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id") or ""
        except Exception:
            acct = ""
        return {"token": token, "base_url": base, "account_id": acct}
    return None


def generate_answer(query: str, results: List[Dict[str, Any]], model: str = "gpt-5.6-terra") -> Optional[str]:
    """Return a short grounded answer or None on any failure."""
    creds = _load_codex_token()
    if not creds:
        logger.info("answer skipped: no codex credentials")
        return None

    sources = []
    for i, r in enumerate(results[:5], 1):
        sources.append(
            f"[{i}] {r.get('title','')}\nURL: {r.get('url','')}\n{(r.get('content') or '')[:500]}"
        )
    prompt = (
        "Answer the user question using ONLY the sources below. "
        "Be concise (2-5 sentences). If sources are insufficient, say so briefly.\n\n"
        f"Question: {query}\n\nSources:\n" + "\n\n".join(sources)
    )

    headers = {
        "Authorization": f"Bearer {creds['token']}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "OpenAI-Beta": "responses=experimental",
        "User-Agent": "Clearweb/1.0",
    }
    if creds.get("account_id"):
        headers["ChatGPT-Account-Id"] = creds["account_id"]

    body = {
        "model": model,
        "instructions": "You are Clearweb. Answer only from provided sources. No tools.",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }
        ],
        "store": False,
        "stream": True,
    }

    url = creds["base_url"] + "/responses"
    try:
        with httpx.Client(timeout=60.0) as client:
            with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code >= 400:
                    logger.warning("answer http %s", resp.status_code)
                    return None
                texts: List[str] = []
                for line in resp.iter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    payload = line[6:].strip()
                    if payload in {"[DONE]", ""}:
                        continue
                    try:
                        event = json.loads(payload)
                    except Exception:
                        continue
                    typ = event.get("type")
                    if typ in {"response.output_text.delta", "response.text.delta"}:
                        texts.append(event.get("delta") or "")
                    if typ == "response.output_text.done" and event.get("text"):
                        texts = [event["text"]]
                    if typ == "response.completed":
                        resp_obj = event.get("response") or {}
                        outs = resp_obj.get("output") or []
                        final = []
                        for o in outs:
                            for c in o.get("content") or []:
                                if isinstance(c, dict) and c.get("type") in {"output_text", "text"}:
                                    final.append(c.get("text") or "")
                        if final:
                            texts = final
                answer = "".join(texts).strip()
                return answer or None
    except Exception as exc:
        logger.warning("answer generation failed: %s", exc)
        return None
