# Clearweb

Self-hosted, **Tavily-compatible** web search + page extraction API for AI agents.
Send a query (or URLs) → get ranked results from several engines with clean text, plus an optional short
grounded answer generated through **One Gateway** (`gemini-3.8-flash`).

Live at `https://brwnindustries.com/labs/clearweb/` (API only, key required).

## Endpoints

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/health` | public | Liveness: `{"status":"ok"}` only |
| GET | `/ready` | localhost only | DB, engines, gateway reachability (503 if degraded) |
| POST | `/search` | API key | Tavily body: `query`, `max_results`, `topic` (`general`/`news`), `days`, `search_depth`, `include_answer` |
| POST | `/extract` | API key | `urls` (≤10). SSRF-guarded (see below) |
| POST/GET | `/admin/keys`, `/admin/keys/{id}/revoke`, `/admin/usage` | admin key + localhost | Rate-limited (`CLEARWEB_ADMIN_RPM`, default 20/min) |

API keys go in `Authorization: Bearer <key>` or the JSON body `api_key`. `?api_key=` is **not** accepted
(keys in URLs leak into logs). Keys/bearer tokens are masked in logs.

```bash
# on the VPS: mint a key (admin key is in /etc/clearweb/env)
curl -s -X POST localhost:3055/admin/keys -H "Authorization: Bearer $CLEARWEB_ADMIN_KEY" -d '{"label":"my-agent"}' -H 'content-type: application/json'
curl -s https://brwnindustries.com/labs/clearweb/search -H "Authorization: Bearer cw-..." \
  -H 'content-type: application/json' -d '{"query":"latest python release","include_answer":true}'
```

## Stack

Python 3.14 (pinned, `.python-version`), FastAPI + uvicorn, httpx, trafilatura/BeautifulSoup/lxml for
extraction, `ddgs` (DuckDuckGo, fallback `duckduckgo_search`) + Exa (+ Brave if keyed) for search,
SQLite (`/var/lib/clearweb`) for keys, usage, and caches. Expired cache rows and usage older than
`CLEARWEB_USAGE_RETENTION_DAYS` (35) are pruned hourly.

## SSRF protection (`app/netguard.py`)

Every hop of `/extract` is resolved and validated *before* it is requested; redirects are followed manually
(max 5). The TCP connection is pinned to the validated IP (Host header + TLS SNI/cert check keep the
hostname), so DNS rebinding between check and connect does not work. Blocked: private/loopback/link-local,
CGNAT `100.64.0.0/10`, benchmark/doc/reserved/multicast ranges, IPv4-mapped/compatible, NAT64, 6to4 and
Teredo IPv6 addresses that embed IPv4, `localhost`/`.local`/`.internal` names, URL credentials, non-HTTP schemes.
If any DNS answer is blocked the URL is rejected.

## Configuration (`/etc/clearweb/env`, never committed)

`CLEARWEB_ADMIN_KEY`, `EXA_API_KEY`, `BRAVE_SEARCH_API_KEY`, `CLEARWEB_GATEWAY_URL`
(default `http://127.0.0.1:3070/v1`), `CLEARWEB_GATEWAY_KEY` (optional), `CLEARWEB_ANSWER_MODEL`
(default `gemini-3.8-flash`), `CLEARWEB_ANSWER_ENABLED`, `CLEARWEB_DEFAULT_RPM`, `CLEARWEB_DEFAULT_DAILY_QUOTA`,
cache TTLs — see `app/config.py`.

## Deploy

Code lives in `/opt/clearweb` (this repo), runs as user `clearweb` via `deploy/systemd/clearweb.service`
behind nginx (`/labs/clearweb/` on brwnindustries.com, see `deploy/nginx/`).

```bash
cd /opt/clearweb && sudo git pull && sudo scripts/deploy.sh          # rebuild venv from requirements.lock
sudo scripts/deploy.sh --lock                                         # re-resolve requirements.txt → requirements.lock
```

`deploy.sh` builds a fresh venv with the pinned interpreter, installs `requirements.lock`, runs `pip check`,
an import smoke test and the test suite, swaps the venv in, restarts, checks `/health`, and rolls back on failure.

Reliability: `ExecStartPre` asserts Python 3.14 + imports; `StartLimitBurst=5/300s` then
`OnFailure=clearweb-alert.service`; `clearweb-watchdog.timer` (5 min) alerts on failed/unhealthy/restart-loop.
Alerts post a `task_error` event to Agent Progress (token read by root from `/root/.secrets/agent-progress`).

## Tests

```bash
python3.14 -m venv .venv-dev && .venv-dev/bin/pip install -r requirements.lock -r requirements-dev.txt
.venv-dev/bin/pytest -q
```
