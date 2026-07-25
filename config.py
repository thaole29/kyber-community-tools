"""
config.py

Centralized configuration for the Support Analytics tools.
Loads environment variables and defines common constants,
shift schedules, and shared utility functions.
"""

import os
import pandas as pd
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# Load environment variables once
load_dotenv()

# --- Discord & Telegram Tokens ---
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
# Spec v2 standard name is TELEGRAM_BOT_TOKEN; legacy TELEGRAM_TOKEN kept as fallback.
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN') or os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
TELEGRAM_CHAT_ID_2 = os.getenv('TELEGRAM_CHAT_ID_2')

# Broadcast list — every report/alert is sent to each chat id here.
# Add a 3rd group later by exporting TELEGRAM_CHAT_ID_3 and appending below.
TELEGRAM_CHAT_IDS = [cid for cid in [TELEGRAM_CHAT_ID, TELEGRAM_CHAT_ID_2] if cid]

# --- LLM (community digest, Section 2) ---
# Gemini is the current provider (free tier). ANTHROPIC_API_KEY is kept
# for backward compat with archived spec v2; not used at runtime.
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')

# =====================================================
# CHATBOT (RAG over project data) — Section 3
# =====================================================
# Standalone Q&A chatbot over tickets.db + community_digests. The chat-completion
# LLM is provider-agnostic so a bring-your-own endpoint can be plugged in later
# via .env. Until CHATBOT_LLM_* point at your own model, the adapter falls back
# to Gemini (reusing GEMINI_API_KEY) so the bot is fully testable today.
CHATBOT_LLM_PROVIDER = os.getenv('CHATBOT_LLM_PROVIDER', 'gemini')   # gemini | openai | anthropic
CHATBOT_LLM_BASE_URL = os.getenv('CHATBOT_LLM_BASE_URL')            # e.g. http://host:port/v1 for OpenAI-compatible
CHATBOT_LLM_API_KEY = os.getenv('CHATBOT_LLM_API_KEY')             # falls back to GEMINI_API_KEY for the gemini provider
CHATBOT_LLM_MODEL = os.getenv('CHATBOT_LLM_MODEL', GEMINI_MODEL)
CHATBOT_LLM_MAX_TOKENS = int(os.getenv('CHATBOT_LLM_MAX_TOKENS', '1500'))
CHATBOT_LLM_TEMPERATURE = float(os.getenv('CHATBOT_LLM_TEMPERATURE', '0.2'))

# Embeddings are independent of the chat LLM (Gemini free tier by default).
CHATBOT_EMBED_PROVIDER = os.getenv('CHATBOT_EMBED_PROVIDER', 'gemini')
CHATBOT_EMBED_MODEL = os.getenv('CHATBOT_EMBED_MODEL', 'gemini-embedding-001')

# Vector index lives in its own SQLite file so tickets.db schema stays untouched.
CHATBOT_INDEX_DB = os.getenv('CHATBOT_INDEX_DB', 'chatbot/chatbot_index.db')
CHATBOT_TOP_K = int(os.getenv('CHATBOT_TOP_K', '6'))
CHATBOT_PORT = int(os.getenv('CHATBOT_PORT', '8100'))

# HTTP Basic Auth for the chatbot (the service is exposed via a public tunnel).
# Fail-closed: if these are unset, every route returns 503 so an open endpoint
# is never served by accident. Set both in .env to enable access.
CHATBOT_AUTH_USER = os.getenv('CHATBOT_AUTH_USER')
CHATBOT_AUTH_PASS = os.getenv('CHATBOT_AUTH_PASS')

# =====================================================
# COMMUNITY DIGEST CONFIG (Section 2)
# =====================================================
# Discord channel NAMES (without leading '#') to monitor for the daily
# community discussion digest. Match against message.channel.name.
COMMUNITY_CHANNELS = [
    '🌎general-english',
    '🆘community-support',
    '💻devs-discussion',
    '📈degen-speculation',
    '🌌swap-aggregator',
    '🧮limit-order',
    '🌾kyber-earn',
    '🏆kyberians-of-the-month',
    '💡ideas-hub',
]

