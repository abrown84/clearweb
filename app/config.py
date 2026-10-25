"""Clearweb runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 3055
    data_dir: Path = Path("/var/lib/clearweb")
    db_path: Path = Path("/var/lib/clearweb/clearweb.sqlite3")
    admin_master_key: str = ""
    brave_api_key: str = ""
    exa_api_key: str = ""
    default_rpm: int = 60
    default_daily_quota: int = 2000
    search_cache_ttl_sec: int = 1800
    news_cache_ttl_sec: int = 600
    extract_cache_ttl_sec: int = 21600
    failed_extract_ttl_sec: int = 900
    request_timeout_sec: float = 20.0
    extract_max_bytes: int = 2_000_000
    extract_max_chars: int = 50_000
    max_results_cap: int = 20
    answer_enabled: bool = True
    answer_model: str = "gpt-5.6-terra"
    answer_provider: str = "openai-codex"
    public_base_path: str = "/labs/clearweb"
    user_agent: str = "Clearweb/1.0 (+https://brwnindustries.com/labs/clearweb; research agent)"

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(_env("CLEARWEB_DATA_DIR", "/var/lib/clearweb"))
        db_path = Path(_env("CLEARWEB_DB_PATH", str(data_dir / "clearweb.sqlite3")))
        return cls(
            host=_env("CLEARWEB_HOST", "127.0.0.1"),
            port=_env_int("CLEARWEB_PORT", 3055),
            data_dir=data_dir,
            db_path=db_path,
            admin_master_key=_env("CLEARWEB_ADMIN_KEY"),
            brave_api_key=_env("BRAVE_SEARCH_API_KEY") or _env("BRAVE_API_KEY"),
            exa_api_key=_env("EXA_API_KEY"),
            default_rpm=_env_int("CLEARWEB_DEFAULT_RPM", 60),
            default_daily_quota=_env_int("CLEARWEB_DEFAULT_DAILY_QUOTA", 2000),
            search_cache_ttl_sec=_env_int("CLEARWEB_SEARCH_CACHE_TTL", 1800),
            news_cache_ttl_sec=_env_int("CLEARWEB_NEWS_CACHE_TTL", 600),
            extract_cache_ttl_sec=_env_int("CLEARWEB_EXTRACT_CACHE_TTL", 21600),
            failed_extract_ttl_sec=_env_int("CLEARWEB_FAILED_EXTRACT_TTL", 900),
            request_timeout_sec=_env_float("CLEARWEB_REQUEST_TIMEOUT", 20.0),
            extract_max_bytes=_env_int("CLEARWEB_EXTRACT_MAX_BYTES", 2_000_000),
            extract_max_chars=_env_int("CLEARWEB_EXTRACT_MAX_CHARS", 50_000),
            max_results_cap=_env_int("CLEARWEB_MAX_RESULTS", 20),
            answer_enabled=_env("CLEARWEB_ANSWER_ENABLED", "1") not in {"0", "false", "False", "no"},
            answer_model=_env("CLEARWEB_ANSWER_MODEL", "gpt-5.6-terra"),
            answer_provider=_env("CLEARWEB_ANSWER_PROVIDER", "openai-codex"),
            public_base_path=_env("CLEARWEB_PUBLIC_BASE_PATH", "/labs/clearweb").rstrip("/") or "",
            user_agent=_env(
                "CLEARWEB_USER_AGENT",
                "Clearweb/1.0 (+https://brwnindustries.com/labs/clearweb; research agent)",
            ),
        )


settings = Settings.from_env()
