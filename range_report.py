import os
import pandas as pd
from datetime import datetime, timedelta, timezone

import config

CSV_FILE = config.CSV_FILE
TZ_OFFSET = config.TZ_OFFSET
LOCAL_TZ  = config.LOCAL_TZ

normalize_agent = config.normalize_agent
parse_dt = config.parse_dt
fmt_mins = config.fmt_mins

def run_report(start_dt, end_dt):
    if not os.path.exists(CSV_FILE):
        print("❌ ticket_analytics.csv not found.")
        return

    df = pd.read_csv(CSV_FILE)
    df['Agent Name'] = df['Agent Name'].apply(normalize_agent)  # Normalize
    df['_created'] = df['Created At'].apply(parse_dt)
    df['_closed'] = df['Closed At'].apply(parse_dt)

    # Filter by creation time
    mask = (df['_created'] >= start_dt) & (df['_created'] <= end_dt)
    recent = df[mask].copy()

    total_tickets = len(recent)

    # Response metrics
    with_response = recent[recent['Response Time (Mins)'].notna()].copy()
    with_response['_rt'] = pd.to_numeric(with_response['Response Time (Mins)'], errors='coerce')
    with_response = with_response[with_response['_rt'].notna()]

    avg_response = with_response['_rt'].mean() if not with_response.empty else None
    
    slowest_row = None
    fastest_row = None
    if not with_response.empty:
        slowest_row = with_response.loc[with_response['_rt'].idxmax()]
        fastest_row = with_response.loc[with_response['_rt'].idxmin()]

    # Agent stats
    agent_stats = []
    if not with_response.empty:
        grouped = with_response.groupby('Agent Name')['_rt'].mean().reset_index()
        grouped.columns = ['Agent', 'Avg Response (Mins)']
        agent_stats = grouped.values.tolist()

    # Handling time for tickets CLOSED in this window
    closed_in_range = df[df['_closed'].notna() & (df['_closed'] >= start_dt) & (df['_closed'] <= end_dt)].copy()
    closed_in_range['_created_at_val'] = closed_in_range['Created At'].apply(parse_dt)
    closed_in_range = closed_in_range[closed_in_range['_created_at_val'].notna()]
    closed_in_range['_handling'] = [
        (r['_closed'] - r['_created_at_val']).total_seconds() / 60
        for _, r in closed_in_range.iterrows()
    ]
    avg_handling = closed_in_range['_handling'].mean() if not closed_in_range.empty else None
    med_handling = closed_in_range['_handling'].median() if not closed_in_range.empty else None

    print(f"\n📊 PERFORMANCE REPORT")
    print(f"Period: {start_dt.strftime('%Y-%m-%d %H:%M')} to {end_dt.strftime('%Y-%m-%d %H:%M')} (UTC+7)")
    print("-" * 50)
    print(f"🎫 Total Tickets Created: {total_tickets}")
    print(f"⏱️ Avg First Response Time: {fmt_mins(avg_response)}")
    print(f"🔒 Avg Handling Time (Closed in window): {fmt_mins(avg_handling)}")
    print(f"⚖️ Median Handling Time (Closed in window): {fmt_mins(med_handling)}")

    if slowest_row is not None:
        print(f"🐢 Slowest Response: {slowest_row['Ticket ID']} ({fmt_mins(slowest_row['_rt'])}) - Agent: {slowest_row['Agent Name']}")
    if fastest_row is not None:
        print(f"⚡ Fastest Response: {fastest_row['Ticket ID']} ({fmt_mins(fastest_row['_rt'])}) - Agent: {fastest_row['Agent Name']}")

    if agent_stats:
        print("\n👥 Per-Agent Avg Response Time:")
        for agent, rt in sorted(agent_stats, key=lambda x: x[1]):
            print(f" • {agent}: {fmt_mins(rt)}")
    print("-" * 50)

if __name__ == "__main__":
    start = datetime(2026, 3, 18, 9, 0, tzinfo=LOCAL_TZ)
    end = datetime(2026, 4, 1, 9, 0, tzinfo=LOCAL_TZ)
    run_report(start, end)
