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

import asyncio
import discord
import os
import re
import requests
import subprocess
import sys
from datetime import datetime, time, timezone, timedelta
from pathlib import Path
from discord.ext import tasks

import config
import database
import discord_backfill
import classify_tickets

PROJECT_DIR = Path(__file__).resolve().parent
MARKER_DIR = PROJECT_DIR / 'logs' / '.markers'
PYTHON_BIN = PROJECT_DIR / 'venv' / 'bin' / 'python'

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
    print(f'Analytics Bot started successfully as: {client.user}', flush=True)
    # Start the SLA monitoring loop
    if not sla_check_loop.is_running():
        sla_check_loop.start()
    print(f'SLA monitoring loop started (every 5 minutes)', flush=True)
    # Start periodic catch-up loop (safety net for events lost during disconnect)
    if not catchup_loop.is_running():
        catchup_loop.start()
    print(f'Catch-up audit loop started (every 5 minutes)', flush=True)
    # Start safety-net loop for cron reports (DNS-on-wake recovery)
    if not safety_net_loop.is_running():
        safety_net_loop.start()
    print(f'Safety-net loop started (every 30 minutes, after 04:00 UTC)', flush=True)
    # Start LLM-based product classifier loop
    if not classify_loop.is_running():
        classify_loop.start()
    print(f'Classify loop started (every 15 minutes)', flush=True)
    # Aggressive one-shot scan on every (re)connect, to bridge any
    # gateway gap. Runs in background so it doesn't delay on_ready.
    asyncio.create_task(_initial_catchup())


