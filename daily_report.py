"""
daily_report.py

Generates a comprehensive daily support team performance report
with 6 sections matching the mechanism spec, and sends it to Telegram.

Sections:
  1. Overview (created, resolved, open, resolution rate, avg FRT)
  2. Agent Performance (on-shift tickets, responded, missed)
  3. Response Time Breakdown (fastest, slowest, mean, median, p90)
  4. SLA Compliance (overall + per-agent with breach IDs)
  5. Additional Insights (busiest hour, cross-shift help, volume trend)
  6. Open Tickets Requiring Attention
"""

import os
import requests
from datetime import datetime, timedelta, timezone
from collections import Counter

import config
import database


def generate_daily_report(target_date=None):
    """
    Generate the full 6-section daily report.
    target_date: a date object for the report day (default: yesterday in UTC).
    Returns a formatted string for Telegram.
    """
    database.init_db()

    if target_date is None:
        now_utc = datetime.now(tz=timezone.utc)
        # Report covers the previous 24h ending at current time
        end_utc = now_utc
        start_utc = end_utc - timedelta(hours=24)
    else:
        start_utc = datetime(target_date.year, target_date.month, target_date.day,
                             tzinfo=timezone.utc)
        end_utc = start_utc + timedelta(hours=24)

    # Fetch data
    created_tickets = database.get_tickets_in_range(start_utc, end_utc)
    closed_tickets = database.get_tickets_closed_in_range(start_utc, end_utc)
    open_tickets = database.get_open_tickets()

    # =====================================================
    # SECTION 1 — Overview
    # =====================================================
    total_created = len(created_tickets)
    total_resolved = len(closed_tickets)
    still_open = len(open_tickets)
    resolution_rate = (total_resolved / total_created * 100) if total_created > 0 else 0

    # Average FRT for tickets created in window that have a response
    frts = [t['response_time_mins'] for t in created_tickets
            if t['response_time_mins'] is not None]
    avg_frt = sum(frts) / len(frts) if frts else None

    local_start = start_utc.astimezone(config.LOCAL_TZ)
    local_end = end_utc.astimezone(config.LOCAL_TZ)

    lines = [
        f"📊 *Daily Support Report — {local_end.strftime('%b %d, %Y')}*",
        f"`{local_start.strftime('%Y-%m-%d %H:%M')} → {local_end.strftime('%Y-%m-%d %H:%M')} (UTC+7)`",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"📥 Tickets Created:   {total_created}",
        f"✅ Tickets Resolved:   {total_resolved}",
        f"⏳ Still Open:          {still_open}",
        f"📈 Resolution Rate:    {resolution_rate:.1f}%",
        f"⏱️ Avg Response Time:  {config.fmt_mins(avg_frt)}",
        "",
    ]

    # =====================================================
    # SECTION 2 — Agent Performance
    # =====================================================
    lines.append("*👤 Agent Performance*")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    agent_names = ['TerrorMichael', 'Mikaelson', 'Dablendo']
    for agent in agent_names:
        on_shift = [t for t in created_tickets if t['on_duty_agent_name'] == agent]
        responded = [t for t in on_shift if t['on_duty_responded']]
        missed = [t for t in on_shift
                  if t['first_responded_at'] is not None and not t['on_duty_responded']]
        no_response = [t for t in on_shift if t['first_responded_at'] is None]

        lines.append(
            f"• *{agent}*: {len(on_shift)} on-shift | "
            f"{len(responded)} responded | "
            f"{len(missed)} missed | "
            f"{len(no_response)} no reply"
        )

    # Reus (optional agent, no shift)
    reus_responses = [t for t in created_tickets if t['agent_name'] == 'Reus']
    if reus_responses:
        lines.append(f"• *Reus* (optional): {len(reus_responses)} responses (no assigned shift)")
    lines.append("")

    # =====================================================
    # SECTION 3 — Response Time Breakdown
    # =====================================================
    lines.append("*⏱️ Response Times*")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    responded_tickets = [t for t in created_tickets if t['response_time_mins'] is not None]

    if responded_tickets:
        # Fastest & Slowest
        fastest = min(responded_tickets, key=lambda t: t['response_time_mins'])
        slowest = max(responded_tickets, key=lambda t: t['response_time_mins'])

        sla_flag_slow = " ⚠️ SLA" if slowest['sla_breached'] else ""
        lines.append(
            f"🏆 Fastest: `{fastest['ticket_id']}` — "
            f"{config.fmt_mins(fastest['response_time_mins'])} "
            f"(by {fastest['agent_name']})"
        )
        lines.append(
            f"🐢 Slowest: `{slowest['ticket_id']}` — "
            f"{config.fmt_mins(slowest['response_time_mins'])} "
            f"(by {slowest['agent_name']}){sla_flag_slow}"
        )
        lines.append("")

        # Per-agent mean, median, p90
        from statistics import median as stat_median
        agent_frt = {}
        for t in responded_tickets:
            name = t['agent_name'] or 'Unknown'
            agent_frt.setdefault(name, []).append(t['response_time_mins'])

        lines.append("📊 *Mean FRT per Agent:*")
        for agent in sorted(agent_frt.keys()):
            vals = agent_frt[agent]
            mean_val = sum(vals) / len(vals)
            flag = " ✅" if mean_val <= config.SLA_FRT_THRESHOLD_MINS else " ⚠️"
            lines.append(f"   {agent}: {config.fmt_mins(mean_val)}{flag}")

        lines.append("📊 *Median FRT per Agent:*")
        for agent in sorted(agent_frt.keys()):
            vals = sorted(agent_frt[agent])
            med_val = stat_median(vals)
            lines.append(f"   {agent}: {config.fmt_mins(med_val)}")

        lines.append("📊 *P90 FRT per Agent:*")
        for agent in sorted(agent_frt.keys()):
            vals = sorted(agent_frt[agent])
            p90_idx = int(len(vals) * 0.9)
            p90_val = vals[min(p90_idx, len(vals) - 1)]
            lines.append(f"   {agent}: {config.fmt_mins(p90_val)}")
    else:
        lines.append("No responses recorded in this period.")
    lines.append("")

    # =====================================================
    # SECTION 4 — SLA Compliance
    # =====================================================
    lines.append(f"*🚦 SLA Compliance* (target: ≤ {config.SLA_FRT_THRESHOLD_MINS} min FRT)")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    if responded_tickets:
        compliant = [t for t in responded_tickets if not t['sla_breached']]
        breached = [t for t in responded_tickets if t['sla_breached']]
        total_resp = len(responded_tickets)

        lines.append(
            f"Overall: {len(compliant)}/{total_resp} "
            f"({len(compliant)/total_resp*100:.1f}%)"
        )

        # Per-agent SLA
        agent_tickets = {}
        for t in responded_tickets:
            name = t['agent_name'] or 'Unknown'
            agent_tickets.setdefault(name, []).append(t)

        for agent in sorted(agent_tickets.keys()):
            tickets_list = agent_tickets[agent]
            ok = [t for t in tickets_list if not t['sla_breached']]
            bad = [t for t in tickets_list if t['sla_breached']]
            pct = len(ok) / len(tickets_list) * 100 if tickets_list else 0

            star = " ⭐" if pct == 100 else ""
            breach_info = ""
            if bad:
                breach_ids = ", ".join(
                    f"{t['ticket_id']} ({config.fmt_mins(t['response_time_mins'])})"
                    for t in bad[:3]  # Show max 3 breaches
                )
                breach_info = f" — breaches: {breach_ids}"

            lines.append(
                f"   {agent}: {len(ok)}/{len(tickets_list)} "
                f"({pct:.0f}%){star}{breach_info}"
            )
    else:
        lines.append("No responses to evaluate.")
    lines.append("")

    # =====================================================
    # SECTION 5 — Additional Insights
    # =====================================================
    lines.append("*🔍 Insights*")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # Busiest / quietest hour
    if created_tickets:
        hour_counts = Counter()
        for t in created_tickets:
            if t['created_at']:
                dt = datetime.fromisoformat(t['created_at'])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                hour_counts[dt.hour] += 1

        if hour_counts:
            busiest_h = hour_counts.most_common(1)[0]
            quietest_h = hour_counts.most_common()[-1]
            lines.append(
                f"📈 Busiest Hour:  {busiest_h[0]:02d}:00–{busiest_h[0]+1:02d}:00 UTC "
                f"({busiest_h[1]} tickets)"
            )
            lines.append(
                f"📉 Quietest Hour: {quietest_h[0]:02d}:00–{quietest_h[0]+1:02d}:00 UTC "
                f"({quietest_h[1]} tickets)"
            )

    # Cross-shift help
    cross_shift_tickets = [t for t in created_tickets if t['cross_shift_help']]
    if cross_shift_tickets:
        cross_details = []
        for t in cross_shift_tickets[:3]:
            cross_details.append(
                f"{t['agent_name']} covered for {t['on_duty_agent_name']} "
                f"({t['ticket_id']})"
            )
        lines.append(f"🔄 Cross-Shift Help: {len(cross_shift_tickets)} ticket(s)")
        for d in cross_details:
            lines.append(f"   • {d}")
    else:
        lines.append("🔄 Cross-Shift Help: 0")

    # Tickets closed by whom
    agent_closed = [t for t in closed_tickets if t['closed_by_agent']]
    user_closed = [t for t in closed_tickets if t['closed_by_agent'] == 0]
    lines.append(f"👤 Closed by agent: {len(agent_closed)} | "
                 f"Closed by user: {len(user_closed)}")

    # Repeat users
    owners = [t['ticket_owner'] for t in created_tickets if t['ticket_owner']]
    owner_counts = Counter(owners)
    repeat_users = {uid: cnt for uid, cnt in owner_counts.items() if cnt >= 2}
    if repeat_users:
        lines.append(f"🔁 Repeat Users: {len(repeat_users)} user(s) opened 2+ tickets")

    # Volume vs 7-day average
    seven_days_ago = start_utc - timedelta(days=7)
    week_tickets = database.get_tickets_in_range(seven_days_ago, start_utc)
    if week_tickets:
        avg_daily = len(week_tickets) / 7
        if avg_daily > 0:
            pct_change = ((total_created - avg_daily) / avg_daily) * 100
            arrow = "▲" if pct_change >= 0 else "▼"
            lines.append(
                f"📊 Volume: {total_created} today | "
                f"7-day avg: {avg_daily:.1f} | "
                f"{arrow} {abs(pct_change):.0f}%"
            )
    lines.append("")

    # =====================================================
    # SECTION 6 — Open Tickets Requiring Attention
    # =====================================================
    if open_tickets:
        lines.append("*⏳ Open Tickets (Needs Action)*")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

        now_utc = datetime.now(tz=timezone.utc)
        for t in open_tickets[:10]:  # Show max 10
            if not t['created_at']:
                continue
            created = datetime.fromisoformat(t['created_at'])
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            open_mins = (now_utc - created).total_seconds() / 60

            if t['first_responded_at'] is None and open_mins > config.SLA_FRT_THRESHOLD_MINS:
                icon = "🔴"
                status = "No response"
            elif t['first_responded_at'] is None:
                icon = "🟡"
                status = "No response yet (within SLA)"
            else:
                icon = "🟡"
                status = "Responded, awaiting resolution"

            on_duty = t.get('on_duty_agent_name', 'N/A')
            lines.append(
                f"{icon} `{t['ticket_id']}` — Open {config.fmt_mins(open_mins)} — "
                f"{status} — On-duty: {on_duty}"
            )
        lines.append("")

    lines.append("_KyberSwap Support Analytics_")
    return "\n".join(lines)


