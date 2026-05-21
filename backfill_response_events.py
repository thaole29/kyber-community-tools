"""
backfill_response_events.py

Rebuild `ticket_response_events` for every active Discord ticket channel by
replaying the message history. For each channel:
  * Drop any existing event rows for that ticket_id (idempotent re-run).
  * Walk messages oldest→newest, pair each user-msg-burst with the next
    agent reply, emit one event per pair (first reply → 'first',
    subsequent pairs → 'followup').

Tickets whose channel has been deleted from Discord cannot be recovered
from here — they need transcript parsing (see transcript_backfill.py).

Usage:
    venv/bin/python backfill_response_events.py [--limit N]
"""

import argparse
import asyncio
import re
import sys
from datetime import timezone

import discord

import config
import database

TOKEN = config.DISCORD_TOKEN
TICKET_CHANNEL_PREFIXES = config.TICKET_CHANNEL_PREFIXES
SUPPORT_ROLES = config.SUPPORT_ROLES

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
client = discord.Client(intents=intents)


def normalize_id(name):
    m = re.search(r'(\d+)', name)
    return f"ticket-{m.group(1)}" if m else name


async def _classify_member(guild, user_id, cache):
    if user_id in cache:
        return cache[user_id]
    try:
        member = await guild.fetch_member(user_id)
    except Exception:
        cache[user_id] = (False, None)
        return cache[user_id]
    role_names = [r.name for r in member.roles]
    is_agent = any(r in SUPPORT_ROLES for r in role_names)
    cache[user_id] = (is_agent, member.display_name)
    return cache[user_id]


async def replay_channel(channel, guild, history_limit=None):
    """Walk one ticket channel's history and return a list of
    (ticket_id, user_msg_at, agent_msg_at, agent_name, agent_user_id, event_type)
    tuples ready for database.insert_response_event.

    Returns None if read permission was denied.
    """
    tid = normalize_id(channel.name)
    channel_created = channel.created_at.replace(tzinfo=timezone.utc)
    events = []
    pending_user_at = None
    first_event_emitted = False
    role_cache = {}

    try:
        async for msg in channel.history(limit=history_limit, oldest_first=True):
            if msg.author.id == config.TICKET_TOOL_BOT_ID:
                continue
            if msg.author.bot:
                continue

            ts = msg.created_at.replace(tzinfo=timezone.utc)
            is_agent, display_name = await _classify_member(guild, msg.author.id, role_cache)

            if is_agent:
                agent_canonical = config.normalize_agent(display_name)
                if not first_event_emitted:
                    # First-response event: anchor user_msg_at to channel
                    # creation so the gap matches the FRT clock used in
                    # metrics.contributions_for_ticket.
                    events.append((tid, channel_created, ts, agent_canonical,
                                   str(msg.author.id), 'first'))
                    first_event_emitted = True
                    pending_user_at = None
                elif pending_user_at is not None:
                    events.append((tid, pending_user_at, ts, agent_canonical,
                                   str(msg.author.id), 'followup'))
                    pending_user_at = None
                # else: consecutive agent messages with no user wait between
                # them — not an event.
            else:
                if pending_user_at is None:
                    pending_user_at = ts
    except discord.Forbidden:
        return None

    return events


@client.event
async def on_ready():
    print(f'[READY] Connected as {client.user}', flush=True)
    database.init_db()

    args = client._cli_args
    total_channels = 0
    total_events = 0
    skipped = 0

    for guild in client.guilds:
        ticket_channels = [
            c for c in guild.text_channels
            if any(c.name.startswith(p) for p in TICKET_CHANNEL_PREFIXES)
        ]
        print(f'[{guild.name}] {len(ticket_channels)} ticket channels', flush=True)

        for ch in ticket_channels:
            tid = normalize_id(ch.name)
            events = await replay_channel(ch, guild, history_limit=args.limit)
            if events is None:
                print(f'  {tid}: no read permission, skipped', flush=True)
                skipped += 1
                continue

            database.delete_response_events_for_ticket(tid)
            for ev in events:
                database.insert_response_event(*ev)

            first_count = sum(1 for e in events if e[5] == 'first')
            fu_count = sum(1 for e in events if e[5] == 'followup')
            print(f'  {tid}: {len(events)} events ({first_count} first, '
                  f'{fu_count} followup)', flush=True)
            total_channels += 1
            total_events += len(events)

    print(f'\n[DONE] {total_channels} channels processed, '
          f'{total_events} events inserted, {skipped} skipped (no permission).',
          flush=True)
    await client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=None,
                        help='Max messages per channel (default: unbounded).')
    args = parser.parse_args()
    client._cli_args = args
    client.run(TOKEN)


if __name__ == '__main__':
    main()