async def _initial_catchup():
    """Right after a (re)connect, scan the last 24h of ticket channels and
    backfill anything the bot missed while disconnected. Also reconcile
    message timestamps on already-tracked open tickets — covers the case
    where the row exists but specific on_message events were dropped.
    Suppress alerts so historical breaches don't fire retroactive Telegram
    messages."""
    try:
        await asyncio.sleep(5)  # let gateway finish READY/guild sync
        created = await discord_backfill.audit_and_backfill(
            client, max_age_hours=24, suppress_alerts=True
        )
        if created:
            print(f'[ON_READY CATCHUP] Backfilled {len(created)} new ticket(s): {created}', flush=True)
        else:
            print(f'[ON_READY CATCHUP] No missing tickets in last 24h', flush=True)
        reconciled = await discord_backfill.reconcile_open_tickets(
            client, max_age_hours=24, suppress_alerts=True
        )
        if reconciled:
            print(f'[ON_READY CATCHUP] Reconciled {len(reconciled)} open ticket(s)', flush=True)
    except Exception as e:
        print(f'[ON_READY CATCHUP] Error: {e}', flush=True)


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

            # Late-response alert: response happened AFTER the SLA window
            # closed but BEFORE the 5-minute sla_check_loop could fire.
            # Without this, breaches in the gap between threshold and the
            # next loop tick go silent.
            if (ticket and rt is not None
                    and rt > config.SLA_FRT_THRESHOLD_MINS
                    and not ticket.get('sla_alert_sent')):
                shift = ticket.get('shift_label') or '?'
                shift_info = ""
                for s in config.SHIFTS:
                    if s['label'] == shift:
                        shift_info = f"{s['start']:02d}:00–{s['end']:02d}:00 UTC"
                        break

                esc = config.html_escape
                parts = [
                    f"⚠️ <b>SLA Breach (late response) — {esc(tid)}</b>",
                    "",
                    f"⏱️ Response time: {int(rt)} minutes "
                    f"(threshold: {config.SLA_FRT_THRESHOLD_MINS} min)",
                    f"👤 Responded by: <b>{esc(agent_name)}</b>",
                    f"📋 On-duty: <b>{esc(on_duty)}</b> "
                    f"(Shift {esc(shift)}: {esc(shift_info)})",
                ]
                if cross:
                    parts.append("🔄 Cross-shift help")

                ok = await send_telegram_alert_async("\n".join(parts))
                if ok:
                    database.mark_sla_alert_sent(tid)
                    print(f'[SLA LATE] {tid} — responded in {int(rt)} min — alert sent', flush=True)
                else:
                    print(f'[SLA LATE] {tid} — Telegram send failed; will retry', flush=True)
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

            ok = await send_telegram_alert_async("\n".join(parts))
            if ok:
                database.mark_sla_alert_sent(tid)
                print(f'[SLA ALERT] {tid} — {int(minutes_waiting)} min — {on_duty}', flush=True)
            else:
                print(f'[SLA ALERT] {tid} — Telegram send failed; will retry', flush=True)

    # =====================================================
    # PHASE 2 — Follow-up SLA
    # Tickets where the first reply already happened but the user has
    # posted again and is still waiting on a follow-up response.
    # =====================================================
    followup_candidates = database.get_followup_breach_candidates()
    for ticket in followup_candidates:
        last_user_str = ticket.get('last_user_msg_at')
        if not last_user_str:
            continue
        ref_dt = datetime.fromisoformat(last_user_str)
        if ref_dt.tzinfo is None:
            ref_dt = ref_dt.replace(tzinfo=timezone.utc)

        minutes_waiting = (now - ref_dt).total_seconds() / 60
        if minutes_waiting <= config.SLA_FRT_THRESHOLD_MINS:
            continue

        # On-duty is the agent on shift WHEN THE USER POSTED — i.e., the
        # agent who was supposed to respond. Using `now` would mis-attribute
        # a breach to the next shift's agent if the alert fires near a shift
        # boundary (or fires late after bot downtime).
        breach_shift, breach_on_duty = config.get_on_duty_agent(ref_dt)
        on_duty = breach_on_duty or 'Unknown'
        shift = breach_shift or '?'
        tid = ticket['ticket_id']

        shift_info = ""
        for s in config.SHIFTS:
            if s['label'] == shift:
                shift_info = f"{s['start']:02d}:00–{s['end']:02d}:00 UTC"
                break

        esc = config.html_escape
        parts = [
            f"⏰ <b>Follow-up SLA Alert — {esc(tid)}</b>",
            "",
            f"⏳ User waiting for follow-up: {int(minutes_waiting)} min "
            f"(threshold: {config.SLA_FRT_THRESHOLD_MINS} min)",
            f"📋 On-duty: <b>{esc(on_duty)}</b> "
            f"(Shift {esc(shift)}: {esc(shift_info)})",
        ]
        ok = await send_telegram_alert_async("\n".join(parts))
        if ok:
            database.mark_followup_alert_sent(tid)
            print(f'[FOLLOWUP SLA] {tid} — waiting {int(minutes_waiting)} min — on-duty: {on_duty}', flush=True)
        else:
            print(f'[FOLLOWUP SLA] {tid} — Telegram send failed; will retry', flush=True)


@sla_check_loop.before_loop
async def before_sla_check():
    await client.wait_until_ready()


# =====================================================
# CATCH-UP LOOP (every 5 minutes)
# Safety net for ticket-create events lost during gateway disconnects.
# Compares active Discord channels against DB and silently backfills any
# missing rows (with SLA alert flags pre-set so no retro alert fires).
# =====================================================

@tasks.loop(minutes=5)
async def catchup_loop():
    try:
        created = await discord_backfill.audit_and_backfill(
            client, max_age_hours=2, suppress_alerts=True
        )
        if created:
            print(f'[CATCHUP] Backfilled {len(created)} missing ticket(s): {created}', flush=True)
        # Reconcile message timestamps for open tickets — catches missed
        # on_message events even when the channel_create event was caught.
        reconciled = await discord_backfill.reconcile_open_tickets(
            client, max_age_hours=24, suppress_alerts=True
        )
        if reconciled:
            print(f'[CATCHUP] Reconciled {len(reconciled)} open ticket(s)', flush=True)
    except Exception as e:
        print(f'[CATCHUP] Error during audit: {e}', flush=True)


