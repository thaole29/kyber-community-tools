"""
weekly_report.py

Generates a weekly support team performance summary report
and sends it to Telegram. Designed to run every Monday at 00:00 UTC.

Includes:
  - Week-over-week FRT trends per agent
  - Shift load balancing analysis
  - SLA compliance trend
  - Cross-shift help summary
"""

import os
import requests
from datetime import datetime, timedelta, timezone
from collections import Counter
from statistics import median as stat_median

import config
import database
import metrics


def generate_weekly_report(end_date=None):
    """
    Generate a weekly summary report covering the last 7 days.
    end_date: a UTC datetime for the end of the report window (default: now).
    Returns a formatted string for Telegram.
    """
    database.init_db()

    if end_date is None:
        end_utc = datetime.now(tz=timezone.utc)
    else:
        end_utc = end_date

    start_utc = end_utc - timedelta(days=7)

    # Also fetch previous week for comparison
    prev_start = start_utc - timedelta(days=7)
    prev_end = start_utc

    # Fetch data
    this_week = database.get_tickets_in_range(start_utc, end_utc)
    prev_week = database.get_tickets_in_range(prev_start, prev_end)
    closed_this_week = database.get_tickets_closed_in_range(start_utc, end_utc)

    local_start = start_utc.astimezone(config.LOCAL_TZ)
    local_end = end_utc.astimezone(config.LOCAL_TZ)

    esc = config.html_escape
    lines = [
        "📊 <b>Weekly Support Report</b>",
        f"<code>{esc(local_start.strftime('%b %d'))} → "
        f"{esc(local_end.strftime('%b %d, %Y'))} (UTC+7)</code>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    # =====================================================
    # SECTION 1 — Volume Overview
    # =====================================================
    total_created = len(this_week)
    total_resolved = len(closed_this_week)
    prev_created = len(prev_week)

    vol_change = ""
    if prev_created > 0:
        pct = ((total_created - prev_created) / prev_created) * 100
        arrow = "▲" if pct >= 0 else "▼"
        vol_change = f" ({arrow} {abs(pct):.0f}% vs last week)"

    lines.append("<b>📥 Volume</b>")
    lines.append(f"  Created:  {total_created}{vol_change}")
    lines.append(f"  Resolved: {total_resolved}")
    # No "Open:" line — the backlog is all-time, not scoped to this window,
    # so it belongs on the dashboard rather than in the post (same rule as
    # daily_report, user 2026-07-25).
    lines.append("")

    # Daily breakdown
    lines.append("  Daily breakdown:")
    for day_offset in range(7):
        day_start = start_utc + timedelta(days=day_offset)
        day_end = day_start + timedelta(days=1)
        day_tickets = [t for t in this_week
                       if t['created_at'] and day_start.isoformat() <= t['created_at'] < day_end.isoformat()]
        day_label = day_start.astimezone(config.LOCAL_TZ).strftime('%a %b %d')
        lines.append(f"    {esc(day_label)}: {len(day_tickets)} tickets")
    lines.append("")

    # =====================================================
    # SECTION 2 — Agent FRT Trends (This Week vs Last Week)
    # =====================================================
    lines.append("<b>⏱️ Agent FRT Trends (This Week vs Last Week)</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    agents = ['Dablendo', 'Mikaelson', 'TerrorMichael', 'Reus']

    # Shift-split aggregation: each agent's FRT pool counts both their own
    # responses AND time the user spent waiting on their watch when the
    # ticket was eventually picked up by someone else.
    # Waive the miss for an on-duty agent who actually replied to the ticket
    # (worked it, just not first to touch) — same rule as the dashboard.
    tw_responders = metrics.responders_by_ticket(
        database.get_response_events_in_range(start_utc, end_utc))
    pw_responders = metrics.responders_by_ticket(
        database.get_response_events_in_range(prev_start, prev_end))
    tw_per_agent = metrics.aggregate_per_agent(
        this_week, responded_agents_by_ticket=tw_responders)
    pw_per_agent = metrics.aggregate_per_agent(
        prev_week, responded_agents_by_ticket=pw_responders)

    for agent in agents:
        tw_frts = tw_per_agent.get(agent, {}).get('frts', [])
        pw_frts = pw_per_agent.get(agent, {}).get('frts', [])

        tw_avg = sum(tw_frts) / len(tw_frts) if tw_frts else None
        pw_avg = sum(pw_frts) / len(pw_frts) if pw_frts else None
        tw_med = stat_median(tw_frts) if tw_frts else None

        if tw_avg is not None and pw_avg is not None:
            diff = tw_avg - pw_avg
            trend = "📉 improved" if diff < 0 else "📈 increased" if diff > 0 else "→ same"
            lines.append(
                f"  <b>{esc(agent)}</b>: {esc(config.fmt_mins(tw_avg))} avg "
                f"(was {esc(config.fmt_mins(pw_avg))}) — {trend}"
            )
            lines.append(f"    Median: {esc(config.fmt_mins(tw_med))} | "
                         f"Segments: {len(tw_frts)}")
        elif tw_avg is not None:
            lines.append(
                f"  <b>{esc(agent)}</b>: {esc(config.fmt_mins(tw_avg))} avg "
                f"(no data last week) | Segments: {len(tw_frts)}"
            )
        else:
            # An agent can have zero FRT minutes yet still have worked:
            # same-shift covers credit the wait to the on-duty agent and
            # leave the responder with a label-only 'covering' marker.
            covering_n = tw_per_agent.get(agent, {}).get('covering', 0)
            if covering_n:
                lines.append(
                    f"  <b>{esc(agent)}</b>: no FRT of their own — "
                    f"covered {covering_n} ticket(s) on someone else's shift"
                )
            else:
                lines.append(f"  <b>{esc(agent)}</b>: No responses this week")
    lines.append("")

    # =====================================================
    # SECTION 3 — Shift Load Balancing
    # =====================================================
    lines.append("<b>⚖️ Shift Load Balancing</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    for shift_info in config.SHIFTS:
        label = shift_info['label']
        agent = shift_info['agent']
        start_h = shift_info['start']
        end_h = shift_info['end']

        a = tw_per_agent.get(agent, {})
        on_shift = a.get('on_shift', 0)
        responded_n = a.get('responded', 0)
        # 'covered' = answered by someone else inside this shift (handled, but
        # the wait still belongs to the on-duty agent); 'missed' only remains
        # for waits that spilled past the shift boundary.
        covered_n = a.get('covered', 0) + a.get('missed', 0)
        cross_help_n = a.get('cross_help', 0) + a.get('covering', 0)
        lines.append(
            f"  Shift {esc(label)} ({esc(agent)}, "
            f"{start_h:02d}:00–{end_h:02d}:00 UTC): "
            f"{on_shift} on-shift | "
            f"{responded_n} handled | {covered_n} covered by others | "
            f"{cross_help_n} cross-help out"
        )
    lines.append("")

    # =====================================================
    # SECTION 4 — SLA Compliance Trend
    # =====================================================
    lines.append(f"<b>🚦 SLA Compliance</b> (target: ≤ {config.SLA_FRT_THRESHOLD_MINS} min)")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    tw_responded = [t for t in this_week if t['response_time_mins'] is not None]
    pw_responded = [t for t in prev_week if t['response_time_mins'] is not None]

    tw_ok = [t for t in tw_responded if not t['sla_breached']]
    pw_ok = [t for t in pw_responded if not t['sla_breached']]

    tw_pct = (len(tw_ok) / len(tw_responded) * 100) if tw_responded else 0
    pw_pct = (len(pw_ok) / len(pw_responded) * 100) if pw_responded else 0

    trend_arrow = ""
    if pw_responded:
        if tw_pct > pw_pct:
            trend_arrow = " 📈 improving"
        elif tw_pct < pw_pct:
            trend_arrow = " 📉 declining"
        else:
            trend_arrow = " → stable"

    lines.append(
        f"  This week: {len(tw_ok)}/{len(tw_responded)} "
        f"({tw_pct:.1f}%){trend_arrow}"
    )
    if pw_responded:
        lines.append(
            f"  Last week: {len(pw_ok)}/{len(pw_responded)} "
            f"({pw_pct:.1f}%)"
        )

    # Per-agent SLA this week — based on per-segment contributions vs
    # SLA_FRT_THRESHOLD. A breach = a single shift's contribution > threshold.
    for agent in sorted(tw_per_agent.keys()):
        s = metrics.summarize(tw_per_agent[agent])
        total = s['count_with_frt']
        if total == 0:
            continue
        ok = total - len(s['breaches'])
        pct = ok / total * 100
        star = " ⭐" if pct == 100 else ""
        lines.append(f"    {esc(agent)}: {ok}/{total} ({pct:.0f}%){star}")
    lines.append("")

    # =====================================================
    # SECTION 5 — Cross-Shift Help Summary
    # =====================================================
    cross_shifts = [t for t in this_week if t['cross_shift_help']]
    if cross_shifts:
        lines.append("<b>🔄 Cross-Shift Help</b>")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"  {len(cross_shifts)} ticket(s) handled by off-duty agents")

        # Group by helper
        helper_counts = Counter(t['agent_name'] for t in cross_shifts)
        covered_counts = Counter(t['on_duty_agent_name'] for t in cross_shifts)
        for helper, cnt in helper_counts.most_common():
            lines.append(f"    {esc(helper)} helped {cnt} time(s)")
        for covered, cnt in covered_counts.most_common():
            lines.append(f"    {esc(covered)} was covered {cnt} time(s)")
        lines.append("")

    # =====================================================
    # SECTION 6 — Notable Items
    # =====================================================
    # Repeat users this week
    owners = [t['ticket_owner'] for t in this_week if t['ticket_owner']]
    owner_counts = Counter(owners)
    repeat = {uid: cnt for uid, cnt in owner_counts.items() if cnt >= 3}
    if repeat:
        lines.append("<b>🔁 Frequent Users (3+ tickets this week)</b>")
        for uid, cnt in sorted(repeat.items(), key=lambda x: -x[1]):
            lines.append(f"  • User {esc(uid)}: {cnt} tickets")
        lines.append("")

    lines.append("<i>KyberSwap Support Analytics — Weekly Summary</i>")
    return "\n".join(lines)


# =====================================================
# TELEGRAM DELIVERY
# =====================================================

def send_telegram_message(text):
    """Send message to every configured Telegram chat."""
    token = config.TELEGRAM_TOKEN
    chat_ids = config.TELEGRAM_CHAT_IDS

    if not token or not chat_ids:
        print("⚠️  TELEGRAM_TOKEN or TELEGRAM_CHAT_ID(s) not set.")
        return

    # Split long messages once
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
                r = requests.post(url, json=payload)
                r.raise_for_status()
                if i == len(chunks) - 1:
                    print(f"✅ Telegram weekly report sent to {chat_id}")
            except Exception as e:
                print(f"❌ Failed to send Telegram report to {chat_id}: {e}")


def send_report():
    """Generate and send the weekly report."""
    report_text = generate_weekly_report()
    send_telegram_message(report_text)


if __name__ == '__main__':
    send_report()
