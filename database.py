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
            followup_alert_sent   BOOLEAN DEFAULT 0,
            conversation_excerpt  TEXT,
            product_group         TEXT,
            product_subcategory   TEXT,
            category_source       TEXT,
            category_confidence   REAL,
            classified_at         TEXT
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
        ('conversation_excerpt', 'TEXT'),
        ('product_group',        'TEXT'),
        ('product_subcategory',  'TEXT'),
        ('category_source',      'TEXT'),
        ('category_confidence',  'REAL'),
        ('classified_at',        'TEXT'),
        ('pending_user_msg_at',  'TEXT'),
    ):
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE tickets ADD COLUMN {col} {ddl}")

    # Per-message response events: one row per (user_msg → agent_reply) gap.
    # Used to compute avg response time across both first and follow-ups,
    # which the rolling last_*_at columns alone cannot reconstruct.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ticket_response_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id       TEXT NOT NULL,
            user_msg_at     TEXT NOT NULL,
            agent_msg_at    TEXT NOT NULL,
            response_mins   REAL NOT NULL,
            agent_name      TEXT,
            agent_user_id   TEXT,
            on_duty_agent   TEXT,
            on_duty_shift   TEXT,
            event_type      TEXT NOT NULL,
            sla_breached    BOOLEAN DEFAULT 0,
            created_at      TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tre_ticket "
        "ON ticket_response_events(ticket_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tre_agent_time "
        "ON ticket_response_events(agent_name, agent_msg_at)"
    )

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

    # Daily report snapshots — JSON of computed metrics (per-agent, per-product,
    # SLA, etc.) saved by daily_report.py after it sends to Telegram. Used by
    # the dashboard to render historical data without re-computing.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_reports (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            report_date TEXT NOT NULL UNIQUE,
            report_json TEXT NOT NULL,
            created_at  TEXT NOT NULL
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

    Also inserts a row into ticket_response_events when there is a pending
    user wait being closed (first response or a follow-up). The event's
    user_msg_at is:
      - created_at for the first response (matches the FRT clock used in
        metrics.contributions_for_ticket), or
      - pending_user_msg_at for follow-ups (set by touch_user_msg on the
        first user message of the current wait cycle).
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()

        if not row:
            return False

        canonical_agent = config.normalize_agent(agent_name)
        is_first = row['first_responded_at'] is None

        # Resolve the wait-clock start for this event.
        wait_start_str = row['created_at'] if is_first else row['pending_user_msg_at']
        if wait_start_str:
            wait_start = datetime.fromisoformat(wait_start_str)
            if wait_start.tzinfo is None:
                wait_start = wait_start.replace(tzinfo=timezone.utc)
            if wait_start < responded_at_utc:
                gap_mins = round((responded_at_utc - wait_start).total_seconds() / 60, 2)
                shift_label, on_duty_at_reply = config.get_on_duty_agent(responded_at_utc)
                conn.execute(
                    "INSERT INTO ticket_response_events "
                    "(ticket_id, user_msg_at, agent_msg_at, response_mins, "
                    " agent_name, agent_user_id, on_duty_agent, on_duty_shift, "
                    " event_type, sla_breached, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ticket_id,
                        wait_start.isoformat(),
                        responded_at_utc.isoformat(),
                        gap_mins,
                        canonical_agent,
                        str(agent_user_id),
                        on_duty_at_reply,
                        shift_label,
                        'first' if is_first else 'followup',
                        1 if gap_mins > config.SLA_FRT_THRESHOLD_MINS else 0,
                        datetime.now(tz=timezone.utc).isoformat(),
                    ),
                )

        # Always log agent activity, reset follow-up dedupe, clear pending wait.
        conn.execute(
            "UPDATE tickets SET last_agent_msg_at = ?, followup_alert_sent = 0, "
            "                   pending_user_msg_at = NULL "
            "WHERE ticket_id = ?",
            (responded_at_utc.isoformat(), ticket_id),
        )

        if not is_first:
            conn.commit()
            return False  # Already has a first response — event row above did run

        # Compute response time
        response_mins = None
        if row['created_at']:
            created = datetime.fromisoformat(row['created_at'])
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            diff = responded_at_utc - created
            response_mins = round(diff.total_seconds() / 60, 2)

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
    Sticky-sets pending_user_msg_at to the FIRST user message of the current
    wait cycle (only when previously NULL). record_response uses this as the
    clock start for follow-up response events.
    No-op if the ticket is closed.
    """
    ts_iso = ts.isoformat() if hasattr(ts, 'isoformat') else ts
    conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE tickets "
            "SET last_user_msg_at = ?, "
            "    pending_user_msg_at = COALESCE(pending_user_msg_at, ?), "
            "    sla_alert_sent = CASE WHEN first_responded_at IS NULL "
            "                          THEN 0 ELSE sla_alert_sent END, "
            "    followup_alert_sent = 0 "
            "WHERE ticket_id = ? AND closed_at IS NULL",
            (ts_iso, ts_iso, ticket_id),
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


def delete_response_events_for_ticket(ticket_id):
    """Remove all response events for one ticket. Used by backfill to
    achieve idempotent re-runs."""
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM ticket_response_events WHERE ticket_id = ?",
            (ticket_id,),
        )
        conn.commit()
    finally:
        conn.close()


def insert_response_event(ticket_id, user_msg_at, agent_msg_at, agent_name,
                          agent_user_id, event_type):
    """Insert one (user_msg → agent_reply) gap row. Computes response_mins,
    on-duty agent/shift at agent_msg_at, and SLA breach flag internally."""
    def _to_dt(v):
        if hasattr(v, 'isoformat'):
            return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        dt = datetime.fromisoformat(v)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    user_dt = _to_dt(user_msg_at)
    agent_dt = _to_dt(agent_msg_at)
    gap_mins = round((agent_dt - user_dt).total_seconds() / 60, 2)
    shift_label, on_duty = config.get_on_duty_agent(agent_dt)

    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO ticket_response_events "
            "(ticket_id, user_msg_at, agent_msg_at, response_mins, "
            " agent_name, agent_user_id, on_duty_agent, on_duty_shift, "
            " event_type, sla_breached, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ticket_id,
                user_dt.isoformat(),
                agent_dt.isoformat(),
                gap_mins,
                agent_name,
                agent_user_id,
                on_duty,
                shift_label,
                event_type,
                1 if gap_mins > config.SLA_FRT_THRESHOLD_MINS else 0,
                datetime.now(tz=timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_response_events_in_range(start_utc, end_utc, agent=None):
    """Return ticket_response_events whose agent_msg_at falls in [start, end].
    Optional agent filter (matches normalized name)."""
    conn = get_connection()
    try:
        sql = (
            "SELECT * FROM ticket_response_events "
            "WHERE agent_msg_at >= ? AND agent_msg_at <= ?"
        )
        params = [start_utc.isoformat(), end_utc.isoformat()]
        if agent:
            sql += " AND agent_name = ?"
            params.append(agent)
        sql += " ORDER BY agent_msg_at"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
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


def get_latest_community_digest_date():
    """Return the most recent digest_date present in community_digests, or
    None. Used by the dashboard to pick which day's data to render."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT digest_date FROM community_digests "
            "ORDER BY digest_date DESC LIMIT 1"
        ).fetchone()
        return row['digest_date'] if row else None
    finally:
        conn.close()


