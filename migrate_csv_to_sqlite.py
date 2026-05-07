"""
migrate_csv_to_sqlite.py

One-time migration script: reads ticket_analytics.csv,
normalizes data, retroactively computes shift/on-duty fields,
and inserts everything into the SQLite database.
"""

import csv
import os
from datetime import datetime, timezone
import config
import database


def migrate():
    csv_path = config.CSV_FILE
    if not os.path.exists(csv_path):
        print(f"❌ {csv_path} not found. Nothing to migrate.")
        return

    # Initialize the database schema
    database.init_db()

    # Check if DB already has data
    existing_count = database.count_tickets()
    if existing_count > 0:
        print(f"⚠️  Database already has {existing_count} tickets. Skipping migration.")
        print("   Delete tickets.db first if you want to re-migrate.")
        return

    # Read CSV with raw strings to preserve precision on IDs
    rows = []
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    print(f"📂 Read {len(rows)} rows from {csv_path}")

    migrated = 0
    skipped = 0

    for row in rows:
        ticket_id = row.get('Ticket ID', '').strip()
        if not ticket_id:
            skipped += 1
            continue

        # Parse timestamps
        created_at = _clean_ts(row.get('Created At'))
        first_responded_at = _clean_ts(row.get('First Responded At'))
        closed_at = _clean_ts(row.get('Closed At'))
        deleted_at = _clean_ts(row.get('Deleted At'))

        # Parse response time
        rt_str = row.get('Response Time (Mins)', '').strip()
        response_time_mins = None
        if rt_str and rt_str != 'nan':
            try:
                response_time_mins = round(float(rt_str), 2)
            except ValueError:
                pass

        # Normalize agent name
        agent_name_raw = row.get('Agent Name', '').strip()
        agent_name = config.normalize_agent(agent_name_raw) if agent_name_raw and agent_name_raw != 'nan' else None

        # Clean agent user ID (handle float notation like "8.82e+17")
        agent_uid_raw = row.get('Agent User ID', '').strip()
        agent_user_id = _clean_id(agent_uid_raw)

        # Clean ticket owner
        owner_raw = row.get('Ticket Owner', '').strip()
        ticket_owner = _clean_id(owner_raw)

        # Clean closed by
        closed_by_raw = row.get('Closed By', '').strip()
        closed_by = closed_by_raw if closed_by_raw and closed_by_raw != 'nan' else None

        # Closed by agent flag
        cba_raw = row.get('Closed by Agent', '').strip()
        closed_by_agent = None
        if cba_raw.lower() == 'true':
            closed_by_agent = True
        elif cba_raw.lower() == 'false':
            closed_by_agent = False

        # Compute shift/on-duty retroactively from created_at
        created_dt = _parse_dt_utc(created_at)
        shift_label, on_duty_agent = config.get_on_duty_agent(created_dt)

        # Determine if on-duty agent responded
        on_duty_responded = False
        cross_shift_help = False
        if agent_name and on_duty_agent:
            on_duty_responded = (agent_name == on_duty_agent)
            cross_shift_help = (agent_name != on_duty_agent)

        # SLA breach
        sla_breached = False
        if response_time_mins is not None:
            sla_breached = response_time_mins > config.SLA_FRT_THRESHOLD_MINS

        data = {
            'ticket_id': ticket_id,
            'created_at': created_at,
            'first_responded_at': first_responded_at,
            'response_time_mins': response_time_mins,
            'agent_name': agent_name,
            'agent_user_id': agent_user_id,
            'ticket_owner': ticket_owner,
            'closed_by': closed_by,
            'closed_by_agent': closed_by_agent,
            'closed_at': closed_at,
            'deleted_at': deleted_at,
            'on_duty_agent_name': on_duty_agent,
            'on_duty_responded': on_duty_responded,
            'sla_breached': sla_breached,
            'sla_alert_sent': 0,
            'cross_shift_help': cross_shift_help,
            'shift_label': shift_label,
        }

        database.upsert_ticket(data)
        migrated += 1

    final_count = database.count_tickets()
    print(f"✅ Migration complete!")
    print(f"   Migrated: {migrated} | Skipped: {skipped}")
    print(f"   Total tickets in DB: {final_count}")
    print(f"   Original CSV kept as backup: {csv_path}")


def _clean_ts(val):
    """Clean a timestamp value from CSV."""
    if not val or val.strip() in ('', 'nan', 'None'):
        return None
    return val.strip()


def _clean_id(val):
    """Clean a numeric ID from CSV, handling float notation."""
    if not val or val.strip() in ('', 'nan', 'None'):
        return None
    val = val.strip()
    # Handle scientific notation (e.g., "5.551368391162593e+17")
    try:
        if 'e+' in val or 'E+' in val or '.' in val:
            return str(int(float(val)))
        return val
    except (ValueError, OverflowError):
        return val


def _parse_dt_utc(ts_str):
    """Parse an ISO timestamp string into a UTC-aware datetime."""
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


if __name__ == '__main__':
    migrate()
