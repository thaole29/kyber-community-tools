"""
bot.py

Discord bot for tracking support ticket lifecycle events.
Uses SQLite (via database.py) for storage and config.py for all settings.

Tracks:
  - Ticket creation (channel create)
  - First agent response (message from Community Admin)
  - Ticket closure (channel moved to closed category)
  - Ticket deletion (channel deleted)
  - Shift-based on-duty agent assignment
  - SLA breach detection with Telegram alerts
"""

import discord
import os
import re
import requests
from datetime import datetime, timezone, timedelta
from discord.ext import tasks

import config
import database

# --- CONFIGURATION ---
TOKEN = config.DISCORD_TOKEN
TICKET_CHANNEL_PREFIXES = config.TICKET_CHANNEL_PREFIXES
SUPPORT_ROLES = config.SUPPORT_ROLES
CLOSED_CATEGORY_NAMES = config.CLOSED_CATEGORY_NAMES
# ---------------------

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

client = discord.Client(intents=intents)


def normalize_id(name):
    """Extract ticket number and return canonical ticket-NNNN ID."""
    match = re.search(r'(\d+)', name)
    if match:
        return f"ticket-{match.group(1)}"
    return name


# =====================================================
# BOT EVENTS
# =====================================================

@client.event
async def on_ready():
    database.init_db()
    print(f'Analytics Bot started successfully as: {client.user}')
    # Start the SLA monitoring loop
    if not sla_check_loop.is_running():
        sla_check_loop.start()
    print(f'SLA monitoring loop started (every 5 minutes)')


@client.event
async def on_guild_channel_create(channel):
    """Track new ticket channel creation."""
    if not isinstance(channel, discord.TextChannel):
        return
    if not any(channel.name.startswith(p) for p in TICKET_CHANNEL_PREFIXES):
        return

    tid = normalize_id(channel.name)
    created_at = datetime.now(tz=timezone.utc)

    database.create_ticket(tid, created_at)

    shift_label, on_duty = config.get_on_duty_agent(created_at)
    print(f'[NEW TICKET] {tid} at {created_at.strftime("%H:%M:%S")} UTC '
          f'| On-duty: {on_duty} (Shift {shift_label})')


@client.event
async def on_guild_channel_update(before, after):
    """
    Detect when Ticket Tool 'closes' a ticket by moving it
    to a closed category — BEFORE deleting it.
    """
    if not isinstance(after, discord.TextChannel):
        return
    if not any(after.name.startswith(p) for p in TICKET_CHANNEL_PREFIXES):
        return

    before_cat = before.category.name.lower() if before.category else ''
    after_cat = after.category.name.lower() if after.category else ''

    moved_to_closed = (
        after_cat in CLOSED_CATEGORY_NAMES and
        before_cat not in CLOSED_CATEGORY_NAMES
    )

    if moved_to_closed:
        tid = normalize_id(after.name)
        closed_at = datetime.now(tz=timezone.utc)

        existing = database.get_ticket(tid)
        if existing:
            database.close_ticket(tid, closed_at)
            print(f'[CLOSED] {tid} at {closed_at.strftime("%H:%M:%S")} UTC '
                  f'(Category: {after_cat})')
        else:
            database.close_ticket(tid, closed_at)
            print(f'[CLOSED-NEW] Pre-existing ticket closed: {tid}')


@client.event
async def on_message(message):
    """Track agent responses and ticket metadata from Ticket Tool messages."""
    if message.author.bot and message.author.id != config.TICKET_TOOL_BOT_ID:
        return  # Ignore other bots, but process Ticket Tool messages

    if not isinstance(message.channel, discord.TextChannel):
        return
    if not any(message.channel.name.startswith(p) for p in TICKET_CHANNEL_PREFIXES):
        return

    tid = normalize_id(message.channel.name)

    # --- Handle Ticket Tool messages ---
    if message.author.id == config.TICKET_TOOL_BOT_ID:
        # 1. Capture Ticket Owner from welcome message
        owner_match = re.search(r'Hi <@!?(\d+)>', message.content)
        if owner_match:
            owner_id = owner_match.group(1)
            database.set_ticket_owner(tid, owner_id)
            print(f'[OWNER] Identified owner for #{tid}: {owner_id}')

        # 2. Capture Ticket Closer from closing embed
        for embed in message.embeds:
            if embed.description and "Ticket Closed by" in embed.description:
                closer_match = re.search(r'<@!?(\d+)>', embed.description)
                if closer_match:
                    closer_id = closer_match.group(1)
                    closed_at = datetime.now(tz=timezone.utc)

                    # Determine if closer is an agent
                    closer_val = str(closer_id)
                    is_closer_agent = False
                    try:
                        closer_member = await message.guild.fetch_member(int(closer_id))
                        is_closer_agent = any(r.name in SUPPORT_ROLES for r in closer_member.roles)
                        if is_closer_agent:
                            closer_val = config.normalize_agent(closer_member.display_name)
                    except Exception:
                        pass

                    database.close_ticket(tid, closed_at, closer_val, is_closer_agent)
                    print(f'[CLOSER] #{tid} closed by {closer_val} (Agent: {is_closer_agent})')
        return  # Don't process Ticket Tool as an agent response

    # --- Handle human messages ---
    try:
        member = await message.guild.fetch_member(message.author.id)
        role_names = [role.name for role in member.roles]
    except Exception as e:
        print(f'[WARN] Could not fetch member roles for {message.author}: {e}')
        return

    is_agent = any(role in SUPPORT_ROLES for role in role_names)
    print(f'[MSG] {message.author.name} (ID:{message.author.id}) '
          f'in #{message.channel.name} | Agent: {is_agent}')

    if is_agent:
        responded_at = datetime.now(tz=timezone.utc)
        agent_name = config.normalize_agent(member.display_name)

        recorded = database.record_response(
            tid, agent_name, str(message.author.id), responded_at
        )

        if recorded:
            ticket = database.get_ticket(tid)
            rt = ticket['response_time_mins'] if ticket else None
            on_duty = ticket['on_duty_agent_name'] if ticket else 'Unknown'
            cross = ticket['cross_shift_help'] if ticket else False

            status = ""
            if cross:
                status = f" ⚠️ Cross-shift (on-duty: {on_duty})"

            print(f'[RESPONSE] {agent_name} responded in {rt} mins '
                  f'in #{tid}{status}')
    else:
        # Non-agent human message in a ticket channel — capture as the
        # ticket owner's issue description for SLA alerts (idempotent).
        if message.content:
            saved = database.set_first_user_message(tid, message.content)
            if saved:
                print(f'[USER MSG] Captured first user message for #{tid}')

        # Refresh last_user_msg_at and clear sla_alert_sent so the SLA
        # loop re-evaluates the ticket from this new activity timestamp.
        # No-op if ticket already has an agent response or is closed.
        touched = database.touch_user_msg(tid, datetime.now(tz=timezone.utc))
        if touched:
            print(f'[USER MSG] Touched #{tid} for SLA re-check')