# Cap messages fetched per channel per run; sample evenly if exceeded.
COMMUNITY_MAX_MESSAGES_PER_CHANNEL = 100
# Group consecutive messages from the same author within this window.
COMMUNITY_BURST_WINDOW_SECS = 120
# Skip messages shorter than this (in words) unless they are replies.
COMMUNITY_MIN_WORDS = 5

# --- Database & Legacy ---
DB_FILE = 'tickets.db'
CSV_FILE = 'ticket_analytics.csv'  # kept for migration reference

# --- Timezone Configuration ---
TZ_OFFSET = timedelta(hours=7)
LOCAL_TZ = timezone(TZ_OFFSET)

# --- Ticket Tool Categories & Naming ---
TICKET_CHANNEL_PREFIXES = ['ticket-', 'claimed-', 'closed-']
CLOSED_CATEGORY_NAMES = ['closed tickets', 'closed', 'archived tickets', 'archived']
SUPPORT_ROLES = ['Community Admin']
TICKET_TOOL_BOT_ID = 557628352828014614  # Ticket Tool's Discord user ID

# --- Guild ID ---
GUILD_ID = 608934314960224276

# =====================================================
# AGENT CONFIGURATION
# =====================================================

# Canonical mapping to group duplicate agent display names
AGENT_MAPPING = {
    'dablendo': 'Dablendo',
    'dablendo | kybernetwork': 'Dablendo',
    'reus11123': 'Reus',
    'reus/ kyber': 'Reus',
    'joseph_mikaelson': 'Mikaelson',
    'mikaelson': 'Mikaelson',
    'terrormichael': 'TerrorMichael',
}

# Discord User ID → Canonical Agent Name
AGENT_DISCORD_IDS = {
    '882483828130734140': 'Dablendo',
    '965601501873598574': 'Mikaelson',
    '919921788610314282': 'TerrorMichael',
    '757611576797954058': 'Reus',
}

# =====================================================
# SHIFT CONFIGURATION (all times in UTC)
# =====================================================
# Full 24-hour coverage, no gaps.
#   Shift A: Dablendo       02:00 – 09:00 UTC (7h)
#   Shift B: Mikaelson      09:00 – 17:00 UTC (8h)
#   Shift C: TerrorMichael  17:00 – 02:00 UTC (9h, crosses midnight)
#
# Reus is an optional agent — not assigned to any shift.

SHIFTS = [
    {"label": "A", "agent": "Dablendo",      "start": 2,  "end": 9},
    {"label": "B", "agent": "Mikaelson",      "start": 9,  "end": 17},
    {"label": "C", "agent": "TerrorMichael",  "start": 17, "end": 2},  # crosses midnight
]

# =====================================================
# SHIFT BUDDIES (time-windowed overlap pairs)
# =====================================================
# Declares pairs of agents that share the seat for a specific UTC hour
# window. When a buddy of the on-duty agent answers a ticket AND the reply
# moment falls inside the declared window, the on-duty agent is NOT charged
# as "missed" (their buddy effectively covered the seat). The buddy still
# gets the response credit as `cross_help` since they are not the shift's
# primary owner in SHIFTS.
#
# Keyed by on-duty agent → list of {buddy, start, end} entries.
# `start`/`end` are UTC hours, half-open [start, end). Windows that cross
# midnight (end <= start) are supported.
#
# Current pairings (per 2026-05-25 spec from product):
#   Reus ↔ Dablendo   09:00–16:00 UTC+7  →  02:00–09:00 UTC (full Shift A)
#   Reus ↔ Mikaelson  16:00–18:00 UTC+7  →  09:00–11:00 UTC (first 2h of Shift B)
SHIFT_BUDDIES = {
    'Dablendo': [
        {'buddy': 'Reus', 'start': 2, 'end': 9},
    ],
    'Mikaelson': [
        {'buddy': 'Reus', 'start': 9, 'end': 11},
    ],
}


