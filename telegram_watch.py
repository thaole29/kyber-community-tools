"""
telegram_watch.py

Responsiveness tracking for the internal staff Telegram group.

Ticket metrics only prove CS is working when tickets exist. This watcher
covers the quiet stretches: whenever one of the four agents is @-tagged in the
internal group, it starts a clock and stops it at the first sign of life from
that agent — any message they post in the chat, or a reaction they place on the
tagging message. A tag that goes unanswered past
`config.TELEGRAM_MENTION_SLA_MINS` (20) is recorded as a slow response.

Scope rule (user, 2026-07-27): the clock only counts when the tag lands inside
that agent's OWN shift. Off-shift tags are still stored (on_shift=0) so the
history is complete, but they are never flagged slow — the agent was off duty.

Run it either way:
    venv/bin/python telegram_watch.py       # standalone long-poll loop
    from bot.py                             # started as an asyncio task

REQUIRED SETUP — without these the watcher sees nothing:
  1. config.AGENT_TELEGRAM_USERNAMES must map each agent's @username.
  2. BotFather → /setprivacy → Disable. With privacy mode ON a bot only
     receives messages that mention the BOT, so tags of an agent — and the
     agent's own replies — are invisible to it.
  3. The bot must be a member of the group (admin if you want reactions:
     `message_reaction` updates are only delivered to admins).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import config      # noqa: E402
import database    # noqa: E402

API = "https://api.telegram.org/bot{token}/{method}"
LONG_POLL_SECS = 30
# message_reaction is opt-in: Telegram omits it unless it is named explicitly
# in allowed_updates.
ALLOWED_UPDATES = ["message", "edited_message", "message_reaction"]


def _log(msg):
    print(f"[TG-WATCH] {msg}", flush=True)


def _ts(unix_secs):
    """Telegram sends whole seconds since epoch, UTC."""
    return datetime.fromtimestamp(int(unix_secs), tz=timezone.utc)


def _can_poll():
    """Enough config to run at all. Commands like /record work with nothing
    but a token — Telegram delivers slash commands to a group bot even with
    privacy mode ON, so this path needs no BotFather change."""
    return bool(config.TELEGRAM_TOKEN)


def _mention_tracking_on():
    """Automatic @-tag timing additionally needs to know who the agents are on
    Telegram, and which chat to watch."""
    return bool(config.TELEGRAM_INTERNAL_CHAT_ID
                and (config.AGENT_TELEGRAM_USERNAMES or config.AGENT_TELEGRAM_IDS))


def _same_chat(chat_id):
    return str(chat_id) == str(config.TELEGRAM_INTERNAL_CHAT_ID)


def _identify(user):
    """Canonical agent name for a Telegram `User` object, or None.

    Checks the static config first, then ids learned from earlier sightings —
    so an agent who changes their @username keeps being tracked.
    """
    if not user:
        return None
    uid = user.get("id")
    name = config.get_agent_by_telegram(user_id=uid, username=user.get("username"))
    if not name:
        name = database.get_telegram_identities().get(str(uid))
    if name and uid:
        database.remember_telegram_identity(
            uid, name,
            username=(user.get("username") or "").lower() or None,
            display_name=" ".join(
                p for p in (user.get("first_name"), user.get("last_name")) if p
            ) or None,
        )
    return name


def _mentioned_agents(msg):
    """Every agent tagged in this message, from both mention entity kinds.

    `mention`      → plain "@username" text, resolve via the username map.
    `text_mention` → a user object (someone with no @username), resolve by id.
    """
    text = msg.get("text") or msg.get("caption") or ""
    entities = (msg.get("entities") or []) + (msg.get("caption_entities") or [])
    found = {}
    for ent in entities:
        etype = ent.get("type")
        if etype == "mention":
            handle = text[ent["offset"]:ent["offset"] + ent["length"]].lstrip("@").lower()
            name = config.AGENT_TELEGRAM_USERNAMES.get(handle)
            if name:
                found[name] = handle
        elif etype == "text_mention":
            name = _identify(ent.get("user") or {})
            if name:
                found[name] = (ent.get("user") or {}).get("username")
    return found


def _sender_label(user):
    if not user:
        return None
    if user.get("username"):
        return "@" + user["username"]
    return " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p) or None


def _known_agents():
    return sorted({s["agent"] for s in config.SHIFTS}
                  | set(config.AGENT_DISCORD_IDS.values()))


def _resolve_agent(token):
    """Map whatever was typed after /record onto a canonical agent name.

    Accepts the canonical name, any alias in AGENT_MAPPING, and @username when
    the Telegram map is filled in.
    """
    raw = (token or "").strip()
    if not raw:
        return None
    if raw.startswith("@"):
        hit = config.AGENT_TELEGRAM_USERNAMES.get(raw[1:].lower())
        if hit:
            return hit
    name = config.normalize_agent(raw)
    known = _known_agents()
    if name in known:
        return name
    for k in known:                       # case-insensitive last resort
        if k.lower() == raw.lower():
            return k
    return None


async def send_reply(session, chat_id, text, reply_to=None, thread_id=None):
    url = API.format(token=config.TELEGRAM_TOKEN, method="sendMessage")
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
        payload["allow_sending_without_reply"] = True
    if thread_id:
        payload["message_thread_id"] = thread_id
    try:
        async with session.post(url, json=payload,
                                timeout=aiohttp.ClientTimeout(total=20)) as resp:
            data = await resp.json()
        if not data.get("ok"):
            _log(f"sendMessage failed: {str(data)[:200]}")
    except Exception as exc:
        _log(f"sendMessage error: {exc!r}")


def _cmd_record(msg, arg, by):
    """/record <agent> [note] — log one response-time compliance reminder."""
    if not arg:
        return ("Usage: <code>/record &lt;agent&gt; [note]</code>\n"
                "Agents: " + ", ".join(_known_agents()))
    parts = arg.split(None, 1)
    agent = _resolve_agent(parts[0])
    if not agent:
        return (f"Unknown agent <b>{parts[0]}</b>.\n"
                "Valid: " + ", ".join(_known_agents()))
    note = parts[1] if len(parts) > 1 else None
    at = _ts(msg.get("date"))
    shift_label, on_duty = config.get_on_duty_agent(at)
    on_shift = (on_duty == agent)
    rid = database.record_agent_reminder(
        agent_name=agent, note=note, recorded_by=by, recorded_at=at.isoformat(),
        shift_label=shift_label, on_shift=on_shift,
        chat_id=(msg.get("chat") or {}).get("id"), message_id=msg.get("message_id"),
    )
    # Count so far today (UTC), so the group sees whether this is a pattern.
    day_start = at.replace(hour=0, minute=0, second=0, microsecond=0)
    today = len(database.get_agent_reminders_in_range(
        day_start, at + timedelta(seconds=1), agent_name=agent))
    local = at.astimezone(config.LOCAL_TZ)
    scope = (f"during their shift {shift_label}" if on_shift
             else f"outside their shift (shift {shift_label} belongs to {on_duty})")
    out = (f"📝 Reminder #{rid} recorded — <b>{agent}</b> "
           f"at {local:%H:%M} {scope}.")
    if note:
        out += f"\nNote: {note}"
    out += f"\nToday: <b>{today}</b> reminder(s). <i>Use /undo if this was a mistake.</i>"
    return out


def _cmd_records(arg, _by):
    """/records [days] [agent] — summary of reminders."""
    days, agent = 7, None
    for tok in (arg or "").split():
        if tok.isdigit():
            days = max(1, min(int(tok), 90))
        else:
            agent = _resolve_agent(tok) or agent
    end = datetime.now(timezone.utc) + timedelta(seconds=1)
    rows = database.get_agent_reminders_in_range(
        end - timedelta(days=days), end, agent_name=agent)
    if not rows:
        return f"No reminders in the last {days} day(s)." + (
            f" (agent: {agent})" if agent else "")
    per = {}
    for r in rows:
        per.setdefault(r["agent_name"], []).append(r)
    lines = [f"📋 <b>Reminders — last {days} day(s)</b> — {len(rows)} total"]
    for name in sorted(per, key=lambda k: -len(per[k])):
        lines.append(f"• <b>{name}</b>: {len(per[name])}")
    for r in rows[-5:]:
        when = datetime.fromisoformat(r["recorded_at"]).astimezone(config.LOCAL_TZ)
        note = f" — {r['note']}" if r["note"] else ""
        lines.append(f"   #{r['id']} {when:%m-%d %H:%M} {r['agent_name']}{note}")
    return "\n".join(lines)


def _cmd_undo(_arg, by):
    """/undo — remove the last reminder YOU logged."""
    last = database.get_last_agent_reminder(by)
    if not last:
        return "You have no reminder to undo."
    database.delete_agent_reminder(last["id"], recorded_by=by)
    when = datetime.fromisoformat(last["recorded_at"]).astimezone(config.LOCAL_TZ)
    return (f"🗑️ Removed #{last['id']} — {last['agent_name']} "
            f"({when:%m-%d %H:%M}).")


def _cmd_help(_arg, _by):
    return ("<b>Response-time tracking</b>\n"
            "<code>/record &lt;agent&gt; [note]</code> — log one missed response time\n"
            "<code>/records [days] [agent]</code> — summary (default: 7 days)\n"
            "<code>/undo</code> — remove your own most recent entry\n"
            "Agents: " + ", ".join(_known_agents()))


COMMANDS = {
    "record": _cmd_record,
    "records": _cmd_records,
    "undo": _cmd_undo,
    "help": _cmd_help,
}


def handle_command(msg):
    """Parse a slash command. Returns reply text, or None if not for us.

    Runs before the chat filter so a command sent from an unconfigured group
    can answer with that group's chat id — that is how you bootstrap
    TELEGRAM_INTERNAL_CHAT_ID without digging through the API by hand.
    """
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return None
    head, _, arg = text.partition(" ")
    cmd = head[1:].split("@")[0].lower()      # strip /cmd@botname
    handler = COMMANDS.get(cmd)
    if not handler:
        return None
    chat = msg.get("chat") or {}
    by = _sender_label(msg.get("from") or {}) or "?"
    if cmd == "record" and not _same_chat(chat.get("id")):
        return (f"⚠️ This group (chat id <code>{chat.get('id')}</code>) is not configured.\n"
                f"Set <code>TELEGRAM_INTERNAL_CHAT_ID={chat.get('id')}</code> in .env "
                f"and restart the bot.")
    if handler is _cmd_record:
        return handler(msg, arg.strip(), by)
    return handler(arg.strip(), by)


def handle_message(msg):
    """Record tags in this message, then let the sender clear their own."""
    chat = msg.get("chat") or {}
    if not _same_chat(chat.get("id")):
        return
    sender = msg.get("from") or {}
    if sender.get("is_bot"):
        return
    at = _ts(msg.get("date"))
    at_iso = at.isoformat()

    # The sender is alive in the chat → close every tag still open on them.
    # This is the "any message counts" rule: they do not have to hit Reply.
    sender_agent = _identify(sender)
    if sender_agent:
        closed = database.resolve_telegram_mentions(
            chat.get("id"), sender_agent, at_iso, "message")
        for c in closed:
            _log(f"{sender_agent} answered a tag in {c['response_mins']}m"
                 f"{' — SLOW' if c['slow'] else ''}")

    for agent, _handle in _mentioned_agents(msg).items():
        # Tagging yourself is not a request for a response.
        if agent == sender_agent:
            continue
        shift_label, on_duty = config.get_on_duty_agent(at)
        on_shift = (on_duty == agent)
        added = database.record_telegram_mention(
            chat_id=chat.get("id"),
            message_id=msg.get("message_id"),
            agent_name=agent,
            mentioned_by=_sender_label(sender),
            mentioned_at=at_iso,
            mention_text=msg.get("text") or msg.get("caption"),
            shift_label=shift_label,
            on_shift=on_shift,
        )
        if added:
            _log(f"tagged {agent} at {at:%H:%M} UTC — "
                 f"{'on shift ' + str(shift_label) if on_shift else 'OFF shift, not counted'}")


def handle_reaction(upd):
    """A reaction from the tagged agent counts as 'I saw it'."""
    chat = upd.get("chat") or {}
    if not _same_chat(chat.get("id")):
        return
    if not upd.get("new_reaction"):        # reaction removed, not added
        return
    agent = _identify(upd.get("user") or {})
    if not agent:
        return
    at_iso = _ts(upd.get("date")).isoformat()
    closed = database.resolve_telegram_mentions(
        chat.get("id"), agent, at_iso, "reaction",
        message_id=upd.get("message_id"))
    for c in closed:
        _log(f"{agent} reacted to a tag after {c['response_mins']}m"
             f"{' — SLOW' if c['slow'] else ''}")


def dispatch(update):
    """Handle one update. Returns a (chat_id, text, reply_to, thread) tuple when
    the caller should post a reply, else None. Kept sync + side-effect-only so
    it can be unit-tested without a network stub."""
    if "message" in update or "edited_message" in update:
        msg = update.get("message") or update["edited_message"]
        reply = handle_command(msg)
        if reply is not None:
            chat = msg.get("chat") or {}
            return (chat.get("id"), reply, msg.get("message_id"),
                    msg.get("message_thread_id"))
        # A slash command is never also a tag, so only fall through for
        # ordinary messages. An edit can add a tag the original lacked.
        if _mention_tracking_on():
            handle_message(msg)
    elif "message_reaction" in update:
        if _mention_tracking_on():
            handle_reaction(update["message_reaction"])
    return None


async def poll_once(session, offset):
    """One long-poll round. Returns the next offset, or the same one on error."""
    url = API.format(token=config.TELEGRAM_TOKEN, method="getUpdates")
    payload = {"timeout": LONG_POLL_SECS, "allowed_updates": ALLOWED_UPDATES}
    if offset is not None:
        payload["offset"] = offset
    async with session.post(url, json=payload,
                            timeout=aiohttp.ClientTimeout(total=LONG_POLL_SECS + 15)) as resp:
        data = await resp.json()
    if not data.get("ok"):
        _log(f"getUpdates not ok: {str(data)[:200]}")
        return offset
    for upd in data.get("result", []):
        try:
            reply = dispatch(upd)
            if reply:
                chat_id, text, reply_to, thread_id = reply
                await send_reply(session, chat_id, text, reply_to, thread_id)
        except Exception as exc:                      # one bad update must not
            _log(f"update {upd.get('update_id')} failed: {exc!r}")   # kill the loop
        offset = upd["update_id"] + 1
        database.set_watch_offset(offset)
    return offset


async def watch_loop():
    """Long-poll forever. Safe to run alongside bot.py's Discord loops."""
    if not _can_poll():
        _log("no TELEGRAM_BOT_TOKEN — watcher idle")
        return
    database.init_db()
    offset = database.get_watch_offset()
    _log(f"commands on (/record, /records, /undo) | "
         f"mention tracking {'ON' if _mention_tracking_on() else 'OFF (agent @usernames not mapped)'} | "
         f"chat={config.TELEGRAM_INTERNAL_CHAT_ID or 'unset'} | "
         f"SLA {config.TELEGRAM_MENTION_SLA_MINS}m | offset={offset}")
    backoff = 5
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                offset = await poll_once(session, offset)
                backoff = 5
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # DNS on this machine drops regularly; never die on it.
                _log(f"poll failed ({exc!r}) — retry in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)


async def sweep_overdue():
    """Stamp still-unanswered on-shift tags as slow once past the threshold, so
    a report does not have to wait for a reply that may never come."""
    n = database.flag_overdue_telegram_mentions()
    if n:
        _log(f"flagged {n} overdue tag(s) as slow")
    return n


if __name__ == "__main__":
    try:
        asyncio.run(watch_loop())
    except KeyboardInterrupt:
        pass
