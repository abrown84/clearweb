"""Mask API keys / bearer tokens in log records."""

from __future__ import annotations

import logging
import re

_PATTERNS = [
    (re.compile(r"(?i)(api[_-]?key[\"']?\s*[=:]\s*[\"']?)[^\s&\"',}]+"), r"\1***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1***"),
    (re.compile(r"\b([a-z]{1,8}-)[A-Za-z0-9]{30,}\b"), r"\1***"),
]


def mask(text: str) -> str:
    for pat, rep in _PATTERNS:
        text = pat.sub(rep, text)
    return text


class MaskSecretsFilter(logging.Filter):
    """Mask secrets while preserving record.args shape (uvicorn's AccessFormatter unpacks args)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = mask(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(mask(a) if isinstance(a, str) else a for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: mask(v) if isinstance(v, str) else v for k, v in record.args.items()}
        except Exception:
            pass
        return True


def install() -> None:
    f = MaskSecretsFilter()
    for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error", "clearweb", "httpx"):
        lg = logging.getLogger(name)
        lg.addFilter(f)
        for h in lg.handlers:
            h.addFilter(f)
