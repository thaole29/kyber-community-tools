"""
database.py

SQLite database layer for ticket analytics.
Provides schema creation, CRUD helpers, and query functions
used by bot.py, reports, and backfill scripts.
"""

import sqlite3
import os
import json
from datetime import datetime, timezone
import config


def get_connection():
    """Get a connection to the SQLite database."""
    conn = sqlite3.connect(config.DB_FILE)
    conn.row_factory = sqlite3.Row  # Access columns by name
    conn.execute("PRAGMA journal_mode=WAL")  # Better concurrent read performance
    return conn


def init_db():
    """Create the tickets table if it doesn't exist; apply additive migrations."""
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id             TEXT UNIQUE NOT NULL,
            created_at            TEXT,
            first_responded_at    TEXT,
            response_time_mins    REAL,
            agent_name            TEXT,
            agent_user_id         TEXT,
            ticket_owner          TEXT,
            closed_by             TEXT,
            closed_by_agent       BOOLEAN,
            closed_at             TEXT,
            deleted_at            TEXT,
            on_duty_agent_id      TEXT,
            on_duty_agent_name    TEXT,
            on_duty_responded     BOOLEAN DEFAULT 0,
            sla_breached          BOOLEAN DEFAULT 0,
            sla_alert_sent        BOOLEAN DEFAULT 0,
            cross_shift_help      BOOLEAN DEFAULT 0,
            shift_label           TEXT,
            first_user_message    TEXT,
            last_user_msg_at      TEXT,
            last_agent_msg_at     TEXT,
            followup_alert_sent   BOOLEAN DEFAULT 0
        )
    """)

    # Additive migrations for DBs created before these columns existed.
    existing_cols = {row['name'] for row in conn.execute("PRAGMA table_info(tickets)")}
    for col, ddl in (
        ('on_duty_agent_id',     'TEXT'),
        ('first_user_message',   'TEXT'),
        ('last_user_msg_at',     'TEXT'),
        ('last_agent_msg_at',    'TEXT'),
        ('followup_alert_sent',  'BOOLEAN DEFAULT 0'),
    ):
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE tickets ADD COLUMN {col} {ddl}")

    # Backfill on_duty_agent_id from on_duty_agent_name on existing rows.
    for agent_id, canonical_name in config.AGENT_DISCORD_IDS.items():
        conn.execute(
            "UPDATE tickets SET on_duty_agent_id = ? "
            "WHERE on_duty_agent_name = ? AND on_duty_agent_id IS NULL",
            (agent_id, canonical_name),
        )

    # Community digest storage (Section 2).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS community_digests (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            digest_date   TEXT NOT NULL,
            channel       TEXT NOT NULL,
            digest_json   TEXT NOT NULL,
            message_count INTEGER,
            created_at    TEXT NOT NULL,
            UNIQUE (digest_date, channel)
        )
    """)

    conn.commit()
    conn.close()


# =====================================================
# CREATE / UPDATE HELPERS
# =====================================================

def create_ticket(ticket_id, created_at_utc):
    """
    Record a new ticket. Automatically computes on-duty agent and shift.
    created_at_utc should be a timezone-aware datetime in UTC.
    """
    shift_label, on_duty_agent = config.get_on_duty_agent(created_at_utc)
    on_duty_agent_id = config.get_agent_id_by_name(on_duty_agent)

    conn = get_connection()
    try:
        conn.execute("""
            INSERT OR IGNORE INTO tickets
                (ticket_id, created_at, on_duty_agent_id, on_duty_agent_name, shift_label)
            VALUES (?, ?, ?, ?, ?)
        """, (
            ticket_id,
            created_at_utc.isoformat() if created_at_utc else None,
            on_duty_agent_id,
            on_duty_agent,
            shift_label,
        ))
        conn.commit()
    finally:
        conn.close()


