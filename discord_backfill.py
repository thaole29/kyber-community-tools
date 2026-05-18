"""
discord_backfill.py

Shared helper to scan Discord ticket channels and upsert their state into
the local SQLite store. Used by:
  - bot.py on_ready catch-up + periodic 15-min audit
  - backfill.py / backfill_closed.py CLI

Designed for SAFETY NET use: when backfilling historical state, alert
dedupe flags are pre-set so SLA loops do not fire retroactive alerts.
"""

import asyncio
import re
from datetime import datetime, timezone, timedelta

import discord

import config
import database


def normalize_id(name):
    m = re.search(r'(\d+)', name)
    return f"ticket-{m.group(1)}" if m else name


def _is_stale(updates, ticket=None, hours=24):
    """A ticket is 'stale' for alert-suppression purposes when no message
    activity (user msg, agent msg, or first response) has occurred in the
    last `hours`. created_at alone does NOT count — a 5-day-old ticket
    whose user just re-pinged 10 minutes ago is fresh, not stale.

    Looks at the effective post-update values (updates dict takes precedence
    over ticket dict) for last_user_msg_at, last_agent_msg_at,
    first_responded_at. created_at is the absolute floor only when no
    message field is set at all.
    """
    ticket = ticket or {}
    candidates = []
    for key in ('last_user_msg_at', 'last_agent_msg_at', 'first_responded_at'):
        v = updates.get(key) or ticket.get(key)
        if v:
            candidates.append(v)
    if not candidates:
        # Fall back to created_at when no message timestamps are available
        v = updates.get('created_at') or ticket.get('created_at')
        if v:
            candidates.append(v)
    if not candidates:
        return False  # unknown — don't claim staleness without evidence
    latest_str = max(candidates)
    try:
        latest_dt = datetime.fromisoformat(latest_str)
        if latest_dt.tzinfo is None:
            latest_dt = latest_dt.replace(tzinfo=timezone.utc)
    except Exception:
        return False
    age_hours = (datetime.now(tz=timezone.utc) - latest_dt).total_seconds() / 3600
    return age_hours > hours


async def _classify_member(guild, user_id):
    """Return (is_agent, display_name). Uses cache first, then fetch."""
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except Exception:
            return False, None
    role_names = [r.name for r in member.roles]
    is_agent = any(r in config.SUPPORT_ROLES for r in role_names)
    return is_agent, member.display_name


