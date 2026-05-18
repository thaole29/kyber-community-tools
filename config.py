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
    while cursor < end_dt:
        shift_label, shift_agent = get_on_duty_agent(cursor)
        boundary = _next_shift_boundary(cursor)
        segment_end = min(boundary, end_dt)
        mins = (segment_end - cursor).total_seconds() / 60.0
        is_final = segment_end >= end_dt
        if is_final and (responding_agent is None or shift_agent == responding_agent):
            seg_type = 'responded'
        else:
            # Final segment whose shift agent doesn't match the responder is
            # still a 'missed' contribution — the responder was helping cross-
            # shift, and this on-duty agent should have caught it.
            seg_type = 'missed'
        contributions.append({
            'agent': shift_agent,
            'shift_label': shift_label,
            'mins': round(mins, 2),
            'type': seg_type,
        })
        cursor = segment_end

    # If the responding agent helped cross-shift (none of the segments are
    # tagged 'responded'), tag the FINAL segment as 'cross_help' for that
    # responder. We still record the on-duty agent's miss for that segment;
    # the responder's true contribution is appended separately so their pool
    # gets credited.
    if responding_agent and not any(c['type'] == 'responded' for c in contributions):
        # The cross-help portion is the FINAL segment's minutes — that's the
        # time between the responder's shift start (or simply: end_dt minus
        # boundary) and end_dt. Since we already attributed those minutes to
        # the on-duty agent as 'missed', also append a parallel 'cross_help'
        # entry for the actual responder so their FRT pool reflects it.
        final = contributions[-1]
        contributions.append({
            'agent': responding_agent,
            'shift_label': None,
            'mins': final['mins'],
            'type': 'cross_help',
        })
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