# =====================================================
# DAILY REPORT SNAPSHOTS (dashboard)
# =====================================================

def save_daily_report(report_date, payload):
    """Persist (or replace) one day's computed daily-report JSON."""
    conn = get_connection()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO daily_reports
                (report_date, report_json, created_at)
            VALUES (?, ?, ?)
        """, (
            report_date,
            json.dumps(payload),
            datetime.now(timezone.utc).isoformat(),
        ))
        conn.commit()
    finally:
        conn.close()


def get_daily_report(report_date):
    """Return parsed report_json for a given date, or None."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT report_json FROM daily_reports WHERE report_date = ?",
            (report_date,),
        ).fetchone()
        return json.loads(row['report_json']) if row else None
    finally:
        conn.close()


# =====================================================
# PRODUCT CATEGORY (LLM-classified)
# =====================================================

def get_tickets_needing_classification(limit=20):
    """Return tickets that have some text content but no product_group yet.
    The classify loop sends these to Gemini in batches.
    """
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT ticket_id, first_user_message, conversation_excerpt
            FROM tickets
            WHERE product_group IS NULL
              AND (
                (first_user_message IS NOT NULL AND length(first_user_message) >= 8)
                OR
                (conversation_excerpt IS NOT NULL AND length(conversation_excerpt) >= 8)
              )
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def save_ticket_classification(ticket_id, group, subcategory, source, confidence):
    """Persist the LLM's classification for one ticket. Idempotent — overwrites."""
    conn = get_connection()
    try:
        conn.execute("""
            UPDATE tickets
            SET product_group       = ?,
                product_subcategory = ?,
                category_source     = ?,
                category_confidence = ?,
                classified_at       = ?
            WHERE ticket_id = ?
        """, (
            group, subcategory, source, confidence,
            datetime.now(timezone.utc).isoformat(),
            ticket_id,
        ))
        conn.commit()
    finally:
        conn.close()


def save_conversation_excerpt(ticket_id, excerpt):
    """Update conversation_excerpt (concat of first few user msgs) if absent
    or shorter than the new value. No-op if the new excerpt is empty."""
    if not excerpt:
        return
    conn = get_connection()
    try:
        conn.execute("""
            UPDATE tickets
            SET conversation_excerpt = ?
            WHERE ticket_id = ?
              AND (conversation_excerpt IS NULL
                   OR length(conversation_excerpt) < length(?))
        """, (excerpt, ticket_id, excerpt))
        conn.commit()
    finally:
        conn.close()
