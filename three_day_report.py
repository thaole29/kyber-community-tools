"""
three_day_report.py

Generates a support team performance report for the last 3 days (72 hours)
and sends it to the Telegram bot/group.
"""

import os
import requests
import pandas as pd
from datetime import datetime, timedelta

import config

TELEGRAM_TOKEN = config.TELEGRAM_TOKEN
TELEGRAM_CHAT  = config.TELEGRAM_CHAT_ID
CSV_FILE = config.CSV_FILE
TZ_OFFSET = config.TZ_OFFSET
LOCAL_TZ  = config.LOCAL_TZ

normalize_agent = config.normalize_agent
parse_dt = config.parse_dt

def generate_report():
    """Build and return the report data."""
    if not os.path.exists(CSV_FILE):
        return None, "❌ ticket_analytics.csv not found."

    df = pd.read_csv(CSV_FILE)
    now = datetime.now(tz=LOCAL_TZ)
    cutoff = now - timedelta(hours=72)

    # --- Filter tickets created in the last 72 hours ---
    df['_created'] = df['Created At'].apply(parse_dt)
    df['Agent Name'] = df['Agent Name'].apply(normalize_agent)
    recent = df[df['_created'].notna() & (df['_created'] >= cutoff)].copy()

    total_tickets = len(recent)

    # --- Response time metrics ---
    with_response = recent[recent['Response Time (Mins)'].notna()].copy()
    with_response['_rt'] = pd.to_numeric(with_response['Response Time (Mins)'], errors='coerce')
    with_response = with_response[with_response['_rt'].notna()]

    avg_response = with_response['_rt'].mean() if not with_response.empty else None

    if not with_response.empty:
        slowest_row = with_response.loc[with_response['_rt'].idxmax()]
        fastest_row = with_response.loc[with_response['_rt'].idxmin()]
    else:
        slowest_row = fastest_row = None

    # --- Per-agent average response time ---
    agent_stats = []
    if not with_response.empty:
        grouped = with_response.groupby('Agent Name')['_rt'].mean().reset_index()
        grouped.columns = ['Agent', 'Avg Response (Mins)']
        agent_stats = grouped.values.tolist()

    # --- Average handling time for CLOSED tickets in last 72h ---
    df['_closed'] = df['Closed At'].apply(parse_dt)
    closed_recent = df[df['_closed'].notna() & (df['_closed'] >= cutoff)].copy()
    closed_recent['_created2'] = closed_recent['Created At'].apply(parse_dt)
    closed_recent = closed_recent[closed_recent['_created2'].notna()]
    closed_recent['_handling'] = [
        (r['_closed'] - r['_created2']).total_seconds() / 60
        for _, r in closed_recent.iterrows()
    ]
    median_handling = closed_recent['_handling'].median() if not closed_recent.empty else None
    avg_handling = closed_recent['_handling'].mean() if not closed_recent.empty else None

    # ---- Build fields ----
    fmt_mins = config.fmt_mins

    report = {
        "title": "📊 3-Day Support Team Report",
        "description": f"Performance summary for the last 72 hours\n`{cutoff.strftime('%Y-%m-%d %H:%M')} → {now.strftime('%Y-%m-%d %H:%M')} (UTC+7)`",
        "metrics": {
            "🎫 Tickets Created": str(total_tickets),
            "⏱️ Avg First Response Time": fmt_mins(avg_response),
            "🔒 Avg Handling Time": fmt_mins(avg_handling),
            "⚖️ Median Handling Time": fmt_mins(median_handling)
        },
        "slowest": slowest_row,
        "fastest": fastest_row,
        "agent_stats": agent_stats
    }

    return report, None

def format_telegram_message(report) -> str:
    """Format into a clean Telegram Markdown message."""
    fmt_mins = config.fmt_mins

    lines = [f"*{report['title']}*", report['description'], ""]
    
    for name, value in report['metrics'].items():
        lines.append(f"*{name}*")
        lines.append(value)
        lines.append("")

    if report['slowest'] is not None:
        row = report['slowest']
        lines.append(f"*🐢 Slowest Response*")
        lines.append(f"`{row['Ticket ID']}` — {fmt_mins(row['Response Time (Mins)'])}")
        lines.append(f"Agent: *{row['Agent Name']}*")
        lines.append("")

    if report['fastest'] is not None:
        row = report['fastest']
        lines.append(f"*⚡ Fastest Response*")
        lines.append(f"`{row['Ticket ID']}` — {fmt_mins(row['Response Time (Mins)'])}")
        lines.append(f"Agent: *{row['Agent Name']}*")
        lines.append("")

    if report['agent_stats']:
        lines.append("*👥 Per-Agent Avg Response Time*")
        for a, r in sorted(report['agent_stats'], key=lambda x: x[1]):
            lines.append(f"• *{a}*: {fmt_mins(r)}")
        lines.append("")

    lines.append("_KyberSwap Support Analytics_")
    return "\n".join(lines)

def send_telegram_report(report):
    """Send the report to Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print("⚠️  TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set.")
        return

    chat_id = TELEGRAM_CHAT
    thread_id = None
    if '/' in chat_id:
        parts = chat_id.split('/')
        chat_id, thread_id = parts[0], parts[1]
    if not chat_id.startswith('-'):
        chat_id = f"-100{chat_id}"

    text = format_telegram_message(report)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    if thread_id:
        payload["message_thread_id"] = thread_id

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json=payload)
        r.raise_for_status()
        print("✅ Telegram report sent successfully!")
    except Exception as e:
        print(f"❌ Failed to send Telegram report: {e}")

if __name__ == '__main__':
    report, error = generate_report()
    if error:
        print(error)
    else:
        send_telegram_report(report)
