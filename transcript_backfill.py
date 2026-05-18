"""
transcript_backfill.py

One-shot backfill of historical ticket text from Ticket Tool's HTML
transcripts archived in the #archieved channel.

Ticket Tool exports each closed ticket as `transcript-{closed|ticket}-NNNN.html`,
where the body contains a `messages = "<base64-encoded JSON>"` script var
with the full conversation (username, user_id, bot, content, created, ...).

For each archived transcript whose ticket_id already exists in our DB but
lacks `first_user_message` / `conversation_excerpt`, this script:
  1. Downloads the HTML attachment.
  2. Decodes the base64 JSON.
  3. Splits messages into user (non-agent) vs agent using config.AGENT_DISCORD_IDS.
  4. Writes back `first_user_message` (first non-bot, non-agent message) and
     `conversation_excerpt` (concat of up to 5 user messages, cap 2KB).

Idempotent: re-runs only touch fields that are still NULL/empty.

Usage:
    venv/bin/python transcript_backfill.py            # process all matching
    venv/bin/python transcript_backfill.py --dry      # don't write to DB
    venv/bin/python transcript_backfill.py --limit 20 # cap processed count
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import sys
from pathlib import Path

import discord

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import config  # noqa: E402
import database  # noqa: E402


ARCHIEVED_CHANNEL_ID = 1192426280436650085  # spelled per Discord channel name
TRANSCRIPT_FILE_RE = re.compile(r"transcript-(?:closed|ticket)-(\d+)\.html$", re.IGNORECASE)
MESSAGES_VAR_RE = re.compile(r'let messages\s*=\s*"([A-Za-z0-9+/=]+)"')


def parse_transcript_html(html_bytes: bytes) -> list[dict]:
    """Decode Ticket Tool's transcript HTML → list of message dicts."""
    text = html_bytes.decode("utf-8", errors="replace")
    m = MESSAGES_VAR_RE.search(text)
    if not m:
        return []
    try:
        decoded = base64.b64decode(m.group(1)).decode("utf-8", errors="replace")
        return json.loads(decoded)
    except Exception:
        return []


