#!/usr/bin/env python3
"""Sync Hermes Codex OAuth material into a Clearweb-readable token file.

Writes /var/lib/clearweb/codex.json (root:clearweb, 0640) with:
  access_token, base_url, account_id

Never prints secrets.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

SRC = Path(os.environ.get("HERMES_AUTH_JSON", "/root/.hermes/auth.json"))
DST = Path(os.environ.get("CLEARWEB_CODEX_JSON", "/var/lib/clearweb/codex.json"))
GROUP = os.environ.get("CLEARWEB_GROUP", "clearweb")


def main() -> int:
    if not SRC.exists():
        print("source missing", file=sys.stderr)
        return 1
    data = json.loads(SRC.read_text())
    pool = (data.get("credential_pool") or {}).get("openai-codex") or []
    if not pool:
        print("no openai-codex credentials", file=sys.stderr)
        return 2
    entry = pool[0]
    token = entry.get("access_token") or ""
    if not token:
        print("empty access_token", file=sys.stderr)
        return 3
    base = (entry.get("base_url") or "https://chatgpt.com/backend-api/codex").rstrip("/")
    acct = ""
    try:
        parts = token.split(".")
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        acct = (payload.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id") or ""
    except Exception:
        acct = ""
    out = {
        "access_token": token,
        "base_url": base,
        "account_id": acct,
        "source": str(SRC),
    }
    DST.parent.mkdir(parents=True, exist_ok=True)
    tmp = DST.with_suffix(".tmp")
    tmp.write_text(json.dumps(out))
    os.chmod(tmp, 0o640)
    # chown root:clearweb
    import grp
    import pwd

    uid = 0
    try:
        gid = grp.getgrnam(GROUP).gr_gid
    except KeyError:
        gid = -1
    os.chown(tmp, uid, gid if gid >= 0 else -1)
    tmp.replace(DST)
    print(f"synced codex token -> {DST} (account={'yes' if acct else 'no'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