# =====================================================
# TELEGRAM DELIVERY
# =====================================================

def send_telegram_message(text):
    """Send a message to the configured Telegram group."""
    token = config.TELEGRAM_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID

    if not token or not chat_id:
        print("⚠️  TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set.")
        return

    thread_id = None
    if '/' in chat_id:
        parts = chat_id.split('/')
        chat_id, thread_id = parts[0], parts[1]
    if not chat_id.startswith('-'):
        chat_id = f"-100{chat_id}"

    # Split long messages (Telegram limit: 4096 chars)
    chunks = []
    if len(text) <= 4096:
        chunks = [text]
    else:
        current = ""
        for line in text.split("\n"):
            if len(current) + len(line) + 1 > 4000:
                chunks.append(current)
                current = line + "\n"
            else:
                current += line + "\n"
        if current:
            chunks.append(current)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for i, chunk in enumerate(chunks):
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        if thread_id:
            payload["message_thread_id"] = thread_id

        try:
            r = requests.post(url, json=payload)
            r.raise_for_status()
            if i == len(chunks) - 1:
                print("✅ Telegram daily report sent successfully!")
        except Exception as e:
            print(f"❌ Failed to send Telegram report: {e}")


def send_report():
    """Generate and send the daily report."""
    report_text = generate_daily_report()
    send_telegram_message(report_text)


if __name__ == '__main__':
    send_report()