def is_shift_buddy(on_duty_agent, responder, time_utc=None):
    """Return True if `responder` covers `on_duty_agent`'s seat at `time_utc`.

    `time_utc` is the moment to evaluate (typically the response time). If
    omitted, returns True when ANY buddy window exists for the pair — useful
    for boolean "is there any overlap relation" checks.
    """
    if not on_duty_agent or not responder:
        return False
    entries = SHIFT_BUDDIES.get(on_duty_agent, [])
    matches = [e for e in entries if e.get('buddy') == responder]
    if not matches:
        return False
    if time_utc is None:
        return True
    if time_utc.tzinfo is None:
        t = time_utc.replace(tzinfo=timezone.utc)
    else:
        t = time_utc.astimezone(timezone.utc)
    h = t.hour
    for e in matches:
        start, end = e['start'], e['end']
        if start < end:
            if start <= h < end:
                return True
        else:
            # window crosses midnight (e.g. 22:00–02:00)
            if h >= start or h < end:
                return True
    return False

# =====================================================
# SLA THRESHOLDS
# =====================================================
SLA_FRT_THRESHOLD_MINS = 15        # First Response Time target: ≤ 15 minutes
SLA_RESOLUTION_THRESHOLD_MINS = 1440  # Resolution target: ≤ 24 hours (1440 min)

# =====================================================
# SHARED UTILITY FUNCTIONS
# =====================================================

def normalize_agent(name):
    """Normalize agent display name to its canonical form."""
    if pd.isna(name):
        return name
    n = str(name).strip().lower()
    return AGENT_MAPPING.get(n, name)


def get_agent_name_by_id(user_id):
    """Look up canonical agent name from their Discord user ID."""
    return AGENT_DISCORD_IDS.get(str(user_id))


# Reverse map (built once): canonical name → Discord user id
_AGENT_NAME_TO_ID = {name: uid for uid, name in AGENT_DISCORD_IDS.items()}


def get_agent_id_by_name(name):
    """Look up Discord user ID from a canonical agent name."""
    if not name:
        return None
    return _AGENT_NAME_TO_ID.get(name)


def get_on_duty_agent(ticket_time_utc):
    """
    Determine which agent is on duty at a given UTC time.
    Returns (shift_label, agent_name) or (None, None) if lookup fails.

    Boundary rule: use >= for start, < for end.
    Handles TerrorMichael's midnight-crossing shift.
    """
    if ticket_time_utc is None:
        return None, None
    # Ensure we're working in UTC
    if ticket_time_utc.tzinfo is None:
        ticket_time_utc = ticket_time_utc.replace(tzinfo=timezone.utc)
    else:
        ticket_time_utc = ticket_time_utc.astimezone(timezone.utc)

    hour = ticket_time_utc.hour
    for shift in SHIFTS:
        if shift["start"] < shift["end"]:
            # Normal shift (e.g. 02:00–09:00)
            if shift["start"] <= hour < shift["end"]:
                return shift["label"], shift["agent"]
        else:
            # Midnight-crossing shift (e.g. 17:00–02:00)
            if hour >= shift["start"] or hour < shift["end"]:
                return shift["label"], shift["agent"]
    return None, None  # Should never happen with full coverage