@client.event
async def on_guild_channel_delete(channel):
    """Track ticket channel deletion."""
    if not isinstance(channel, discord.TextChannel):
        return
    if not any(channel.name.startswith(p) for p in TICKET_CHANNEL_PREFIXES):
        return

    tid = normalize_id(channel.name)
    deleted_at = datetime.now(tz=timezone.utc)

    database.mark_deleted(tid, deleted_at)
    print(f'[DELETED] {tid} at {deleted_at.strftime("%H:%M:%S")} UTC')


# =====================================================
# SLA MONITORING LOOP (every 5 minutes)
# =====================================================

@tasks.loop(minutes=5)
async def sla_check_loop():
    """Check for tickets that have breached the FRT SLA and send Telegram alerts."""
    now = datetime.now(tz=timezone.utc)
    open_tickets = database.get_open_tickets_needing_alert()

    for ticket in open_tickets:
        created_str = ticket.get('created_at')
        if not created_str:
            continue

        # Wait time is measured from the most recent user activity:
        # last_user_msg_at if present, else fall back to created_at.
        # This means an old open ticket only re-alerts after a NEW user message.
        last_msg_str = ticket.get('last_user_msg_at')
        ref_str = last_msg_str or created_str
        ref_dt = datetime.fromisoformat(ref_str)
        if ref_dt.tzinfo is None:
            ref_dt = ref_dt.replace(tzinfo=timezone.utc)

        minutes_waiting = (now - ref_dt).total_seconds() / 60

        if minutes_waiting > config.SLA_FRT_THRESHOLD_MINS:
            on_duty = ticket.get('on_duty_agent_name') or 'Unknown'
            shift = ticket.get('shift_label') or '?'
            tid = ticket['ticket_id']
            user_issue = ticket.get('first_user_message') or ''

            # Build shift time range for display
            shift_info = ""
            for s in config.SHIFTS:
                if s['label'] == shift:
                    shift_info = f"{s['start']:02d}:00–{s['end']:02d}:00 UTC"
                    break

            wait_label = "Since last user msg" if last_msg_str else "Waiting"

            esc = config.html_escape
            parts = [
                f"⏰ <b>SLA Alert — {esc(tid)}</b>",
                "",
                f"⏳ {wait_label}: {int(minutes_waiting)} minutes "
                f"(threshold: {config.SLA_FRT_THRESHOLD_MINS} min)",
                f"👤 On-duty: <b>{esc(on_duty)}</b> "
                f"(Shift {esc(shift)}: {esc(shift_info)})",
            ]
            if user_issue:
                parts.append(f'📝 User issue: "{esc(user_issue)}"')

            send_telegram_alert("\n".join(parts))
            database.mark_sla_alert_sent(tid)
            print(f'[SLA ALERT] {tid} — {int(minutes_waiting)} min — {on_duty}')


@sla_check_loop.before_loop
async def before_sla_check():
    await client.wait_until_ready()


def send_telegram_alert(text):
    """Send an alert message to every configured Telegram chat."""
    token = config.TELEGRAM_TOKEN
    chat_ids = config.TELEGRAM_CHAT_IDS

    if not token or not chat_ids:
        print("⚠️  Telegram not configured — skipping SLA alert.")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for raw_chat_id in chat_ids:
        chat_id = raw_chat_id
        thread_id = None
        if '/' in chat_id:
            parts = chat_id.split('/')
            chat_id, thread_id = parts[0], parts[1]
        if not chat_id.startswith('-'):
            chat_id = f"-100{chat_id}"

        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if thread_id:
            payload["message_thread_id"] = thread_id

        try:
            r = requests.post(url, json=payload)
            r.raise_for_status()
        except Exception as e:
            print(f"❌ Failed to send Telegram alert to {chat_id}: {e}")


client.run(TOKEN)
