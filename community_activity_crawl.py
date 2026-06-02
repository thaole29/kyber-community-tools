"""
community_activity_crawl.py

Crawl messages from config.COMMUNITY_CHANNELS over the last N days and
write per-agent per-channel daily message counts into
`community_agent_activity_daily`. Idempotent: re-running for the same
window replaces existing rows for each (date, channel, agent).

Counts ALL non-bot agent messages — no LLM classification, no filtering.

Run:
    venv/bin/python community_activity_crawl.py --days 7

Suggested cron (after community_digest finishes):
    20 0 * * *  venv/bin/python community_activity_crawl.py --days 1
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import config  # noqa: E402
import database  # noqa: E402


def _upsert(rows):
    """rows: iterable of (activity_date, channel, agent_id, agent_name, msg_count).
    For each (date, channel, agent) we DELETE then INSERT to support both
    'fix a previously-wrong count' and 'first write'."""
    conn = sqlite3.connect(config.DB_FILE)
    try:
        keys = {(r[0], r[1], r[2]) for r in rows}
        for date, ch, aid in keys:
            conn.execute(
                "DELETE FROM community_agent_activity_daily "
                "WHERE activity_date = ? AND channel = ? AND agent_id = ?",
                (date, ch, aid),
            )
        conn.executemany(
            "INSERT INTO community_agent_activity_daily "
            "(activity_date, channel, agent_id, agent_name, msg_count) "
            "VALUES (?, ?, ?, ?, ?)",
            list(rows),
        )
        conn.commit()
    finally:
        conn.close()


def _clear_window(channels, dates):
    """Remove any existing rows for the (channel × date) cells we're about
    to re-crawl, so a re-run produces correct counts even if agents who
    posted on day-X stop posting (their old row should drop to 0/gone)."""
    if not channels or not dates:
        return
    conn = sqlite3.connect(config.DB_FILE)
    try:
        placeholders_c = ",".join("?" * len(channels))
        placeholders_d = ",".join("?" * len(dates))
        conn.execute(
            f"DELETE FROM community_agent_activity_daily "
            f"WHERE channel IN ({placeholders_c}) "
            f"AND activity_date IN ({placeholders_d})",
            list(channels) + list(dates),
        )
        conn.commit()
    finally:
        conn.close()


async def _scan(channel, since_utc, agent_ids):
    counts = defaultdict(int)  # (date_str, agent_id) → count
    names = {}
    async for m in channel.history(limit=None, after=since_utc, oldest_first=True):
        if m.author.bot:
            continue
        aid = str(m.author.id)
        if aid not in agent_ids:
            continue
        ts = m.created_at.replace(tzinfo=timezone.utc) if m.created_at.tzinfo is None \
            else m.created_at.astimezone(timezone.utc)
        date_str = ts.strftime("%Y-%m-%d")
        counts[(date_str, aid)] += 1
        names[aid] = config.get_agent_name_by_id(aid) or aid
    return counts, names


async def main(days: int) -> int:
    if not config.DISCORD_TOKEN:
        print("DISCORD_TOKEN not set", file=sys.stderr)
        return 1

    until_utc = datetime.now(timezone.utc)
    since_utc = (until_utc - timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    agent_ids = set(config.AGENT_DISCORD_IDS.keys())
    print(f"[crawl] window {since_utc.isoformat()} → {until_utc.isoformat()}")

    # Pre-compute the set of UTC dates we're (re-)writing — used to clear
    # stale rows before insert.
    dates_in_window = set()
    d = since_utc.date()
    while d <= until_utc.date():
        dates_in_window.add(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    rows: list[tuple] = []
    scanned_channels: list[str] = []

    @client.event
    async def on_ready():
        try:
            print(f"[crawl] connected as {client.user}", flush=True)
            wanted = set(config.COMMUNITY_CHANNELS)
            found = {}
            for g in client.guilds:
                for ch in g.text_channels:
                    if ch.name in wanted:
                        found[ch.name] = ch
            missing = wanted - set(found)
            if missing:
                print(f"[crawl] WARN missing channels: {sorted(missing)}",
                      flush=True)
            for name in config.COMMUNITY_CHANNELS:
                if name not in found:
                    continue
                scanned_channels.append(name)
                counts, names = await _scan(found[name], since_utc, agent_ids)
                total = sum(counts.values())
                print(f"[crawl] #{name}: {total} agent msgs across "
                      f"{len({d for d, _ in counts})} day(s)", flush=True)
                for (date_str, aid), n in counts.items():
                    rows.append((date_str, name, aid, names[aid], n))
        finally:
            await client.close()

    await client.start(config.DISCORD_TOKEN)

    if not scanned_channels:
        print("[crawl] no channels scanned — nothing to write")
        return 0

    _clear_window(scanned_channels, dates_in_window)
    if rows:
        _upsert(rows)
    print(f"[crawl] wrote {len(rows)} rows across "
          f"{len(scanned_channels)} channels × {len(dates_in_window)} dates")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.days)))