@catchup_loop.before_loop
async def before_catchup():
    await client.wait_until_ready()


# =====================================================
# SAFETY-NET LOOP for scheduled reports
# Each report script (daily_report, community_digest) writes a UTC-dated
# marker on successful send. Cron fires at 02:00 UTC (= 09:00 +07). If by
# 04:00 UTC the marker is missing (DNS failure, network not ready on
# Mac wake, etc.), this loop reruns the script as a subprocess.
# Cooldown prevents tight retries when the underlying issue persists.
# =====================================================

_SAFETY_NET_AFTER_UTC = time(hour=4, minute=0)
_safety_net_last_attempt = {}  # job_name -> datetime
_SAFETY_NET_JOBS = [
    # (marker_name, script_filename)
    ('daily_report', 'daily_report.py'),
    ('community_digest', 'community_digest.py'),
]


@tasks.loop(minutes=30)
async def safety_net_loop():
    now = datetime.now(tz=timezone.utc)
    if now.time() < _SAFETY_NET_AFTER_UTC:
        return  # too early — give cron its window first

    today = now.date().isoformat()
    for name, script in _SAFETY_NET_JOBS:
        marker = MARKER_DIR / f'{name}.success.{today}'
        if marker.exists():
            continue

        last = _safety_net_last_attempt.get(name)
        if last and (now - last) < timedelta(minutes=29):
            continue  # cooldown
        _safety_net_last_attempt[name] = now

        print(f'[SAFETY NET] {name} marker for {today} missing — rerunning', flush=True)
        try:
            proc = await asyncio.create_subprocess_exec(
                str(PYTHON_BIN), str(PROJECT_DIR / script),
                cwd=str(PROJECT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            stdout, _ = await proc.communicate()
            tail_lines = (stdout.decode(errors='ignore') or '').strip().split('\n')[-3:]
            print(f'[SAFETY NET] {name} exited {proc.returncode}; tail:', flush=True)
            for line in tail_lines:
                print(f'    {line}', flush=True)
        except Exception as e:
            print(f'[SAFETY NET] {name} subprocess error: {e}', flush=True)


@safety_net_loop.before_loop
async def before_safety_net():
    await client.wait_until_ready()


# =====================================================
# PRODUCT CLASSIFIER LOOP (every 15 minutes)
# Pulls open tickets that have user text but no product_group yet and
# calls Gemini to classify them in batches. Cached results power the
# dashboard's product breakdown. Idempotent — already-classified tickets
# are skipped via the WHERE product_group IS NULL filter.
# =====================================================

@tasks.loop(minutes=15)
async def classify_loop():
    try:
        n = await classify_tickets.classify_unclassified(batch_size=15, max_batches=3)
        if n:
            print(f'[CLASSIFY] Classified {n} ticket(s) this tick', flush=True)
    except Exception as e:
        print(f'[CLASSIFY] Error: {e}', flush=True)


@classify_loop.before_loop
async def before_classify():
    await client.wait_until_ready()


def send_telegram_alert(text):
    """Send an alert message to every configured Telegram chat.

    Returns True iff *every* configured target accepted the message.
    Caller should only mark the ticket as alerted on True so transient
    failures (timeout, 5xx, DNS) can be retried on the next loop tick.
    """
    token = config.TELEGRAM_TOKEN
    chat_ids = config.TELEGRAM_CHAT_IDS

    if not token or not chat_ids:
        print("⚠️  Telegram not configured — skipping SLA alert.", flush=True)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    all_ok = True
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
            r = requests.post(url, json=payload, timeout=10)
            r.raise_for_status()
        except Exception as e:
            all_ok = False
            print(f"❌ Failed to send Telegram alert to {chat_id}: {e}", flush=True)
    return all_ok


async def send_telegram_alert_async(text):
    """Run the blocking Telegram POST in a worker thread so the discord.py
    event loop keeps heart-beating even if Telegram's API is slow or down."""
    return await asyncio.to_thread(send_telegram_alert, text)


client.run(TOKEN)