def record_response(ticket_id, agent_name, agent_user_id, responded_at_utc):
    """
    Record an agent message in a ticket.

    Always updates last_agent_msg_at and clears followup_alert_sent so the
    next user-wait cycle can re-alert. Additionally, if this is the FIRST
    agent reply, computes FRT, on-duty match, cross-shift flag, and sets
    first_responded_at. Returns True only when it recorded the first reply.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()

        if not row:
            return False

        # Always log this agent activity and reset follow-up dedupe.
        conn.execute(
            "UPDATE tickets SET last_agent_msg_at = ?, followup_alert_sent = 0 "
            "WHERE ticket_id = ?",
            (responded_at_utc.isoformat(), ticket_id),
        )

        if row['first_responded_at'] is not None:
            conn.commit()
            return False  # Already has a first response — but bookkeeping above did run

        # Compute response time
        response_mins = None
        if row['created_at']:
            created = datetime.fromisoformat(row['created_at'])
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            diff = responded_at_utc - created
            response_mins = round(diff.total_seconds() / 60, 2)

        # Normalize agent name
        canonical_agent = config.normalize_agent(agent_name)

        # Determine on-duty match
        on_duty_agent = row['on_duty_agent_name']
        on_duty_responded = (canonical_agent == on_duty_agent) if on_duty_agent else False
        cross_shift = (canonical_agent != on_duty_agent) if on_duty_agent else False

        # Check SLA breach
        sla_breached = (response_mins > config.SLA_FRT_THRESHOLD_MINS) if response_mins is not None else False

        conn.execute("""
            UPDATE tickets SET
                first_responded_at = ?,
                response_time_mins = ?,
                agent_name = ?,
                agent_user_id = ?,
                on_duty_responded = ?,
                cross_shift_help = ?,
                sla_breached = ?
            WHERE ticket_id = ? AND first_responded_at IS NULL
        """, (
            responded_at_utc.isoformat(),
            response_mins,
            canonical_agent,
            str(agent_user_id),
            on_duty_responded,
            cross_shift,
            sla_breached,
            ticket_id,
        ))
        conn.commit()
        return True
    finally:
        conn.close()


def set_ticket_owner(ticket_id, owner_id):
    """Set the ticket owner (the user who opened the ticket)."""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE tickets SET ticket_owner = ? WHERE ticket_id = ?",
            (str(owner_id), ticket_id)
        )
        conn.commit()
    finally:
        conn.close()


def close_ticket(ticket_id, closed_at_utc, closed_by=None, closed_by_agent=None):
    """Record ticket closure."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT closed_at FROM tickets WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()

        if row and row['closed_at'] is not None:
            return  # Already closed

        if row:
            conn.execute("""
                UPDATE tickets SET
                    closed_at = ?,
                    closed_by = ?,
                    closed_by_agent = ?
                WHERE ticket_id = ?
            """, (
                closed_at_utc.isoformat(),
                closed_by,
                closed_by_agent,
                ticket_id,
            ))
        else:
            # Ticket we haven't seen — create a stub
            shift_label, on_duty_agent = config.get_on_duty_agent(closed_at_utc)
            on_duty_agent_id = config.get_agent_id_by_name(on_duty_agent)
            conn.execute("""
                INSERT OR IGNORE INTO tickets
                    (ticket_id, closed_at, closed_by, closed_by_agent,
                     on_duty_agent_id, on_duty_agent_name, shift_label)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                ticket_id,
                closed_at_utc.isoformat(),
                closed_by,
                closed_by_agent,
                on_duty_agent_id,
                on_duty_agent,
                shift_label,
            ))
        conn.commit()
    finally:
        conn.close()


def mark_deleted(ticket_id, deleted_at_utc):
    """Record ticket channel deletion."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()

        if row:
            updates = {"deleted_at": deleted_at_utc.isoformat()}
            if row['closed_at'] is None:
                updates["closed_at"] = deleted_at_utc.isoformat()
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE tickets SET {set_clause} WHERE ticket_id = ?",
                (*updates.values(), ticket_id)
            )
        else:
            shift_label, on_duty_agent = config.get_on_duty_agent(deleted_at_utc)
            on_duty_agent_id = config.get_agent_id_by_name(on_duty_agent)
            conn.execute("""
                INSERT OR IGNORE INTO tickets
                    (ticket_id, closed_at, deleted_at,
                     on_duty_agent_id, on_duty_agent_name, shift_label)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                ticket_id,
                deleted_at_utc.isoformat(),
                deleted_at_utc.isoformat(),
                on_duty_agent_id,
                on_duty_agent,
                shift_label,
            ))
        conn.commit()
    finally:
        conn.close()


FIRST_USER_MSG_MAX_CHARS = 200


def set_first_user_message(ticket_id, text):
    """
    Store the first user message for a ticket (used in SLA alerts).
    No-op if a message is already stored, or if text is empty.
    Trims to FIRST_USER_MSG_MAX_CHARS.
    """
    if not text:
        return False
    snippet = text.strip()
    if not snippet:
        return False
    if len(snippet) > FIRST_USER_MSG_MAX_CHARS:
        snippet = snippet[:FIRST_USER_MSG_MAX_CHARS - 1].rstrip() + '…'

    conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE tickets SET first_user_message = ? "
            "WHERE ticket_id = ? AND (first_user_message IS NULL OR first_user_message = '')",
            (snippet, ticket_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def mark_sla_alert_sent(ticket_id):
    """Mark that an SLA alert has been sent for this ticket (prevent duplicates)."""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE tickets SET sla_alert_sent = 1, sla_breached = 1 WHERE ticket_id = ?",
            (ticket_id,)
        )
        conn.commit()
    finally:
        conn.close()


def touch_user_msg(ticket_id, ts):
    """
    Record a new user (non-agent) message in an open ticket. Always updates
    last_user_msg_at. Also resets alert-dedupe flags so the SLA loop can
    re-evaluate against this fresh activity:
      - sla_alert_sent: only reset if no first agent reply yet (Phase 1).
      - followup_alert_sent: always reset (Phase 2 — agent has replied once
        and user is now pinging again).
    No-op if the ticket is closed.
    """
    conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE tickets "
            "SET last_user_msg_at = ?, "
            "    sla_alert_sent = CASE WHEN first_responded_at IS NULL "
            "                          THEN 0 ELSE sla_alert_sent END, "
            "    followup_alert_sent = 0 "
            "WHERE ticket_id = ? AND closed_at IS NULL",
            (ts.isoformat() if hasattr(ts, 'isoformat') else ts, ticket_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def mark_followup_alert_sent(ticket_id):
    """Mark that a follow-up SLA alert has been sent for this ticket."""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE tickets SET followup_alert_sent = 1 WHERE ticket_id = ?",
            (ticket_id,)
        )
        conn.commit()
    finally:
        conn.close()


# =====================================================
# QUERY HELPERS
# =====================================================

def get_open_tickets_needing_alert():
    """
    Find tickets that are still waiting for a first response,
    have exceeded the SLA threshold, and haven't been alerted yet.
    """
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT * FROM tickets
            WHERE first_responded_at IS NULL
              AND created_at IS NOT NULL
              AND closed_at IS NULL
              AND sla_alert_sent = 0
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_followup_breach_candidates():
    """
    Find tickets where:
      - the first agent reply has already happened, AND
      - the user has posted a newer message since the latest agent reply
        (or the ticket has no recorded agent activity timestamp), AND
      - we haven't sent a follow-up alert yet for this wait cycle.

    The caller compares (now - last_user_msg_at) against the SLA threshold
    to decide whether to alert.
    """
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT * FROM tickets
            WHERE first_responded_at IS NOT NULL
              AND closed_at IS NULL
              AND last_user_msg_at IS NOT NULL
              AND (last_agent_msg_at IS NULL OR last_user_msg_at > last_agent_msg_at)
              AND followup_alert_sent = 0
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_tickets_in_range(start_utc, end_utc):
    """Get all tickets created within a UTC time range."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT * FROM tickets
            WHERE created_at IS NOT NULL
              AND created_at >= ? AND created_at <= ?
            ORDER BY created_at
        """, (start_utc.isoformat(), end_utc.isoformat())).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_tickets_closed_in_range(start_utc, end_utc):
    """Get all tickets closed within a UTC time range."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT * FROM tickets
            WHERE closed_at IS NOT NULL
              AND closed_at >= ? AND closed_at <= ?
            ORDER BY closed_at
        """, (start_utc.isoformat(), end_utc.isoformat())).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_all_tickets():
    """Get all tickets."""
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM tickets ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_open_tickets():
    """Get tickets that are still open (no closed_at)."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT * FROM tickets
            WHERE closed_at IS NULL
              AND created_at IS NOT NULL
            ORDER BY created_at
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_ticket(ticket_id):
    """Get a single ticket by ID."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def upsert_ticket(data):
    """
    Insert or update a ticket from a dict. Used by migration and backfill.
    data must contain 'ticket_id'.
    """
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT * FROM tickets WHERE ticket_id = ?",
            (data['ticket_id'],)
        ).fetchone()

        if existing:
            # Only update fields that are non-null in data and null in DB
            updates = {}
            for key, val in data.items():
                if key == 'ticket_id':
                    continue
                if val is not None and val != '':
                    existing_val = existing[key] if key in existing.keys() else None
                    # Always update these fields, or update if existing is null
                    if key in ('on_duty_agent_name', 'shift_label', 'on_duty_responded',
                               'cross_shift_help', 'sla_breached') or existing_val is None:
                        updates[key] = val

            if updates:
                set_clause = ", ".join(f"{k} = ?" for k in updates)
                conn.execute(
                    f"UPDATE tickets SET {set_clause} WHERE ticket_id = ?",
                    (*updates.values(), data['ticket_id'])
                )
        else:
            cols = [k for k in data if data[k] is not None]
            placeholders = ", ".join("?" for _ in cols)
            col_names = ", ".join(cols)
            conn.execute(
                f"INSERT INTO tickets ({col_names}) VALUES ({placeholders})",
                [data[k] for k in cols]
            )
        conn.commit()
    finally:
        conn.close()


def count_tickets():
    """Return total ticket count."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT COUNT(*) as cnt FROM tickets").fetchone()
        return row['cnt']
    finally:
        conn.close()


# =====================================================
# COMMUNITY DIGEST STORAGE (Section 2)
# =====================================================

def save_community_digest(digest_date, channel, result):
    """Persist (or replace) one channel's daily digest JSON."""
    conn = get_connection()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO community_digests
                (digest_date, channel, digest_json, message_count, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            digest_date,
            channel,
            json.dumps(result),
            int(result.get('message_count', 0) or 0),
            datetime.now(timezone.utc).isoformat(),
        ))
        conn.commit()
    finally:
        conn.close()


def get_community_digests_in_range(start_date, end_date):
    """
    Return all stored digests whose digest_date is between start_date and
    end_date (inclusive, ISO YYYY-MM-DD). Each row: {digest_date, channel, digest}.
    """
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT digest_date, channel, digest_json
            FROM community_digests
            WHERE digest_date >= ? AND digest_date <= ?
            ORDER BY digest_date, channel
        """, (start_date, end_date)).fetchall()
        return [
            {
                'digest_date': r['digest_date'],
                'channel': r['channel'],
                'digest': json.loads(r['digest_json']),
            }
            for r in rows
        ]
    finally:
        conn.close()
