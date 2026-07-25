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
from pathlib import Path

import config
import database
import metrics

# Success marker for safety-net check in bot.py — see _safety_net_loop().
# bot.py looks for this file each day after the cron window; if missing,
# it reruns the report. Format: logs/.markers/daily_report.success.<UTC date>
MARKER_DIR = Path(__file__).resolve().parent / 'logs' / '.markers'


def _touch_marker():
    MARKER_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(tz=timezone.utc).date().isoformat()
    (MARKER_DIR / f'daily_report.success.{today}').touch()


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
    esc = config.html_escape

    lines = [
        f"📊 <b>Daily Support Report — {esc(local_end.strftime('%b %d, %Y'))}</b>",
        f"<code>{esc(local_start.strftime('%Y-%m-%d %H:%M'))} → "
        f"{esc(local_end.strftime('%Y-%m-%d %H:%M'))} (UTC+7)</code>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"📥 Tickets Created:   {total_created}",
        f"✅ Tickets Resolved:   {total_resolved}",
        f"⏳ Still Open:          {still_open}",
        f"📈 Resolution Rate:    {resolution_rate:.1f}%",
        f"⏱️ Avg Response Time:  {esc(config.fmt_mins(avg_frt))}",
        "",
    ]

    # =====================================================
    # SECTION 2 — Agent Performance (FRT split applies)
    # =====================================================
    lines.append("<b>👤 Agent Performance</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # Per-agent split aggregation — accounts for cross-shift handoffs.
    # An agent's "missed" counts ticket-segments waiting on their watch.
    # Waive the miss for an on-duty agent who actually replied to the ticket
    # (worked it, just not first to touch) — same rule as the dashboard.
    responders_map = metrics.responders_by_ticket(
        database.get_response_events_in_range(start_utc, end_utc))
    per_agent = metrics.aggregate_per_agent(
        created_tickets, responded_agents_by_ticket=responders_map)
    agent_names = ['TerrorMichael', 'Mikaelson', 'Dablendo']
    for agent in agent_names:
        a = per_agent.get(agent, {})
        on_shift = a.get('on_shift', 0)
        no_response = sum(
            1 for t in created_tickets
            if t['on_duty_agent_name'] == agent
            and t['first_responded_at'] is None
        )
        # 'covered' = handled by another agent inside this shift — not a miss,
        # but the wait time still lands on the on-duty agent's FRT.
        covered_txt = (
            f"{a.get('covered', 0)} covered | " if a.get('covered', 0) else ""
        )
        lines.append(
            f"• <b>{esc(agent)}</b>: {on_shift} on-shift | "
            f"{a.get('responded', 0)} responded | "
            f"{covered_txt}"
            f"{a.get('missed', 0)} missed | "
            f"{no_response} no reply | "
            f"{a.get('cross_help', 0)} cross-help"
        )

    # Reus (optional agent, no shift) — his help always lands inside someone
    # else's shift, so it shows up as 'covering' (same shift) or 'cross_help'
    # (after a boundary). Count both.
    reus = per_agent.get('Reus')
    reus_help = (reus or {}).get('cross_help', 0) + (reus or {}).get('covering', 0)
    if reus_help:
        lines.append(
            f"• <b>Reus</b> (optional): {reus_help} cross-help responses"
        )
    lines.append("")

    # =====================================================
    # SECTION 3 — Response Time Breakdown
    # =====================================================
    lines.append("<b>⏱️ Response Times</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    responded_tickets = [t for t in created_tickets if t['response_time_mins'] is not None]

    if responded_tickets:
        # Fastest & Slowest
        fastest = min(responded_tickets, key=lambda t: t['response_time_mins'])
        slowest = max(responded_tickets, key=lambda t: t['response_time_mins'])

        sla_flag_slow = " ⚠️ SLA" if slowest['sla_breached'] else ""
        lines.append(
            f"🏆 Fastest: <code>{esc(fastest['ticket_id'])}</code> — "
            f"{esc(config.fmt_mins(fastest['response_time_mins']))} "
            f"(by {esc(fastest['agent_name'])})"
        )
        lines.append(
            f"🐢 Slowest: <code>{esc(slowest['ticket_id'])}</code> — "
            f"{esc(config.fmt_mins(slowest['response_time_mins']))} "
            f"(by {esc(slowest['agent_name'])}){sla_flag_slow}"
        )
        lines.append("")

        # Per-agent mean / median / p90 — using FRT split. Each agent's
        # pool includes BOTH their own response time AND time the user
        # waited on their watch when someone else ended up responding.
        lines.append("📊 <b>Mean FRT per Agent</b> (shift-split):")
        for agent in sorted(per_agent.keys()):
            s = metrics.summarize(per_agent[agent])
            if s['avg_frt'] is None:
                continue
            flag = " ✅" if s['avg_frt'] <= config.SLA_FRT_THRESHOLD_MINS else " ⚠️"
            lines.append(f"   {esc(agent)}: {esc(config.fmt_mins(s['avg_frt']))}{flag}")

        lines.append("📊 <b>Median FRT per Agent</b> (shift-split):")
        for agent in sorted(per_agent.keys()):
            s = metrics.summarize(per_agent[agent])
            if s['median_frt'] is None:
                continue
            lines.append(f"   {esc(agent)}: {esc(config.fmt_mins(s['median_frt']))}")

        lines.append("📊 <b>P90 FRT per Agent</b> (shift-split):")
        for agent in sorted(per_agent.keys()):
            s = metrics.summarize(per_agent[agent])
            if s['p90_frt'] is None:
                continue
            lines.append(f"   {esc(agent)}: {esc(config.fmt_mins(s['p90_frt']))}")
    else:
        lines.append("No responses recorded in this period.")
    lines.append("")

    # =====================================================
    # SECTION 4 — SLA Compliance
    # =====================================================
    lines.append(f"<b>🚦 SLA Compliance</b> (target: ≤ {config.SLA_FRT_THRESHOLD_MINS} min FRT)")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    if responded_tickets:
        compliant = [t for t in responded_tickets if not t['sla_breached']]
        breached = [t for t in responded_tickets if t['sla_breached']]
        total_resp = len(responded_tickets)

        lines.append(
            f"Overall: {len(compliant)}/{total_resp} "
            f"({len(compliant)/total_resp*100:.1f}%)"
        )

        # Per-agent SLA — based on per-segment contributions vs threshold.
        # A "breach" here means a single shift's contribution exceeded
        # SLA_FRT_THRESHOLD_MINS, not necessarily the whole-ticket FRT.
        for agent in sorted(per_agent.keys()):
            s = metrics.summarize(per_agent[agent])
            total = s['count_with_frt']
            if total == 0:
                continue
            breaches = s['breaches']
            ok = total - len(breaches)
            pct = ok / total * 100
            star = " ⭐" if pct == 100 else ""
            breach_info = ""
            if breaches:
                breach_ids = ", ".join(
                    f"{esc(tid)} ({esc(config.fmt_mins(mins))})"
                    for tid, mins in breaches[:3]
                )
                breach_info = f" — breaches: {breach_ids}"
            lines.append(
                f"   {esc(agent)}: {ok}/{total} ({pct:.0f}%){star}{breach_info}"
            )
    else:
        lines.append("No responses to evaluate.")
    lines.append("")

    # =====================================================
    # SECTION 5 — Additional Insights
    # =====================================================
    lines.append("<b>🔍 Insights</b>")
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
                f"{esc(t['agent_name'])} covered for {esc(t['on_duty_agent_name'])} "
                f"({esc(t['ticket_id'])})"
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
        lines.append("<b>⏳ Open Tickets (Needs Action)</b>")
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

            on_duty = t.get('on_duty_agent_name') or 'N/A'
            lines.append(
                f"{icon} <code>{esc(t['ticket_id'])}</code> — "
                f"Open {esc(config.fmt_mins(open_mins))} — "
                f"{esc(status)} — On-duty: {esc(on_duty)}"
            )
        lines.append("")

    lines.append("<i>KyberSwap Support Analytics</i>")
    return "\n".join(lines)


# =====================================================
# TELEGRAM DELIVERY
# =====================================================

def send_telegram_message(text):
    """Send a message to every configured Telegram chat."""
    token = config.TELEGRAM_TOKEN
    chat_ids = config.TELEGRAM_CHAT_IDS

    if not token or not chat_ids:
        print("⚠️  TELEGRAM_TOKEN or TELEGRAM_CHAT_ID(s) not set.")
        return

    # Split long messages once (Telegram limit: 4096 chars)
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
    all_ok = True
    for raw_chat_id in chat_ids:
        chat_id = raw_chat_id
        thread_id = None
        if '/' in chat_id:
            parts = chat_id.split('/')
            chat_id, thread_id = parts[0], parts[1]
        if not chat_id.startswith('-'):
            chat_id = f"-100{chat_id}"

        for i, chunk in enumerate(chunks):
            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if thread_id:
                payload["message_thread_id"] = thread_id

            try:
                r = requests.post(url, json=payload, timeout=15)
                r.raise_for_status()
                if i == len(chunks) - 1:
                    print(f"✅ Telegram daily report sent to {chat_id}")
            except Exception as e:
                all_ok = False
                print(f"❌ Failed to send Telegram report to {chat_id}: {e}")
    return all_ok


def _build_snapshot(end_utc):
    """Compute a small headline snapshot for the daily_reports archive.
    Heavy computation (per-agent, per-product) is done LIVE by the dashboard
    API; this snapshot is for week-over-week comparison only."""
    start_utc = end_utc - timedelta(hours=24)
    created = database.get_tickets_in_range(start_utc, end_utc)
    closed = database.get_tickets_closed_in_range(start_utc, end_utc)
    frts = [t['response_time_mins'] for t in created
            if t['response_time_mins'] is not None]
    avg_frt = round(sum(frts) / len(frts), 2) if frts else None
    breaches = sum(1 for t in created if t.get('sla_breached'))
    responded = [t for t in created if t['response_time_mins'] is not None]
    sla_compliance = (
        round((1 - breaches / len(responded)) * 100, 2)
        if responded else None
    )
    return {
        'window_start_utc': start_utc.isoformat(),
        'window_end_utc': end_utc.isoformat(),
        'total_created': len(created),
        'total_resolved': len(closed),
        'still_open': len(database.get_open_tickets()),
        'avg_frt_mins': avg_frt,
        'sla_breaches': breaches,
        'sla_compliance_pct': sla_compliance,
    }


def send_report():
    """Generate and send the daily report. Touches a success marker so
    bot.py's safety-net check knows today's job already completed. Also
    archives a headline snapshot to daily_reports for historical view."""
    end_utc = datetime.now(tz=timezone.utc)
    report_text = generate_daily_report()
    if send_telegram_message(report_text):
        _touch_marker()
        try:
            snapshot = _build_snapshot(end_utc)
            database.save_daily_report(end_utc.date().isoformat(), snapshot)
        except Exception as e:
            print(f"⚠️  Snapshot save failed (non-fatal): {e}")


if __name__ == '__main__':
    send_report()