async def scan_channel(channel, guild, history_limit=200):
    """Scan a single ticket channel's message history and return a dict
    ready for database.upsert_ticket.

    Does NOT write to DB. Caller decides when to upsert and whether to
    mark alerts as suppressed.
    """
    tid = normalize_id(channel.name)
    created_at = channel.created_at.replace(tzinfo=timezone.utc)

    owner_id = None
    closed_by_id = None
    closed_by_agent = None
    closed_at = None
    first_user_msg_content = None
    first_user_msg_at = None
    last_user_msg_at = None
    first_agent_info = None  # (ts, agent_name, agent_user_id)
    last_agent_msg_at = None
    user_msg_excerpts: list[str] = []  # for classifier — first few user msgs

    try:
        async for msg in channel.history(limit=history_limit, oldest_first=True):
            ts = msg.created_at.replace(tzinfo=timezone.utc)

            # Ticket Tool bot — extract owner and closure metadata
            if msg.author.id == config.TICKET_TOOL_BOT_ID:
                owner_match = re.search(r'Hi <@!?(\d+)>', msg.content or '')
                if owner_match and owner_id is None:
                    owner_id = owner_match.group(1)
                for embed in msg.embeds:
                    desc = embed.description or ''
                    if 'Ticket Closed by' in desc:
                        cm = re.search(r'<@!?(\d+)>', desc)
                        if cm:
                            closed_by_id = cm.group(1)
                            closed_at = ts
                continue

            if msg.author.bot:
                continue

            is_agent, display_name = await _classify_member(guild, msg.author.id)
            if is_agent:
                if first_agent_info is None:
                    first_agent_info = (ts, config.normalize_agent(display_name), str(msg.author.id))
                last_agent_msg_at = ts
            else:
                if first_user_msg_at is None:
                    first_user_msg_at = ts
                    first_user_msg_content = msg.content
                last_user_msg_at = ts
                if len(user_msg_excerpts) < 5 and (msg.content or '').strip():
                    user_msg_excerpts.append((msg.content or '').strip())
    except discord.Forbidden:
        return None

    # Resolve closer-is-agent (best effort)
    if closed_by_id:
        try:
            is_agent_closer, _ = await _classify_member(guild, int(closed_by_id))
            closed_by_agent = is_agent_closer
        except Exception:
            closed_by_agent = None

    shift_label, on_duty_agent = config.get_on_duty_agent(created_at)
    on_duty_id = config.get_agent_id_by_name(on_duty_agent)

    excerpt = ' | '.join(user_msg_excerpts)[:2000] if user_msg_excerpts else None
    data = {
        'ticket_id': tid,
        'created_at': created_at.isoformat(),
        'ticket_owner': owner_id,
        'on_duty_agent_name': on_duty_agent,
        'on_duty_agent_id': on_duty_id,
        'shift_label': shift_label,
        'first_user_message': first_user_msg_content,
        'last_user_msg_at': last_user_msg_at.isoformat() if last_user_msg_at else None,
        'last_agent_msg_at': last_agent_msg_at.isoformat() if last_agent_msg_at else None,
        'conversation_excerpt': excerpt,
    }

    if first_agent_info:
        ts, agent_name, agent_user_id = first_agent_info
        response_mins = round((ts - created_at).total_seconds() / 60, 2)
        data.update({
            'first_responded_at': ts.isoformat(),
            'response_time_mins': response_mins,
            'agent_name': agent_name,
            'agent_user_id': agent_user_id,
            'on_duty_responded': (agent_name == on_duty_agent) if on_duty_agent else False,
            'cross_shift_help': (agent_name != on_duty_agent) if on_duty_agent else False,
            'sla_breached': response_mins > config.SLA_FRT_THRESHOLD_MINS,
        })

    if closed_at:
        data['closed_at'] = closed_at.isoformat()
        data['closed_by'] = closed_by_id
        if closed_by_agent is not None:
            data['closed_by_agent'] = closed_by_agent

    return data


async def audit_and_backfill(client, max_age_hours=2, suppress_alerts=True,
                             history_limit=200, log=print):
    """Compare active Discord ticket channels with DB; backfill any missing.

    max_age_hours: only consider channels created within this window.
                   Use None to scan every active ticket channel.
    suppress_alerts: when inserting NEW rows for historical tickets, also
                     set sla_alert_sent=1 and followup_alert_sent=1 so the
                     SLA loop will not fire retroactive alerts. Existing
                     rows are not touched on these flags.
    Returns list of ticket_ids that were newly created in DB.
    """
    created_tids = []
    cutoff = None
    if max_age_hours is not None:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=max_age_hours)

    for guild in client.guilds:
        for ch in guild.text_channels:
            if not any(ch.name.startswith(p) for p in config.TICKET_CHANNEL_PREFIXES):
                continue
            ch_created = ch.created_at.replace(tzinfo=timezone.utc)
            if cutoff and ch_created < cutoff:
                continue

            tid = normalize_id(ch.name)
            existed_before = database.get_ticket(tid) is not None
            if existed_before:
                # Skip — existing rows already tracked by the bot's live events.
                continue

            data = await scan_channel(ch, guild, history_limit=history_limit)
            if data is None:
                log(f'[CATCHUP] Skipped {tid}: no read permission', flush=True)
                continue

            if suppress_alerts:
                # Smart suppress: only silence dedupe flags when an alert
                # would be retro-spam. "Stale" is defined as no message
                # activity in the last 24h (user OR agent) — so a 5-day-old
                # ticket that the user just re-pinged still counts as fresh.
                stale = _is_stale(data, ticket=None, hours=24)
                already_responded = bool(data.get('first_responded_at'))
                if already_responded or stale:
                    data['sla_alert_sent'] = 1
                # Phase-2 dedupe: silence when conversation is in "agent
                # has the ball" state or the wait is stale.
                last_user = data.get('last_user_msg_at')
                last_agent = data.get('last_agent_msg_at')
                user_waiting = bool(last_user) and (
                    not last_agent or last_user > last_agent
                )
                if not user_waiting or stale:
                    data['followup_alert_sent'] = 1

            database.upsert_ticket(data)
            created_tids.append(tid)
            log(f'[CATCHUP] Backfilled {tid} (created={data.get("created_at")}, '
                f'agent_replied={"yes" if data.get("first_responded_at") else "no"}, '
                f'closed={"yes" if data.get("closed_at") else "no"})', flush=True)
            await asyncio.sleep(0.3)

    return created_tids