def split_user_vs_agent(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (user_messages, agent_messages), both sorted by created asc.
    Excludes bots. Agents are detected by user_id ∈ config.AGENT_DISCORD_IDS."""
    agent_ids = set(config.AGENT_DISCORD_IDS.keys())
    user_msgs = []
    agent_msgs = []
    for m in messages:
        if m.get("bot"):
            continue
        if not (m.get("content") or "").strip():
            continue
        uid = str(m.get("user_id") or "")
        if uid in agent_ids:
            agent_msgs.append(m)
        else:
            user_msgs.append(m)
    user_msgs.sort(key=lambda x: x.get("created", 0))
    agent_msgs.sort(key=lambda x: x.get("created", 0))
    return user_msgs, agent_msgs


def build_excerpt(user_msgs: list[dict], n: int = 5, cap: int = 2000) -> str:
    pieces = [(m.get("content") or "").strip() for m in user_msgs[:n]]
    pieces = [p for p in pieces if p]
    return " | ".join(pieces)[:cap]


async def backfill(limit: int | None = None, dry: bool = False) -> dict:
    """Pull transcripts and enrich existing DB rows. Returns stats dict."""
    if not config.DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN not set")

    intents = discord.Intents.default()
    intents.message_content = True  # required to read attachment URLs
    client = discord.Client(intents=intents)

    stats = {
        "transcripts_seen": 0,
        "tickets_in_db_match": 0,
        "enriched_first_msg": 0,
        "enriched_excerpt": 0,
        "skipped_already_full": 0,
        "skipped_not_in_db": 0,
        "skipped_no_user_msg": 0,
        "parse_failures": 0,
    }

    # Pre-load the set of ticket_ids in our DB so we can quickly filter
    # transcripts we DON'T care about (saves bandwidth on the ~3800 not in DB).
    db_tickets = set()
    db_state: dict[str, dict] = {}
    for t in database.get_all_tickets():
        db_tickets.add(t["ticket_id"])
        db_state[t["ticket_id"]] = {
            "first_user_message": t.get("first_user_message"),
            "conversation_excerpt": t.get("conversation_excerpt"),
        }

    @client.event
    async def on_ready():
        print(f"Logged in as: {client.user}", flush=True)
        try:
            channel = client.get_channel(ARCHIEVED_CHANNEL_ID)
            if channel is None:
                channel = await client.fetch_channel(ARCHIEVED_CHANNEL_ID)
            print(f"Iterating #{channel.name} for transcripts…", flush=True)

            processed = 0
            async for msg in channel.history(limit=None, oldest_first=False):
                if not msg.attachments:
                    continue
                for att in msg.attachments:
                    fname_match = TRANSCRIPT_FILE_RE.search(att.filename or "")
                    if not fname_match:
                        continue
                    stats["transcripts_seen"] += 1
                    n = int(fname_match.group(1))
                    tid = f"ticket-{n:04d}" if n < 1000 else f"ticket-{n}"
                    # Try both formatted and unpadded — DB has mixed historical
                    candidates = {f"ticket-{n}", f"ticket-{n:04d}"}
                    matched_tid = next((c for c in candidates if c in db_tickets), None)
                    if matched_tid is None:
                        stats["skipped_not_in_db"] += 1
                        continue
                    stats["tickets_in_db_match"] += 1
                    cur = db_state[matched_tid]
                    needs_first = not (cur["first_user_message"] or "").strip()
                    needs_excerpt = not (cur["conversation_excerpt"] or "").strip()
                    if not needs_first and not needs_excerpt:
                        stats["skipped_already_full"] += 1
                        continue

                    try:
                        html_bytes = await att.read()
                    except Exception as e:
                        print(f"  [{matched_tid}] download failed: {e}", flush=True)
                        stats["parse_failures"] += 1
                        continue
                    msgs = parse_transcript_html(html_bytes)
                    if not msgs:
                        stats["parse_failures"] += 1
                        continue
                    user_msgs, _ = split_user_vs_agent(msgs)
                    if not user_msgs:
                        stats["skipped_no_user_msg"] += 1
                        continue

                    first_text = (user_msgs[0].get("content") or "").strip()
                    excerpt = build_excerpt(user_msgs)
                    if dry:
                        print(f"  [DRY {matched_tid}] first={first_text[:60]!r} "
                              f"excerpt_len={len(excerpt)}", flush=True)
                    else:
                        conn = database.get_connection()
                        try:
                            updates = []
                            values = []
                            if needs_first and first_text:
                                updates.append("first_user_message = ?")
                                values.append(first_text)
                                stats["enriched_first_msg"] += 1
                            if needs_excerpt and excerpt:
                                updates.append("conversation_excerpt = ?")
                                values.append(excerpt)
                                stats["enriched_excerpt"] += 1
                            if updates:
                                values.append(matched_tid)
                                conn.execute(
                                    f"UPDATE tickets SET {', '.join(updates)} WHERE ticket_id = ?",
                                    values,
                                )
                                conn.commit()
                        finally:
                            conn.close()
                        # Update in-memory state so duplicate transcripts skip
                        if needs_first and first_text:
                            db_state[matched_tid]["first_user_message"] = first_text
                        if needs_excerpt and excerpt:
                            db_state[matched_tid]["conversation_excerpt"] = excerpt
                        print(f"  [{matched_tid}] enriched", flush=True)

                    processed += 1
                    if limit and processed >= limit:
                        print(f"\nReached --limit {limit}, stopping.", flush=True)
                        await client.close()
                        return
        finally:
            await client.close()

    await client.start(config.DISCORD_TOKEN)
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="parse but don't write DB")
    ap.add_argument("--limit", type=int, default=None, help="cap processed count")
    args = ap.parse_args()
    stats = asyncio.run(backfill(limit=args.limit, dry=args.dry))
    print("\n=== STATS ===")
    for k, v in stats.items():
        print(f"  {k:24s} {v}")


if __name__ == "__main__":
    main()
