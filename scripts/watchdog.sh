#!/usr/bin/env bash
# Every 5 min: alert if clearweb is failed, unhealthy, or crash-looping (restarts climbing). Deduped.
set -uo pipefail
STATE=/var/lib/clearweb-watchdog; mkdir -p "$STATE"
N=$(systemctl show clearweb -p NRestarts --value 2>/dev/null || echo 0)
ACTIVE=$(systemctl is-active clearweb)
LAST=$(cat "$STATE/nrestarts" 2>/dev/null || echo "$N"); echo "$N" > "$STATE/nrestarts"
problem=""
if [[ $ACTIVE != active ]]; then problem="clearweb is $ACTIVE"
elif (( N - LAST >= 3 )); then problem="clearweb restart loop: $((N-LAST)) restarts in 5 min"
elif ! curl -fsS -m 5 http://127.0.0.1:3055/health >/dev/null; then problem="clearweb /health failing on 127.0.0.1:3055"
fi
if [[ -n $problem ]]; then
  # alert at most once per hour for the same problem class
  key=$(echo "$problem" | cut -d: -f1 | tr -c 'a-z' _)
  if [[ ! -f $STATE/$key ]] || (( $(date +%s) - $(stat -c %Y "$STATE/$key") > 3600 )); then
    /opt/clearweb/scripts/alert.sh clearweb-watchdog "$problem; last log: $(journalctl -u clearweb -n 1 -o cat --no-pager | tr -d '\n' | cut -c1-200)" && touch "$STATE/$key"
  fi
else
  rm -f "$STATE"/clearweb_* 2>/dev/null
fi
