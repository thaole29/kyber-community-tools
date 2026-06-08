"""Build / refresh the chatbot's semantic index from project data.

Reads (read-only) from tickets.db and writes embeddings into the separate
chatbot index DB. Run once after setup, then on a cron (e.g. after the daily
snapshot) to keep the index fresh.

    venv/bin/python -m chatbot.index_build            # build everything
    venv/bin/python -m chatbot.index_build --limit 50 # cap tickets (testing)

Idempotent: re-running upserts by (source_type, source_id).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import config  # noqa: E402
import database  # noqa: E402
from chatbot import embeddings  # noqa: E402


def _ticket_text(t):
    """Compose a compact, searchable text blob for one ticket."""
    parts = []
    if t.get("first_user_message"):
        parts.append(f"First message: {t['first_user_message']}")
    if t.get("conversation_excerpt"):
        parts.append(f"Conversation: {t['conversation_excerpt']}")
    meta = []
    if t.get("product_group"):
        meta.append(t["product_group"])
    if t.get("product_subcategory"):
        meta.append(t["product_subcategory"])
    if meta:
        parts.append("Category: " + " / ".join(meta))
    if t.get("agent_name"):
        parts.append(f"Handled by: {t['agent_name']}")
    if t.get("satisfaction_label"):
        parts.append(f"Satisfaction: {t['satisfaction_label']}")
    return "\n".join(parts).strip()


def _digest_text(d):
    """Flatten a community_digests row's parsed JSON into searchable prose.
    get_community_digests_in_range returns the JSON already parsed under 'digest'.
    """
    payload = d.get("digest")
    if not isinstance(payload, dict):
        return ""
    lines = [f"Channel {d['channel']} on {d['digest_date']}:"]
    themes = payload.get("themes") or []
    for th in themes:
        if isinstance(th, dict):
            title = th.get("title") or th.get("theme") or ""
            summ = th.get("summary") or th.get("description") or ""
            lines.append(f"- {title}: {summ}".strip())
        else:
            lines.append(f"- {th}")
    for key in ("sentiment", "summary", "highlights"):
        if payload.get(key):
            lines.append(f"{key}: {payload[key]}")
    return "\n".join(lines).strip()


def collect_ticket_chunks(limit=None):
    rows = []
    tickets = database.get_all_tickets()
    if limit:
        tickets = tickets[:limit]
    for t in tickets:
        t = dict(t)
        text = _ticket_text(t)
        if not text:
            continue
        rows.append({
            "source_type": "ticket",
            "source_id": t["ticket_id"],
            "title": t["ticket_id"],
            "text": text,
        })
    return rows


def collect_digest_chunks():
    rows = []
    digests = database.get_community_digests_in_range("0000-01-01", "9999-12-31")
    for d in digests:
        d = dict(d)
        text = _digest_text(d)
        if not text:
            continue
        rows.append({
            "source_type": "digest",
            "source_id": f"{d['digest_date']}::{d['channel']}",
            "title": f"{d['channel']} {d['digest_date']}",
            "text": text,
        })
    return rows


def main():
    import time

    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="cap tickets (testing)")
    ap.add_argument("--tickets-only", action="store_true")
    ap.add_argument("--digests-only", action="store_true")
    ap.add_argument("--rebuild", action="store_true",
                    help="re-embed everything (default skips already-indexed chunks)")
    ap.add_argument("--group", type=int, default=20,
                    help="chunks embedded per pause (Gemini free tier ~100/min)")
    ap.add_argument("--sleep", type=float, default=15.0,
                    help="seconds to pause between groups to respect the quota")
    args = ap.parse_args()

    if not config.GEMINI_API_KEY:
        print("[index] ERROR: GEMINI_API_KEY not set (needed for embeddings)", flush=True)
        return 1

    embeddings.init_index()
    rows = []
    if not args.digests_only:
        rows += collect_ticket_chunks(limit=args.limit)
    if not args.tickets_only:
        rows += collect_digest_chunks()

    if not args.rebuild:
        have = embeddings.existing_keys()
        before = len(rows)
        rows = [r for r in rows if (r["source_type"], r["source_id"]) not in have]
        print(f"[index] {before} candidate chunks, {before - len(rows)} already indexed, "
              f"{len(rows)} to embed", flush=True)

    # Paced groups so re-runs fill the index incrementally without tripping the
    # free-tier embedding quota; resumable since upsert is keyed and we skip
    # already-indexed chunks.
    total = 0
    for i in range(0, len(rows), args.group):
        group = rows[i:i + args.group]
        total += embeddings.upsert_chunks(group)
        print(f"[index] {total}/{len(rows)} embedded...", flush=True)
        if i + args.group < len(rows):
            time.sleep(args.sleep)

    stamp = datetime.now(tz=timezone.utc).isoformat()
    print(f"[index] done at {stamp}: {total} new chunks. stats={embeddings.stats()}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
