#!/bin/bash
# scripts/refresh_snapshot.sh
#
# Daily snapshot pipeline (Option B1):
#   1. Generate fresh JSON snapshots from SQLite.
#   2. git add → commit → push to main.
#   3. GitHub Actions (deploy-pages.yml) auto-builds + deploys to Pages.
#
# Invoked by launchd at 09:00 UTC+7 daily via wait_and_run.sh, which probes
# api.github.com DNS first so the push doesn't fail on wake-from-sleep.
#
# Marker file (for the safety_net_loop): touches
#   logs/.markers/dashboard_snapshot.success.<UTC date>
# on success, so bot.py's safety net can rerun this script if today's
# marker is missing past 04:00 UTC.

set -u
set -o pipefail

PROJECT_DIR="/Volumes/Macintosh HD - Data/Project"
cd "$PROJECT_DIR" || exit 1

ts() { date '+%Y-%m-%d %H:%M:%S %z'; }
log() { echo "[$(ts)] [refresh_snapshot] $*"; }

# 1. Generate snapshots
log "generating JSON snapshots..."
if ! "$PROJECT_DIR/venv/bin/python" "$PROJECT_DIR/scripts/generate_snapshot.py"; then
    log "ERROR: snapshot generation failed"
    exit 1
fi

# 2. Stage + check if anything changed
git add "$PROJECT_DIR/dashboard/web/public/data/" 2>&1

if git diff --cached --quiet; then
    log "no changes in snapshots — nothing to commit"
else
    log "snapshot changed, committing + pushing..."
    git commit -m "chore: refresh dashboard snapshot $(date -u +%Y-%m-%d)" \
        --author "dashboard-bot <dashboard@kyber-community-tools>" 2>&1
    if ! git push origin main 2>&1; then
        log "ERROR: git push failed"
        exit 1
    fi
fi

# 3. Touch success marker (idempotency for safety_net_loop)
MARKER_DIR="$PROJECT_DIR/logs/.markers"
mkdir -p "$MARKER_DIR"
TODAY=$(date -u '+%Y-%m-%d')
touch "$MARKER_DIR/dashboard_snapshot.success.$TODAY"

log "done"
