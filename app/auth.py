"""API key auth helpers for Clearweb."""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from fastapi import Header, HTTPException, Request

from .config import Settings
from .db import Database


@dataclass
class AuthContext:
    key_id: Optional[int]
    label: str
    is_admin: bool = False
    rpm: int = 60
    daily_quota: int = 2000


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key(prefix: str = "cw") -> str:
    # Tavily-like opaque token
    body = secrets.token_urlsafe(32).replace("-", "").replace("_", "")[:40]
    return f"{prefix}-{body}"


def extract_raw_key(
    request: Request,
    authorization: Optional[str] = None,
    body_api_key: Optional[str] = None,
) -> Optional[str]:
    if authorization:
        parts = authorization.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        if len(parts) == 1:
            return parts[0].strip()
    if body_api_key:
        return body_api_key.strip()
    # ?api_key= query support was removed: keys in URLs leak into proxy/access logs.
    return None


def authenticate(
    db: Database,
    settings: Settings,
    raw_key: Optional[str],
    *,
    allow_admin: bool = False,
) -> AuthContext:
    if not raw_key:
        raise HTTPException(status_code=401, detail="Missing API key")

    if allow_admin and settings.admin_master_key and secrets.compare_digest(raw_key, settings.admin_master_key):
        return AuthContext(key_id=None, label="admin", is_admin=True, rpm=10_000, daily_quota=1_000_000)

    row = db.get_key_by_hash(hash_key(raw_key))
    if not row:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if row["revoked_at"] is not None:
        raise HTTPException(status_code=401, detail="API key revoked")

    key_id = int(row["id"])
    rpm = int(row["rpm"])
    daily = int(row["daily_quota"])

    # RPM window
    now = time.time()
    minute_count = db.count_usage_since(key_id, now - 60)
    if minute_count >= rpm:
        raise HTTPException(status_code=429, detail="Rate limit exceeded (RPM)")

    day_count = db.count_usage_since(key_id, now - 86400)
    if day_count >= daily:
        raise HTTPException(status_code=432, detail="Daily quota exceeded")

    return AuthContext(
        key_id=key_id,
        label=str(row["label"] or ""),
        is_admin=False,
        rpm=rpm,
        daily_quota=daily,
    )
