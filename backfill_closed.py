"""
backfill_closed.py

Scans the transcript archive channel for closed ticket transcripts,
parses HTML transcript files, and backfills ticket data into SQLite.
Computes shift assignments and on-duty flags retroactively.
"""

import discord
import aiohttp
import asyncio
import os
import re
import json
import base64
from datetime import datetime, timezone, timedelta

import config
import database

TOKEN = config.DISCORD_TOKEN
TRANSCRIPT_CHANNEL_NAME = 'archieved'
SUPPORT_ROLES = config.SUPPORT_ROLES

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
client = discord.Client(intents=intents)


def decode_transcript(html):
    """Parse a Ticket Tool HTML transcript into a list of message dicts."""
    match = re.search(r'messages\s*=\s*["\'](.+?)["\'];', html, re.DOTALL)
    if not match:
        return []

    try:
        data = json.loads(base64.b64decode(match.group(1)).decode('utf-8'))
        parsed = []
        for msg in data:
            ts_val = msg.get('created') or msg.get('timestamp')
            if not ts_val:
                continue

            ts = datetime.fromtimestamp(ts_val / 1000, tz=timezone.utc)
            author_id = str(msg.get('user_id'))
            author_name = msg.get('nick') or msg.get('username')
            embeds = msg.get('embeds', [])
            content = msg.get('content', '')

            parsed.append({
                'id': author_id,
                'name': author_name,
                'ts': ts,
                'content': content,
                'embeds': embeds,
            })
        return sorted(parsed, key=lambda x: x['ts'])
    except Exception:
        return []


@client.event
async def on_ready():
    print(f'Bot connected as {client.user}')
    database.init_db()
    updated = 0

    guild = client.get_guild(config.GUILD_ID)
    ch = discord.utils.get(guild.text_channels, name=TRANSCRIPT_CHANNEL_NAME)

    if not ch:
        print(f'❌ Channel #{TRANSCRIPT_CHANNEL_NAME} not found.')
        await client.close()
        return

    print(f'Checking #{ch.name}...')
    known_agents = set(config.AGENT_DISCORD_IDS.keys())

    async with aiohttp.ClientSession() as session:
        async for msg in ch.history(limit=200):
            for att in msg.attachments:
                if not att.filename.endswith('.html'):
                    continue

                match = re.search(r'(\d+)', att.filename)
                if not match:
                    continue
                tid = f"ticket-{match.group(1)}"

                try:
                    async with session.get(att.url) as r:
                        msgs = decode_transcript(await r.text())

                    if not msgs:
                        continue

                    created = msgs[0]['ts']

                    # Find first agent response
                    agent = None
                    for m in msgs:
                        if m['id'] in [str(client.user.id), str(config.TICKET_TOOL_BOT_ID)]:
                            continue

                        if m['id'] in known_agents:
                            agent = m
                            break
                        try:
                            member = await guild.fetch_member(int(m['id']))
                            if any(r.name in SUPPORT_ROLES for r in member.roles):
                                agent = m
                                known_agents.add(m['id'])
                                break
                        except Exception:
                            pass

                    # Identify owner
                    owner_id = None
                    if msgs:
                        first_msg = msgs[0]
                        if first_msg['id'] == str(config.TICKET_TOOL_BOT_ID):
                            owner_match = re.search(r'Hi <@!?(\d+)>', first_msg['content'] or '')
                            if owner_match:
                                owner_id = owner_match.group(1)

                    # Identify closer
                    closer_id = None
                    closer_name = None
                    is_closer_agent = None
                    for m in msgs:
                        for e in m.get('embeds', []):
                            desc = e.get('description', '')
                            if desc and "Ticket Closed by" in desc:
                                closer_match = re.search(r'<@!?(\d+)>', desc)
                                if closer_match:
                                    closer_id = closer_match.group(1)
                                    is_closer_agent = (closer_id in known_agents)

                                    for msg_search in msgs:
                                        if msg_search['id'] == closer_id:
                                            closer_name = msg_search['name']
                                            break

                                    if not is_closer_agent:
                                        try:
                                            member = await guild.fetch_member(int(closer_id))
                                            if any(r.name in SUPPORT_ROLES for r in member.roles):
                                                is_closer_agent = True
                                                closer_name = member.display_name
                                                known_agents.add(closer_id)
                                        except Exception:
                                            pass
                                    break
                        if closer_id:
                            break

                    # Compute values
                    closer_val = config.normalize_agent(closer_name) if (is_closer_agent and closer_name) else closer_id
                    agent_name = config.normalize_agent(agent['name']) if agent else None
                    resp_mins = round((agent['ts'] - created).total_seconds() / 60, 2) if agent else None

                    # Compute shift info
                    shift_label, on_duty_agent = config.get_on_duty_agent(created)
                    on_duty_responded = False
                    cross_shift = False
                    sla_breached = False

                    if agent_name and on_duty_agent:
                        on_duty_responded = (agent_name == on_duty_agent)
                        cross_shift = (agent_name != on_duty_agent)

                    if resp_mins is not None:
                        sla_breached = resp_mins > config.SLA_FRT_THRESHOLD_MINS

                    data = {
                        'ticket_id': tid,
                        'created_at': created.isoformat(),
                        'first_responded_at': agent['ts'].isoformat() if agent else None,
                        'response_time_mins': resp_mins,
                        'agent_name': agent_name,
                        'agent_user_id': agent['id'] if agent else None,
                        'ticket_owner': owner_id,
                        'closed_by': closer_val,
                        'closed_by_agent': is_closer_agent,
                        'closed_at': msg.created_at.isoformat(),
                        'on_duty_agent_name': on_duty_agent,
                        'on_duty_responded': on_duty_responded,
                        'cross_shift_help': cross_shift,
                        'sla_breached': sla_breached,
                        'shift_label': shift_label,
                    }

                    database.upsert_ticket(data)

                    print(f'  [SUCCESS] {tid} (Agent: {agent_name})')
                    updated += 1
                except Exception:
                    pass  # Silently continue for robustness in bulk

    print(f'Finished. Updated {updated} tickets.')
    await client.close()

client.run(TOKEN)
