#!/usr/bin/env bash
# Post a task_error event to Agent Progress (pushes to Alex's phone). Runs as root.
# Usage: alert.sh <task_id> <description>.  Never echoes the token.
set -uo pipefail
SECRETS=/root/.secrets/agent-progress
[[ -r $SECRETS ]] || { echo "alert: no secrets file" >&2; exit 1; }
TOKEN=$(sed -n 's/^WRITE_TOKEN=//p' "$SECRETS" | tr -d '"'"'"'\r')
URL=$(sed -n 's/^URL=//p' "$SECRETS" | tr -d '"'"'"'\r'); URL=${URL:-https://progress.brwnindustries.com}
TASK=${1:-clearweb}; DESC=${2:-clearweb alert}
BODY=$(python3 -c 'import json,sys,time,socket; print(json.dumps({"agent":{"name":"clearweb-watchdog","source":"custom","label":"Clearweb ("+socket.gethostname()+")"},"events":[{"type":"task_error","task_id":sys.argv[1],"tool":"systemd","description":sys.argv[2],"timestamp":int(time.time()*1000)}]}))' "$TASK" "$DESC")
code=$(curl -sS -o /dev/null -w '%{http_code}' -m 15 -X POST "${URL%/}/api/events" \
  -H "Content-Type: application/json" -H @<(printf 'Authorization: Bearer %s\n' "$TOKEN") --data "$BODY")
echo "alert posted: http $code"
[[ $code == 2* ]]
