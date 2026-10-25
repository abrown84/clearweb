"""SQLite persistence for Clearweb keys, cache, and usage."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS api_keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key_hash TEXT NOT NULL UNIQUE,
  key_prefix TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  revoked_at REAL,
  rpm INTEGER NOT NULL DEFAULT 60,
  daily_quota INTEGER NOT NULL DEFAULT 2000
);

CREATE TABLE IF NOT EXISTS usage_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key_id INTEGER,
  endpoint TEXT NOT NULL,
  status INTEGER NOT NULL,
  latency_ms INTEGER NOT NULL,
  cache_hit INTEGER NOT NULL DEFAULT 0,
  query_hash TEXT,
  created_at REAL NOT NULL,
  FOREIGN KEY(key_id) REFERENCES api_keys(id)
);

CREATE TABLE IF NOT EXISTS search_cache (
  cache_key TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS extract_cache (
  cache_key TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_key_time ON usage_events(key_id, created_at);
CREATE INDEX IF NOT EXISTS idx_search_exp ON search_cache(expires_at);
CREATE INDEX IF NOT EXISTS idx_extract_exp ON extract_cache(expires_at);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(SCHEMA)
                conn.commit()
            finally:
                conn.close()

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def create_key(
        self,
        key_hash: str,
        key_prefix: str,
        label: str,
        rpm: int,
        daily_quota: int,
    ) -> int:
        now = time.time()
        with self.conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO api_keys(key_hash, key_prefix, label, created_at, rpm, daily_quota)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (key_hash, key_prefix, label, now, rpm, daily_quota),
            )
            return int(cur.lastrowid)

    def get_key_by_hash(self, key_hash: str) -> Optional[sqlite3.Row]:
        with self.conn() as conn:
            row = conn.execute(
                "SELECT * FROM api_keys WHERE key_hash = ?",
                (key_hash,),
            ).fetchone()
            return row

    def revoke_key(self, key_id: int) -> bool:
        now = time.time()
        with self.conn() as conn:
            cur = conn.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (now, key_id),
            )
            return cur.rowcount > 0

    def list_keys(self) -> List[Dict[str, Any]]:
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT id, key_prefix, label, created_at, revoked_at, rpm, daily_quota FROM api_keys ORDER BY id DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def count_usage_since(self, key_id: int, since_ts: float) -> int:
        with self.conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM usage_events WHERE key_id = ? AND created_at >= ?",
                (key_id, since_ts),
            ).fetchone()
            return int(row["c"] if row else 0)

    def log_usage(
        self,
        key_id: Optional[int],
        endpoint: str,
        status: int,
        latency_ms: int,
        cache_hit: bool = False,
        query_hash: str = "",
    ) -> None:
        with self.conn() as conn:
            conn.execute(
                """
                INSERT INTO usage_events(key_id, endpoint, status, latency_ms, cache_hit, query_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key_id,
                    endpoint,
                    status,
                    latency_ms,
                    1 if cache_hit else 0,
                    query_hash or None,
                    time.time(),
                ),
            )

    def get_cache(self, table: str, cache_key: str) -> Optional[Dict[str, Any]]:
        assert table in {"search_cache", "extract_cache"}
        now = time.time()
        with self.conn() as conn:
            row = conn.execute(
                f"SELECT payload_json, expires_at FROM {table} WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if not row:
                return None
            if float(row["expires_at"]) < now:
                conn.execute(f"DELETE FROM {table} WHERE cache_key = ?", (cache_key,))
                return None
            return json.loads(row["payload_json"])

    def set_cache(self, table: str, cache_key: str, payload: Dict[str, Any], ttl_sec: int) -> None:
        assert table in {"search_cache", "extract_cache"}
        now = time.time()
        with self.conn() as conn:
            conn.execute(
                f"""
                INSERT INTO {table}(cache_key, payload_json, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                  payload_json = excluded.payload_json,
                  created_at = excluded.created_at,
                  expires_at = excluded.expires_at
                """,
                (cache_key, json.dumps(payload, ensure_ascii=False), now, now + ttl_sec),
            )

    def usage_summary(self, hours: int = 24) -> Dict[str, Any]:
        since = time.time() - hours * 3600
        with self.conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM usage_events WHERE created_at >= ?",
                (since,),
            ).fetchone()["c"]
            by_endpoint = conn.execute(
                """
                SELECT endpoint, COUNT(*) AS c, AVG(latency_ms) AS avg_ms,
                       SUM(cache_hit) AS hits
                FROM usage_events
                WHERE created_at >= ?
                GROUP BY endpoint
                """,
                (since,),
            ).fetchall()
            by_key = conn.execute(
                """
                SELECT k.id, k.key_prefix, k.label, COUNT(u.id) AS c
                FROM api_keys k
                LEFT JOIN usage_events u
                  ON u.key_id = k.id AND u.created_at >= ?
                GROUP BY k.id
                ORDER BY c DESC
                """,
                (since,),
            ).fetchall()
        return {
            "hours": hours,
            "total_events": int(total),
            "by_endpoint": [dict(r) for r in by_endpoint],
            "by_key": [dict(r) for r in by_key],
        }

    def prune(self, usage_retention_days: int = 35) -> Dict[str, int]:
        """Delete expired cache rows and usage events older than the retention window."""
        now = time.time()
        out: Dict[str, int] = {}
        with self.conn() as conn:
            for table in ("search_cache", "extract_cache"):
                out[table] = conn.execute(f"DELETE FROM {table} WHERE expires_at < ?", (now,)).rowcount
            out["usage_events"] = conn.execute(
                "DELETE FROM usage_events WHERE created_at < ?", (now - usage_retention_days * 86400,)
            ).rowcount
        return out

    def ping(self) -> bool:
        with self.conn() as conn:
            return conn.execute("SELECT 1").fetchone()[0] == 1