def _next_shift_boundary(dt):
    """Given a UTC datetime, return the next shift transition time after `dt`.
    Used to walk shift segments when splitting FRT across handoffs.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    # Build the list of UTC hours where a shift starts/ends (these are the
    # transition points). From SHIFTS, transitions are at hours 2, 9, 17.
    transitions = sorted({s["start"] for s in SHIFTS} | {s["end"] for s in SHIFTS})
    today_midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    for offset_days in (0, 1):
        for h in transitions:
            cand = today_midnight + timedelta(days=offset_days, hours=h)
            if cand > dt:
                return cand
    # Should never happen with our 3-shift, 24h coverage
    return dt + timedelta(hours=1)


def compute_frt_contributions(start_dt, end_dt, responding_agent=None):
    """Walk [start_dt, end_dt] across shift boundaries; return a list of
    {agent, mins, type} entries.

    `start_dt` is the moment the wait began (typically `last_user_msg_at`,
    falling back to `created_at`). `end_dt` is the first agent reply
    (`first_responded_at`). Both must be timezone-aware UTC.

    Each segment's `type` is:
      - 'responded'  : segment whose end is `end_dt` AND whose shift agent
                       matches `responding_agent`. This is the cross-help
                       (or on-shift) agent's contribution.
      - 'missed'     : any earlier segment whose shift agent did NOT respond
                       during their shift before it ended. Agent is "blamed"
                       for the time the user was waiting on their watch.
      - 'covered'    : the on-duty agent's segment when SOMEONE ELSE answered
                       INSIDE that same shift (no boundary crossed). The seat
                       was still theirs, so they own the REAL wait
                       (`end_dt - start_dt`) — not the punitive "rest of your
                       shift" charge that 'missed' applies. `covered_by` names
                       the agent who actually replied.
      - 'covering'   : marker (mins=0) for that actual responder. The ticket
                       WAS handled, so this is credit-by-label only: the
                       minutes belong to the shift owner, not to them.
                       `covered_for` names the on-duty agent they helped.
      - 'followup'   : marker (mins=0) for the agent on duty when a PREVIOUS
                       shift's owner answered their OWN ticket late, spilling
                       into this shift. They never owned the wait, so they are
                       NOT charged a miss — the ticket is just on their radar
                       to follow up. The late responder owns the whole gap.

    Same-shift cover (user rule 2026-07-25):
      Ticket opens during Shift X → another agent replies BEFORE X ends.
      X agent gets `end_dt - start_dt` minutes (type=covered, covered_by=Y).
      Y gets a 0-minute 'covering' marker only.

    Cross-shift split (the case the user described):
      Ticket waits during Shift X → X ends → Shift Y agent eventually replies.
      X agent gets `shift_end_X - start_dt` minutes (type=missed).
      Y agent gets `end_dt - shift_start_Y` minutes (type=responded).

    Returns [] if start_dt >= end_dt or either is None.
    """
    if not start_dt or not end_dt:
        return []
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)
    if start_dt >= end_dt:
        return []

    contributions = []
    cursor = start_dt
    prior_agents = set()  # on-duty agents of segments already traversed
    while cursor < end_dt:
        shift_label, shift_agent = get_on_duty_agent(cursor)
        boundary = _next_shift_boundary(cursor)
        segment_end = min(boundary, end_dt)
        is_final = segment_end >= end_dt

        if is_final and (responding_agent is None or shift_agent == responding_agent):
            # On-duty agent closed the wait in this segment.
            mins = (segment_end - cursor).total_seconds() / 60.0
            contributions.append({
                'agent': shift_agent,
                'shift_label': shift_label,
                'mins': round(mins, 2),
                'type': 'responded',
            })
            cursor = segment_end
        elif is_final and shift_agent != responding_agent:
            # Cross-help within on-duty's shift: on-duty personally failed
            # during their shift. Charge them up to their shift end (boundary),
            # NOT response time — per the accountability rule. The actual
            # cross-helper gets credit for their true response gap.
            #
            # EXCEPTION (shift buddy): if the responder is declared as an
            # overlap buddy of the on-duty agent (see SHIFT_BUDDIES), the
            # buddy is treated as covering the seat. The on-duty agent is
            # NOT marked missed; the buddy still gets cross_help credit
            # for the actual wait time.
            cross_help_mins = (end_dt - cursor).total_seconds() / 60.0
            # CASE B (previous-shift owner finishing late): the responder was
            # on-duty in an EARLIER segment of this same wait — i.e. they are
            # answering their OWN ticket late, spilling past their shift end
            # into a later agent's shift. The later on-duty agent (shift_agent)
            # never owned this wait, so they are NOT charged a miss; they only
            # get a zero-minute 'followup' marker (the ticket is now on their
            # radar to follow up). The responder owns the whole gap: their
            # earlier-shift miss was already recorded, and this final segment
            # is their actual (late) response delivery.
            if responding_agent and responding_agent in prior_agents:
                contributions.append({
                    'agent': responding_agent,
                    'shift_label': None,
                    'mins': round(cross_help_mins, 2),
                    'type': 'responded',
                })
                contributions.append({
                    'agent': shift_agent,
                    'shift_label': shift_label,
                    'mins': 0.0,
                    'type': 'followup',
                })
                cursor = end_dt
                continue
            # CASE A (same-shift cover, user rule 2026-07-25): no boundary was
            # ever crossed (`prior_agents` is empty ⇒ cursor is still
            # start_dt), so the reply landed inside the on-duty agent's OWN
            # shift. The ticket was handled; nobody is "missed". The on-duty
            # agent still owns the seat, so they carry the REAL wait
            # (end_dt - start_dt) instead of being charged up to their shift
            # end, and the responder is recorded as covering them.
            if not prior_agents:
                contributions.append({
                    'agent': shift_agent,
                    'shift_label': shift_label,
                    'mins': round(cross_help_mins, 2),
                    'type': 'covered',
                    'covered_by': responding_agent,
                })
                if responding_agent:
                    contributions.append({
                        'agent': responding_agent,
                        'shift_label': None,
                        'mins': 0.0,
                        'type': 'covering',
                        'covered_for': shift_agent,
                    })
                cursor = end_dt
                continue
            # Boundary already crossed and a declared buddy closed it out:
            # keep the historical full waiver (0 mins for the on-duty seat).
            if is_shift_buddy(shift_agent, responding_agent, end_dt):
                if responding_agent:
                    contributions.append({
                        'agent': responding_agent,
                        'shift_label': None,
                        'mins': round(cross_help_mins, 2),
                        'type': 'cross_help',
                    })
                # Marker so reports/dashboards can show "Dablendo's seat was
                # covered by Reus" without counting it as a miss. mins=0 so
                # it does not affect any FRT averages.
                contributions.append({
                    'agent': shift_agent,
                    'shift_label': shift_label,
                    'mins': 0.0,
                    'type': 'buddy_covered',
                    'buddy': responding_agent,
                })
                cursor = end_dt
                continue
            missed_mins = (boundary - cursor).total_seconds() / 60.0
            contributions.append({
                'agent': shift_agent,
                'shift_label': shift_label,
                'mins': round(missed_mins, 2),
                'type': 'missed',
            })
            if responding_agent:
                contributions.append({
                    'agent': responding_agent,
                    'shift_label': None,
                    'mins': round(cross_help_mins, 2),
                    'type': 'cross_help',
                })
            cursor = end_dt
        else:
            # Non-final segment: on-duty agent misses for the full segment
            # width (they failed for their entire remaining shift slice).
            mins = (segment_end - cursor).total_seconds() / 60.0
            contributions.append({
                'agent': shift_agent,
                'shift_label': shift_label,
                'mins': round(mins, 2),
                'type': 'missed',
            })
            prior_agents.add(shift_agent)
            cursor = segment_end

    return contributions


def parse_dt(val):
    """Parse a datetime string into a timezone-aware datetime (local tz)."""
    if pd.isna(val) or not val:
        return None
    try:
        dt = datetime.fromisoformat(str(val))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(LOCAL_TZ)
    except Exception:
        return None


def parse_dt_utc(val):
    """Parse a datetime string into a timezone-aware datetime in UTC."""
    if pd.isna(val) or not val:
        return None
    try:
        dt = datetime.fromisoformat(str(val))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def fmt_mins(val):
    """Format minutes into a readable 'Xh Ym' or 'Ym Zs' string."""
    if val is None or pd.isna(val):
        return 'N/A'
    h = int(val // 60)
    m = int(val % 60)
    if h > 0:
        return f'{h}h {m}m'
    return f'{m}m {int((val % 1) * 60)}s'


def html_escape(text):
    """
    Escape Telegram HTML-mode special chars: &, <, >.
    Telegram HTML doesn't require escaping quotes outside of attributes.
    """
    if text is None:
        return ''
    return (
        str(text)
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
    )
