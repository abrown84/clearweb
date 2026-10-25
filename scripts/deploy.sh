#!/usr/bin/env bash
# Rebuild Clearweb's venv from requirements.lock, verify it, swap it in, restart, roll back on failure.
# Usage (as root, from /opt/clearweb):  sudo scripts/deploy.sh [--lock]   (--lock regenerates requirements.lock)
set -euo pipefail
APP=/opt/clearweb
PY=/usr/bin/python3.$(cut -d. -f2 "$APP/.python-version")
SVC_USER=clearweb
NEXT=$APP/.venv.next
PREV=$APP/.venv.prev
cd "$APP"

[[ -x $PY ]] || { echo "pinned interpreter $PY missing (see .python-version)"; exit 1; }
rm -rf "$NEXT"; install -d -o $SVC_USER -g $SVC_USER "$NEXT"
sudo -u $SVC_USER "$PY" -m venv "$NEXT"
PIP=(sudo -u $SVC_USER env HOME=/tmp PIP_DISABLE_PIP_VERSION_CHECK=1 "$NEXT/bin/python" -m pip)
if [[ ${1:-} == --lock ]]; then
  "${PIP[@]}" install -q -r requirements.txt
  "${PIP[@]}" freeze --exclude pip > requirements.lock
  echo "requirements.lock regenerated"
else
  "${PIP[@]}" install -q --no-deps -r requirements.lock
fi
"${PIP[@]}" check
sudo -u $SVC_USER "$NEXT/bin/python" -c "import uvicorn, fastapi, httpx, trafilatura, ddgs, lxml, bs4"

# tests (dev deps go into a throwaway venv so the runtime venv stays lock-exact)
if [[ -d tests ]]; then
  T=$(mktemp -d)
  "$PY" -m venv "$T/v" >/dev/null
  "$T/v/bin/python" -m pip install -q --disable-pip-version-check -r requirements.lock -r requirements-dev.txt
  CLEARWEB_DATA_DIR="$T/data" CLEARWEB_ADMIN_KEY=test-only "$T/v/bin/python" -m pytest -q -p no:cacheprovider tests || { rm -rf "$T" "$NEXT"; echo "tests failed"; exit 1; }
  rm -rf "$T"
fi

rm -rf "$PREV"; [[ -d $APP/.venv ]] && mv "$APP/.venv" "$PREV"
mv "$NEXT" "$APP/.venv"
systemctl daemon-reload
systemctl restart clearweb
for i in $(seq 1 20); do
  sleep 1
  if curl -fsS -m 2 http://127.0.0.1:3055/health >/dev/null 2>&1; then
    sleep 5; systemctl is-active --quiet clearweb && { echo "deploy OK"; exit 0; }
  fi
done
echo "health check failed — rolling back venv" >&2
rm -rf "$APP/.venv"; mv "$PREV" "$APP/.venv"; systemctl restart clearweb; exit 1
