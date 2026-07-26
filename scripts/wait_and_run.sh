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

# One DNS probe, bounded by a hard wall-clock watchdog.
#
# `dig +time +tries` replaced `host -W`, which on macOS ignores its own
# timeout when the resolver is unreachable (incident 2026-05-16: one call
# blocked ~17 min). But +time only bounds how long dig waits for a DNS
# *response* — it does not bound the call itself: on 2026-07-25 a
# `dig +time=3 +tries=1` sat in state S for 20 HOURS after a wake-from-sleep,
# wedging this wrapper and taking bot.py down with it (ticket-2200 went
# unrecorded). So we run dig in the background and SIGKILL it if it outlives
# the probe budget, guaranteeing every attempt terminates.
probe_dns() {
    local out pid killer rc
    out="$(mktemp -t wait_and_run)" || return 1
    /usr/bin/dig +time="$PROBE" +tries=1 +short "$HOST" >"$out" 2>/dev/null &
    pid=$!
    ( sleep "$((PROBE + 2))"; kill -9 "$pid" 2>/dev/null ) &
    killer=$!
    wait "$pid" 2>/dev/null
    rc=$?
    kill "$killer" 2>/dev/null
    wait "$killer" 2>/dev/null
    if [ "$rc" -ne 0 ]; then
        rm -f "$out"
        return 1
    fi
    # A resolver can answer "no records" successfully — require actual output.
    if grep -q . "$out"; then
        rm -f "$out"
        return 0
    fi
    rm -f "$out"
    return 1
}

start=$(date +%s)
attempt=0
while true; do
    attempt=$((attempt + 1))
    if probe_dns; then
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
