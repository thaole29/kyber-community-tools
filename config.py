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