def _apply_reconcile_updates(ticket_id, updates):
    """Apply a partial UPDATE to a ticket row. Used by reconcile_open_tickets."""
    if not updates:
        return
    conn = database.get_connection()
    try:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE tickets SET {set_clause} WHERE ticket_id = ?",
            (*updates.values(), ticket_id),
        )
        conn.commit()
    finally:
        conn.close()


async def reconcile_open_tickets(client, max_age_hours=24, suppress_alerts=True,
                                 history_limit=100, log=print):
    """For every OPEN ticket (closed_at IS NULL) whose Discord channel still
    exists, re-sync message-level fields (last_user_msg_at,
    last_agent_msg_at, first_user_message, and Phase-1 first_responded_at
    if missing) against the actual channel history.

    Fixes the case where the bot processed agent on_message events but
    missed user on_message events during gateway disconnects (or vice
    versa), leaving DB timestamps inconsistent with reality.

    max_age_hours: only reconcile tickets whose last known activity is
                   within this window. Older opens are skipped to keep the
                   scan bounded.
    suppress_alerts: when this reconcile would change the row in a way
                     that could fire a retroactive SLA alert (e.g. setting
                     last_user_msg_at past last_agent_msg_at, or filling
                     in a late first_responded_at), set the matching alert
                     dedupe flag to 1 so the SLA loop stays silent.

    Returns list of (ticket_id, updates_dict) for changed rows.
    """
    open_tickets = database.get_open_tickets()
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=max_age_hours)

    channel_by_tid = {}
    for guild in client.guilds:
        for ch in guild.text_channels:
            if not any(ch.name.startswith(p) for p in config.TICKET_CHANNEL_PREFIXES):
                continue
            channel_by_tid[normalize_id(ch.name)] = (ch, guild)

    changed = []
    for ticket in open_tickets:
        tid = ticket['ticket_id']
        if tid not in channel_by_tid:
            continue  # channel deleted from Discord — nothing to reconcile

        # Bound the work: only reconcile tickets that have had *some* DB
        # activity in the window. Stale opens with no activity in the window
        # are skipped — EXCEPT when the ticket has never been excerpt'd, in
        # which case we want a first-time scan so the LLM classifier has
        # something to work with.
        latest_db_event = max(
            (s for s in (ticket.get('last_user_msg_at'),
                         ticket.get('last_agent_msg_at'),
                         ticket.get('first_responded_at'),
                         ticket.get('created_at')) if s),
            default=None,
        )
        needs_excerpt_first_time = not (ticket.get('conversation_excerpt') or '')
        if latest_db_event:
            latest_dt = datetime.fromisoformat(latest_db_event)
            if latest_dt.tzinfo is None:
                latest_dt = latest_dt.replace(tzinfo=timezone.utc)
            if latest_dt < cutoff and not needs_excerpt_first_time:
                continue
        # Scan the FULL cutoff window (not from latest known event) — the
        # bot may have caught a LATE agent msg but missed an EARLIER user
        # msg, so a watermark based on the latest known event would miss
        # exactly the events we need to recover.
        scan_after = cutoff

        ch, guild = channel_by_tid[tid]

        new_user_ts = None
        new_user_content = None
        new_agent_ts = None
        first_agent_seen = None  # (ts, display_name, user_id)
        excerpt_collected: list[str] = []
        # If this ticket has no conversation_excerpt yet, scan the FULL
        # channel history (not just the cutoff window) so the classifier
        # has something to work with. Otherwise stick to the window.
        needs_excerpt = not (ticket.get('conversation_excerpt') or '')
        scan_from = None if needs_excerpt else scan_after

        try:
            history_kwargs = {'limit': history_limit, 'oldest_first': True}
            if scan_from is not None:
                history_kwargs['after'] = scan_from
            async for msg in ch.history(**history_kwargs):
                if msg.author.id == config.TICKET_TOOL_BOT_ID:
                    continue
                if msg.author.bot:
                    continue
                ts = msg.created_at.replace(tzinfo=timezone.utc)
                # Only message-timestamp updates respect the cutoff window;
                # excerpt collection ignores it (we want full ticket context).
                in_window = ts >= scan_after
                is_agent, display_name = await _classify_member(guild, msg.author.id)
                if is_agent:
                    if in_window:
                        new_agent_ts = ts
                        if first_agent_seen is None:
                            first_agent_seen = (ts, display_name, str(msg.author.id))
                else:
                    if in_window:
                        if new_user_ts is None:
                            new_user_content = msg.content
                        new_user_ts = ts
                    if needs_excerpt and len(excerpt_collected) < 5 and (msg.content or '').strip():
                        excerpt_collected.append((msg.content or '').strip())
        except discord.Forbidden:
            continue

        updates = {}

        # User-side reconcile
        if new_user_ts is not None:
            db_user = ticket.get('last_user_msg_at')
            if db_user is None or new_user_ts.isoformat() > db_user:
                updates['last_user_msg_at'] = new_user_ts.isoformat()
                if not ticket.get('first_user_message') and new_user_content:
                    updates['first_user_message'] = new_user_content

        # Agent-side reconcile
        if new_agent_ts is not None:
            db_agent = ticket.get('last_agent_msg_at')
            if db_agent is None or new_agent_ts.isoformat() > db_agent:
                updates['last_agent_msg_at'] = new_agent_ts.isoformat()

        # conversation_excerpt — for the LLM classifier. Save once per ticket.
        if needs_excerpt and excerpt_collected:
            updates['conversation_excerpt'] = ' | '.join(excerpt_collected)[:2000]

        # Phase-1 first response, if the bot never recorded it
        if ticket.get('first_responded_at') is None and first_agent_seen is not None:
            ts, display_name, user_id = first_agent_seen
            agent_name = config.normalize_agent(display_name) if display_name else None
            created_at_str = ticket.get('created_at')
            response_mins = None
            if created_at_str:
                created_dt = datetime.fromisoformat(created_at_str)
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=timezone.utc)
                response_mins = round((ts - created_dt).total_seconds() / 60, 2)
            updates['first_responded_at'] = ts.isoformat()
            if agent_name:
                updates['agent_name'] = agent_name
                updates['agent_user_id'] = user_id
                on_duty = ticket.get('on_duty_agent_name')
                if on_duty:
                    updates['on_duty_responded'] = (agent_name == on_duty)
                    updates['cross_shift_help'] = (agent_name != on_duty)
            if response_mins is not None:
                updates['response_time_mins'] = response_mins
                updates['sla_breached'] = response_mins > config.SLA_FRT_THRESHOLD_MINS

        if not updates:
            continue

        # Smart suppress: only silence dedupe flags when an alert would be
        # retro-spam. "Stale" = no message activity in the last 24h
        # (see _is_stale); a long-lived ticket the user just re-pinged is
        # NOT stale.
        if suppress_alerts:
            new_user_eff = updates.get('last_user_msg_at') or ticket.get('last_user_msg_at')
            new_agent_eff = updates.get('last_agent_msg_at') or ticket.get('last_agent_msg_at')
            user_waiting = bool(new_user_eff) and (
                not new_agent_eff or new_user_eff > new_agent_eff
            )
            stale = _is_stale(updates, ticket=ticket, hours=24)

            # Phase-1: suppress when retro-filling first response or ticket
            # is stale-without-response.
            phase1_already_responded = (
                ticket.get('first_responded_at') is not None
                or 'first_responded_at' in updates
            )
            if phase1_already_responded or stale:
                updates['sla_alert_sent'] = 1
            # Phase-2: suppress when conversation is in "agent has the ball"
            # state, or wait is stale.
            if not user_waiting or stale:
                updates['followup_alert_sent'] = 1

        _apply_reconcile_updates(tid, updates)
        changed.append((tid, updates))
        log(f'[RECONCILE] {tid} updated fields: {sorted(updates.keys())}', flush=True)
        await asyncio.sleep(0.3)

    return changed
