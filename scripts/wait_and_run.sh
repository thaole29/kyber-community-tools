#!/bin/bash
# wait_and_run.sh — wrapper for launchd cron jobs that fire near wake-from-sleep.
#
# Polls DNS resolution of a target host (default api.telegram.org) until it
# succeeds or a timeout passes, then exec's the given command. Without this
# wrapper, jobs scheduled with StartCalendarInterval can fire 1-2s after wake
# while the network stack is still coming up, producing
# "[Errno 8] nodename nor servname provided" failures.
#
# Usage:
#   wait_and_run.sh /path/to/python /path/to/script.py [args...]
#
# Env overrides:
#   WAIT_HOST     hostname to probe (default: api.telegram.org)
#   WAIT_TIMEOUT  max seconds to wait (default: 600)
#   WAIT_INTERVAL poll interval (default: 5)
#   WAIT_PROBE    seconds per dig probe (default: 3)

set -u

HOST="${WAIT_HOST:-api.telegram.org}"
TIMEOUT="${WAIT_TIMEOUT:-600}"
INTERVAL="${WAIT_INTERVAL:-5}"
PROBE="${WAIT_PROBE:-3}"

ts() { date '+%Y-%m-%d %H:%M:%S %z'; }

start=$(date +%s)
attempt=0
while true; do
    attempt=$((attempt + 1))
    # Use `dig +time +tries` instead of `host -W` — on macOS, `host -W 2`
    # does NOT honor its timeout when the resolver itself is unreachable
    # (real incident 2026-05-16: a single host call blocked ~17 min). `dig`
    # has a hard per-query cap via +time + +tries.
    if /usr/bin/dig +time="$PROBE" +tries=1 +short "$HOST" 2>/dev/null | grep -q .; then
        echo "[$(ts)] [wait_and_run] DNS ready for $HOST after $attempt attempt(s)"
        break
    fi

    elapsed=$(($(date +%s) - start))
    if [ "$elapsed" -ge "$TIMEOUT" ]; then
        echo "[$(ts)] [wait_and_run] TIMEOUT after ${elapsed}s waiting for $HOST — giving up" >&2
        exit 75  # EX_TEMPFAIL
    fi

    sleep "$INTERVAL"
done

echo "[$(ts)] [wait_and_run] exec: $*"
exec "$@"
