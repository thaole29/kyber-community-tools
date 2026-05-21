"""
backfill_response_events_from_transcripts.py

Recover ticket_response_events for closed tickets whose Discord channels
have been deleted by Ticket Tool, by parsing the HTML transcripts archived
in the #archieved channel.

Mirrors the live event-pairing logic (record_response in database.py):
  * Walk messages chronologically.
  * Maintain a sticky `pending_user_msg_at` set on the first user message
    of the current wait cycle.
  * When the next agent message arrives, emit one event (first or followup)
    closing that pending wait.

Anchors the FIRST event's user_msg_at to the ticket's stored `created_at`
(matches the FRT clock used in metrics.contributions_for_ticket).

Usage:
    venv/bin/python backfill_response_events_from_transcripts.py [--days 30]
"""

import argparse
import asyncio
import base64
import json
import re
from datetime import datetime, timedelta, timezone

import discord

import config
import database

ARCHIEVED_CHANNEL_ID = 1192426280436650085  # spelled per Discord channel name
TRANSCRIPT_FILE_RE = re.compile(
    r"transcript-(?:closed|ticket)-(\d+)\.html$", re.IGNORECASE
)
MESSAGES_VAR_RE = re.compile(r'let messages\s*=\s*"([A-Za-z0-9+/=]+)"')


def parse_transcript_html(html_bytes: bytes) -> list[dict]:
    text = html_bytes.decode("utf-8", errors="replace")
    m = MESSAGES_VAR_RE.search(text)
    if not m:
        return []
    try:
        decoded = base64.b64decode(m.group(1)).decode("utf-8", errors="replace")
        return json.loads(decoded)
    except Exception:
        return []


def build_events_from_messages(ticket_id: str, channel_created: datetime,
                               messages: list[dict]) -> list[tuple]:
    """Pair user→agent message gaps and return event tuples in the order
    expected by database.insert_response_event."""
    agent_ids = set(config.AGENT_DISCORD_IDS.keys())
    events = []
    pending_user_at = None
    first_event_emitted = False

    for m in sorted(messages, key=lambda x: x.get("created", 0)):
        if m.get("bot"):
            continue
        uid = str(m.get("user_id") or "")
        if not uid:
            continue
        created_ms = m.get("created")
        if not created_ms:
            continue
        ts = datetime.fromtimestamp(created_ms / 1000.0, tz=timezone.utc)

        if uid in agent_ids:
            agent_name = config.AGENT_DISCORD_IDS[uid]
            if not first_event_emitted:
                events.append(
                    (ticket_id, channel_created, ts, agent_name, uid, 'first')
                )
                first_event_emitted = True
                pending_user_at = None
            elif pending_user_at is not None:
                events.append(
                    (ticket_id, pending_user_at, ts, agent_name, uid, 'followup')
                )
                pending_user_at = None
        else:
            if pending_user_at is None:
                pending_user_at = ts

    return events


async def run(days: int):
    if not config.DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN not set")

    database.init_db()

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)

    # Build target ticket_id → channel_created lookup, scoped to recent window.
    targets: dict[str, datetime] = {}
    for t in database.get_all_tickets():
        ca = t.get("created_at")
        if not ca:
            continue
        ca_dt = datetime.fromisoformat(ca)
        if ca_dt.tzinfo is None:
            ca_dt = ca_dt.replace(tzinfo=timezone.utc)
        if ca_dt < cutoff:
            continue
        targets[t["ticket_id"]] = ca_dt

    print(f"[INIT] {len(targets)} tickets in last {days} days targeted "
          f"(cutoff {cutoff.isoformat()})", flush=True)

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    stats = {
        "transcripts_scanned": 0,
        "tickets_matched": 0,
        "events_inserted": 0,
        "skipped_no_messages": 0,
        "skipped_parse_fail": 0,
        "tickets_handled": set(),
    }

    @client.event
    async def on_ready():
        print(f"[READY] Logged in as {client.user}", flush=True)
        channel = client.get_channel(ARCHIEVED_CHANNEL_ID) \
            or await client.fetch_channel(ARCHIEVED_CHANNEL_ID)
        print(f"[SCAN] iterating #{channel.name} newest-first…", flush=True)

        # Transcripts are uploaded around ticket-close time. Walk back ~60d
        # to comfortably cover 30d-old tickets that closed late.
        scan_cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days + 30)

        try:
            async for msg in channel.history(limit=None, oldest_first=False):
                msg_ts = msg.created_at.replace(tzinfo=timezone.utc)
                if msg_ts < scan_cutoff:
                    print(f"[SCAN] reached {msg_ts.date()}, stopping.", flush=True)
                    break
                if len(stats["tickets_handled"]) >= len(targets):
                    print("[SCAN] all targets handled, stopping.", flush=True)
                    break
                if not msg.attachments:
                    continue
                for att in msg.attachments:
                    fname_match = TRANSCRIPT_FILE_RE.search(att.filename or "")
                    if not fname_match:
                        continue
                    stats["transcripts_scanned"] += 1
                    n = int(fname_match.group(1))
                    candidates = {f"ticket-{n}", f"ticket-{n:04d}"}
                    matched_tid = next(
                        (c for c in candidates if c in targets), None
                    )
                    if matched_tid is None:
                        continue
                    if matched_tid in stats["tickets_handled"]:
                        continue
                    stats["tickets_matched"] += 1

                    try:
                        html_bytes = await att.read()
                    except Exception as e:
                        print(f"  {matched_tid}: download failed: {e}", flush=True)
                        stats["skipped_parse_fail"] += 1
                        continue
                    messages = parse_transcript_html(html_bytes)
                    if not messages:
                        stats["skipped_parse_fail"] += 1
                        continue

                    events = build_events_from_messages(
                        matched_tid, targets[matched_tid], messages
                    )
                    if not events:
                        stats["skipped_no_messages"] += 1
                        stats["tickets_handled"].add(matched_tid)
                        print(f"  {matched_tid}: 0 events (no agent reply)", flush=True)
                        continue

                    database.delete_response_events_for_ticket(matched_tid)
                    for ev in events:
                        database.insert_response_event(*ev)
                    stats["events_inserted"] += len(events)
                    stats["tickets_handled"].add(matched_tid)

                    first_n = sum(1 for e in events if e[5] == 'first')
                    fu_n = sum(1 for e in events if e[5] == 'followup')
                    print(f"  {matched_tid}: {len(events)} events "
                          f"({first_n} first, {fu_n} followup)", flush=True)
        finally:
            print(
                f"\n[DONE] scanned={stats['transcripts_scanned']} "
                f"matched={stats['tickets_matched']} "
                f"handled={len(stats['tickets_handled'])}/{len(targets)} "
                f"events_inserted={stats['events_inserted']} "
                f"parse_fail={stats['skipped_parse_fail']} "
                f"no_reply={stats['skipped_no_messages']}",
                flush=True,
            )
            missing = sorted(set(targets) - stats["tickets_handled"])
            if missing:
                print(f"[INFO] {len(missing)} target tickets had no transcript "
                      f"(channel still active or transcript not yet archived): "
                      f"{', '.join(missing[:15])}"
                      f"{'…' if len(missing) > 15 else ''}", flush=True)
            await client.close()

    await client.start(config.DISCORD_TOKEN)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30,
                        help="Backfill tickets created within the last N days "
                             "(default 30).")
    args = parser.parse_args()
    asyncio.run(run(args.days))


if __name__ == "__main__":
    main()
