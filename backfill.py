"""
backfill.py

Scans all active ticket channels in the Discord server,
reads message history, and backfills ticket data into SQLite.
Computes shift assignments and on-duty flags retroactively.
"""

import discord
import asyncio
import os
import re
from datetime import datetime, timezone

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
    match = re.search(r'(\d+)', name)
    if match:
        return f"ticket-{match.group(1)}"
    return name


@client.event
async def on_ready():
    print(f'Backfill script connected as: {client.user}')
    print('Scanning all ticket channels for historical data...\n')

    database.init_db()
    updated = 0

    for guild in client.guilds:
        print(f'Server: {guild.name}')
        ticket_channels = [
            c for c in guild.text_channels
            if any(c.name.startswith(p) for p in TICKET_CHANNEL_PREFIXES)
        ]
        print(f'Found {len(ticket_channels)} ticket channel(s) to scan.\n')

        for channel in ticket_channels:
            tid = normalize_id(channel.name)

            # Skip if already has full data
            existing = database.get_ticket(tid)
            if (existing
                    and existing['first_responded_at'] is not None
                    and existing['ticket_owner'] is not None):
                print(f'  [SKIP] #{tid} already has full data.')
                continue

            print(f'  [SCANNING] #{tid}...')

            first_message_time = None
            first_agent_message = None
            owner_id = None

            try:
                async for msg in channel.history(limit=200, oldest_first=True):
                    if first_message_time is None:
                        first_message_time = msg.created_at.replace(tzinfo=timezone.utc)

                    # Extract owner from Ticket Tool welcome message
                    if msg.author.id == config.TICKET_TOOL_BOT_ID:
                        owner_match = re.search(r'Hi <@!?(\d+)>', msg.content)
                        if owner_match:
                            owner_id = owner_match.group(1)

                    if msg.author.bot:
                        continue

                    # Check if sender is a support agent
                    try:
                        member = await guild.fetch_member(msg.author.id)
                        role_names = [r.name for r in member.roles]
                        if any(r in SUPPORT_ROLES for r in role_names):
                            first_agent_message = msg
                            break
                    except Exception:
                        continue

            except discord.Forbidden:
                print(f'  [ERROR] No permission to read #{tid}')
                continue

            # Compute shift info from creation time
            creation_utc = first_message_time
            shift_label, on_duty_agent = config.get_on_duty_agent(creation_utc)

            # Prepare agent response data
            agent_name = None
            agent_user_id = None
            responded_at = None
            response_mins = None
            on_duty_responded = False
            cross_shift = False
            sla_breached = False

            if first_agent_message:
                responded_at = first_agent_message.created_at.replace(tzinfo=timezone.utc)
                agent_name = config.normalize_agent(first_agent_message.author.display_name)
                agent_user_id = str(first_agent_message.author.id)

                if creation_utc and responded_at:
                    diff = responded_at - creation_utc
                    response_mins = round(diff.total_seconds() / 60, 2)

                if on_duty_agent:
                    on_duty_responded = (agent_name == on_duty_agent)
                    cross_shift = (agent_name != on_duty_agent)

                if response_mins is not None:
                    sla_breached = response_mins > config.SLA_FRT_THRESHOLD_MINS

            data = {
                'ticket_id': tid,
                'created_at': creation_utc.isoformat() if creation_utc else None,
                'first_responded_at': responded_at.isoformat() if responded_at else None,
                'response_time_mins': response_mins,
                'agent_name': agent_name,
                'agent_user_id': agent_user_id,
                'ticket_owner': owner_id,
                'on_duty_agent_name': on_duty_agent,
                'on_duty_responded': on_duty_responded,
                'cross_shift_help': cross_shift,
                'sla_breached': sla_breached,
                'shift_label': shift_label,
            }

            database.upsert_ticket(data)

            if first_agent_message:
                status = ""
                if cross_shift:
                    status = f" (cross-shift, on-duty: {on_duty_agent})"
                print(f'  [FOUND] Agent: {agent_name} | FRT: {response_mins} min{status}')
                updated += 1
            else:
                print(f'  [NO AGENT REPLY YET] #{tid}')

            await asyncio.sleep(0.5)

    print(f'\nBackfill complete! Updated {updated} ticket(s).')
    await client.close()

client.run(TOKEN)
