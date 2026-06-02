"""
dashboard/api.py

FastAPI server for the KyberSwap Community Dashboard. Exposes:
  GET /api/community  — aggregated community digest data for the latest day
  GET /api/support    — live support team metrics for the last 24h
  GET /api/health     — liveness probe
  GET /               — single-page React app (production bundle from web/dist)

Run:
  cd "/Volumes/Macintosh HD - Data/Project"
  venv/bin/python -m uvicorn dashboard.api:app --host 0.0.0.0 --port 8000

Imports `config` and `database` from project root, so always launch from
the project working directory.
"""

import json
import os
import statistics
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# Allow `import config, database` when launched from project root.
PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import config  # noqa: E402
import database  # noqa: E402

WEB_DIST = Path(__file__).resolve().parent / 'web' / 'dist'

app = FastAPI(title='KyberSwap Community Dashboard')


# =====================================================
# COMMUNITY (Tab 1)
# =====================================================

_PRIORITY_RANK = {'high': 0, 'medium': 1, 'low': 2}


def _infer_action_priority(text: str) -> str:
    t = (text or '').lower()
    high_kw = ('urgent', 'critical', 'broken', 'fail', 'crash', 'security', 'stuck')
    med_kw = ('investigate', 'review', 'fix', 'address', 'audit', 'reduce', 'improve')
    if any(k in t for k in high_kw):
        return 'high'
    if any(k in t for k in med_kw):
        return 'medium'
    return 'low'


def _strip_emoji_channel(name: str) -> str:
    """Channel names in DB are like '🌎general-english' or '#general'. Return
    plain form with '#' prefix and no leading emoji prefix used in Discord."""
    name = (name or '').strip()
    if name.startswith('#'):
        name = name[1:]
    # Drop a single leading emoji-ish char if it doesn't look ASCII-alpha
    while name and not name[0].isalnum() and not name[0] in '_-':
        name = name[1:]
    return '#' + name


def _resolve_window(start: Optional[str], end: Optional[str]) -> tuple[datetime, datetime, str]:
    """Parse ?start=YYYY-MM-DD&end=YYYY-MM-DD into a UTC datetime window.

    Both are inclusive UTC dates. If both omitted, default to the trailing
    24h ending now. If only one is provided, fall back to the default.
    Returns (start_dt, end_dt, label) where label is human-friendly
    ("Last 24 hours", "May 10 → May 17", etc.).
    """
    now = datetime.now(tz=timezone.utc)
    if not start or not end:
        return (now - timedelta(hours=24), now, 'Last 24 hours')
    try:
        start_d = date.fromisoformat(start)
        end_d = date.fromisoformat(end)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f'Bad date: {e}')
    if end_d < start_d:
        raise HTTPException(status_code=400, detail='end must be >= start')
    span_days = (end_d - start_d).days + 1
    if span_days > 365:
        raise HTTPException(status_code=400, detail='range too wide (max 365 days)')
    start_dt = datetime(start_d.year, start_d.month, start_d.day, tzinfo=timezone.utc)
    # end is INCLUSIVE: include the entire end day → add one full day
    end_dt = datetime(end_d.year, end_d.month, end_d.day, tzinfo=timezone.utc) + timedelta(days=1)
    if span_days == 1:
        label = start_d.strftime('%b %d, %Y')
    else:
        label = f"{start_d.strftime('%b %d')} → {end_d.strftime('%b %d, %Y')}"
    return (start_dt, end_dt, label)


def build_community_payload(start_dt: datetime, end_dt: datetime,
                            window_label: str) -> dict[str, Any]:
    # Resolve which digest dates fall in this window. We treat the window
    # END as exclusive (matches _resolve_window), so the last digest_date we
    # accept is (end_dt - 1 day).
    span_days = (end_dt - start_dt).days
    start_date_str = start_dt.date().isoformat()
    end_date_str = (end_dt - timedelta(days=1)).date().isoformat()

    rows = database.get_community_digests_in_range(start_date_str, end_date_str)
    if not rows:
        return {
            'lastUpdated': 'No digest in window',
            'period': window_label,
            'totalMessages': 0,
            'activeUsers': 0,
            'channels': 0,
            'topics': [],
            'actionItems': [],
            'sentimentTimeline': [],
            'channelBreakdown': [],
            'marketNews': [],
        }

    digests = [r['digest'] for r in rows]
    total_messages = sum(int(d.get('message_count', 0) or 0) for d in digests)

    # Topics — flatten themes across all digests in the window. For multi-day
    # windows the SAME theme title can recur (e.g. "Market Downturn" each
    # day); merge them by case-insensitive title to avoid duplicate bubbles,
    # summing counts and unioning channel labels.
    def _bubble_label(title: str) -> str:
        if ' ' in title and len(title) > 12:
            parts = title.split(' ')
            mid = len(parts) // 2
            return ' '.join(parts[:mid]) + '\n' + ' '.join(parts[mid:])
        return title

    topic_index: dict[str, dict[str, Any]] = {}
    for d in digests:
        ch_label = _strip_emoji_channel(d.get('channel', ''))
        for theme in d.get('themes') or []:
            title = (theme.get('title') or '').strip()
            if not title:
                continue
            key = title.lower()
            count = int(theme.get('message_count', 0) or 0)
            if key in topic_index:
                topic_index[key]['count'] += count
                if ch_label not in topic_index[key]['channels']:
                    topic_index[key]['channels'].append(ch_label)
            else:
                topic_index[key] = {
                    'id': 0,  # assigned later
                    'label': _bubble_label(title),
                    'count': count,
                    'sentiment': theme.get('sentiment', 'neutral'),
                    'category': 'product',
                    'channels': [ch_label],
                }
    topics = sorted(topic_index.values(), key=lambda t: -t['count'])[:15]
    for i, t in enumerate(topics, start=1):
        t['id'] = i

    # Action items — dedupe by exact text across the window.
    seen_items: set[str] = set()
    action_items: list[dict[str, Any]] = []
    for d in digests:
        ch_label = _strip_emoji_channel(d.get('channel', ''))
        for item in d.get('action_items') or []:
            text = str(item).strip()
            if not text or text.lower() in seen_items:
                continue
            seen_items.add(text.lower())
            action_items.append({
                'priority': _infer_action_priority(text),
                'text': text,
                'topic': ch_label,
            })
    action_items.sort(key=lambda a: _PRIORITY_RANK[a['priority']])
    action_items = action_items[:10]

    # Channel breakdown — sum message_count per channel across days. For
    # `sentiment`, pick the most common overall_sentiment seen across days.
    channel_index: dict[str, dict[str, Any]] = {}
    channel_sentiment_votes: dict[str, dict[str, int]] = {}
    for d in digests:
        name = _strip_emoji_channel(d.get('channel', ''))
        msgs = int(d.get('message_count', 0) or 0)
        sentiment = d.get('overall_sentiment', 'neutral')
        if name not in channel_index:
            channel_index[name] = {'name': name, 'messages': 0, 'sentiment': sentiment}
            channel_sentiment_votes[name] = {}
        channel_index[name]['messages'] += msgs
        channel_sentiment_votes[name][sentiment] = (
            channel_sentiment_votes[name].get(sentiment, 0) + 1
        )
    for name, votes in channel_sentiment_votes.items():
        channel_index[name]['sentiment'] = max(votes, key=votes.get)
    channel_breakdown = sorted(channel_index.values(), key=lambda c: -c['messages'])

    # Sentiment timeline. We do NOT have per-message timestamps in the digest
    # pipeline, so we generate a synthetic curve. For a single-day window we
    # use 12 two-hour buckets across the day (peak ~14:00 UTC). For multi-day
    # windows we switch to one bucket per calendar day so the time axis
    # actually means something.
    pos_total = sum(t['count'] for t in topics if t['sentiment'] == 'positive')
    neu_total = sum(t['count'] for t in topics if t['sentiment'] in ('neutral', 'mixed'))
    neg_total = sum(t['count'] for t in topics if t['sentiment'] == 'negative')
    sentiment_timeline = []
    if span_days <= 1:
        activity_curve = [0.3, 0.2, 0.2, 0.4, 0.8, 1.2, 1.5, 1.6, 1.4, 1.3, 1.0, 0.6]
        weight_sum = sum(activity_curve)
        hours = ['00:00', '02:00', '04:00', '06:00', '08:00', '10:00',
                 '12:00', '14:00', '16:00', '18:00', '20:00', '22:00']
        for i, hour in enumerate(hours):
            w = activity_curve[i] / weight_sum
            sentiment_timeline.append({
                'hour': hour,
                'positive': round(pos_total * w),
                'neutral': round(neu_total * w),
                'negative': round(neg_total * w),
            })
    else:
        # Per-day buckets — use ACTUAL per-day message totals from digests so
        # the line follows real volume, splitting by sentiment proportions.
        by_day: dict[str, dict[str, int]] = {}
        for r in rows:
            day = r['digest_date']
            d = r['digest']
            sentiment = d.get('overall_sentiment', 'neutral')
            msgs = int(d.get('message_count', 0) or 0)
            by_day.setdefault(day, {'positive': 0, 'neutral': 0, 'negative': 0})
            if sentiment == 'positive':
                by_day[day]['positive'] += msgs
            elif sentiment == 'negative':
                by_day[day]['negative'] += msgs
            else:
                by_day[day]['neutral'] += msgs
        for day in sorted(by_day):
            label_dt = datetime.fromisoformat(day)
            sentiment_timeline.append({
                'hour': label_dt.strftime('%b %d'),
                **by_day[day],
            })

    # Market news — themes from speculation/news channel across the window.
    # Dedupe by title, sum counts.
    market_index: dict[str, dict[str, Any]] = {}
    sentiment_emojis = {
        'positive': '🚀', 'negative': '📉',
        'mixed': '🌀', 'neutral': '📊',
    }
    for d in digests:
        ch_raw = (d.get('channel') or '').lower()
        if not ('speculation' in ch_raw or 'degen' in ch_raw or 'news' in ch_raw):
            continue
        for theme in (d.get('themes') or []):
            title = (theme.get('title') or 'Untitled').strip()
            key = title.lower()
            sentiment = theme.get('sentiment', 'neutral')
            count = int(theme.get('message_count', 0) or 0)
            if key in market_index:
                market_index[key]['mentions'] += count
                market_index[key]['reactions'] += count * 3
            else:
                market_index[key] = {
                    'headline': title,
                    'reactions': count * 3,
                    'sentiment': sentiment,
                    'emoji': sentiment_emojis.get(sentiment, '📊'),
                    'timeAgo': 'in window',
                    'mentions': count,
                }
    market_news = sorted(market_index.values(), key=lambda m: -m['mentions'])[:5]

    # Active users — true count not stored; estimate from total messages
    active_users = max(int(total_messages * 0.4), 1) if total_messages else 0

    return {
        'lastUpdated': end_date_str if span_days > 1 else start_date_str,
        'period': window_label,
        'totalMessages': total_messages,
        'activeUsers': active_users,
        'channels': len(channel_breakdown),
        'topics': topics,
        'actionItems': action_items,
        'sentimentTimeline': sentiment_timeline,
        'channelBreakdown': channel_breakdown,
        'marketNews': market_news,
    }


# =====================================================
# SUPPORT (Tab 2)
# =====================================================

# Product categories — mirror classify_tickets.CATEGORY_MAP. Source of truth
# is the LLM classifier (cached in tickets.product_group + product_subcategory).
# Tickets without LLM result fall to "Other / Uncategorized" — we never guess.
PRODUCT_COLORS = {
    # Trade
    'Aggregator Swap':     '#6366f1',
    'Cross-chain Swap':    '#8b5cf6',
    'Limit Order':         '#06b6d4',
    # Earn
    'Kyber Earn / LP':     '#22c55e',
    'ZAP':                 '#10b981',
    'Smart Exit':          '#14b8a6',
    # Infrastructure
    'Wallet / Connect':    '#f59e0b',
    'Transaction':         '#ef4444',
    'Token Approval':      '#f97316',
    'Gas & MEV':           '#eab308',
    'Bridge':              '#a855f7',
    # Business
    'Integration (B2B)':   '#0ea5e9',
    'Community & Rewards': '#ec4899',
    # Other
    'Uncategorized':       '#64748b',
}

# Subcategory → main group (for the donut chart's top-level rollup).
SUBCATEGORY_TO_GROUP = {
    'Aggregator Swap':     'Trade',
    'Cross-chain Swap':    'Trade',
    'Limit Order':         'Trade',
    'Kyber Earn / LP':     'Earn',
    'ZAP':                 'Earn',
    'Smart Exit':          'Earn',
    'Wallet / Connect':    'Infrastructure',
    'Transaction':         'Infrastructure',
    'Token Approval':      'Infrastructure',
    'Gas & MEV':           'Infrastructure',
    'Bridge':              'Infrastructure',
    'Integration (B2B)':   'Business',
    'Community & Rewards': 'Business',
    'Uncategorized':       'Other',
}


def get_ticket_category(ticket: dict) -> str:
    """Return the cached LLM subcategory, or 'Uncategorized' if not classified
    yet (so the chart shows pending work, not a fake bucket)."""
    return ticket.get('product_subcategory') or 'Uncategorized'


def _median(xs):
    if not xs:
        return None
    return round(statistics.median(xs), 2)


def _safe_avg(xs):
    if not xs:
        return None
    return round(sum(xs) / len(xs), 2)


def _format_age(delta: timedelta) -> str:
    secs = int(delta.total_seconds())
    if secs < 60:
        return f'{secs}s'
    mins = secs // 60
    if mins < 60:
        return f'{mins}m'
    h, m = divmod(mins, 60)
    if h < 24:
        return f'{h}h {m}m' if m else f'{h}h'
    d, h = divmod(h, 24)
    return f'{d}d {h}h' if h else f'{d}d'


def _open_ticket_severity(age: timedelta, status: str) -> str:
    no_response = status == 'No response'
    if no_response and age >= timedelta(hours=2):
        return 'high'
    if no_response and age >= timedelta(minutes=30):
        return 'medium'
    return 'low'


def _agent_action_items(agent: dict, product_breakdown: list[dict]) -> list[dict]:
    items: list[dict] = []
    if agent['slaCompliance'] is not None and agent['slaCompliance'] < 80:
        items.append({
            'priority': 'high',
            'text': f"{len(agent['breaches'])} SLA breaches "
                    f"({', '.join(agent['breaches'][:3])}). "
                    f"Investigate availability during {agent['shift']}.",
        })
    if agent['missed'] > 0:
        items.append({
            'priority': 'high',
            'text': f"{agent['missed']} tickets missed during own shift. "
                    f"Review handoff process.",
        })
    high_frt_prod = next(
        (p for p in product_breakdown if p['avgFRT'] and p['avgFRT'] > 20),
        None,
    )
    if high_frt_prod and agent['responded'] > 0:
        items.append({
            'priority': 'medium',
            'text': f"{high_frt_prod['name']} tickets took longer "
                    f"(avg {high_frt_prod['avgFRT']}m). Consider quick replies.",
        })
    if agent['slaCompliance'] == 100 and agent['responded'] > 0:
        items.append({
            'priority': 'low',
            'text': 'Perfect SLA compliance — recognize performance in standup.',
        })
    if agent['crossHelp'] > 0:
        items.append({
            'priority': 'low',
            'text': f"Helped peers {agent['crossHelp']} times outside own shift.",
        })
    return items[:4]


def build_support_payload(start_utc: datetime, end_utc: datetime,
                          window_label: str) -> dict[str, Any]:
    now_utc = datetime.now(tz=timezone.utc)

    created = database.get_tickets_in_range(start_utc, end_utc)
    open_all = database.get_open_tickets()

    # Headline metrics
    total = len(created)
    resolved = sum(1 for t in created if t.get('closed_at'))
    open_count = sum(
        1 for t in open_all
        if t.get('created_at')
        and start_utc <= datetime.fromisoformat(t['created_at']) < end_utc
    )
    frts = [t['response_time_mins'] for t in created
            if t.get('response_time_mins') is not None]
    avg_frt = _safe_avg(frts)
    median_frt = _median(frts)
    responded = [t for t in created if t.get('response_time_mins') is not None]
    breaches = [t for t in created if t.get('sla_breached')]
    sla_compliance = (
        round((1 - len(breaches) / len(responded)) * 100, 1)
        if responded else 100.0
    )

    # Pre-classify each created ticket using the LLM-cached column
    for t in created:
        t['_product'] = get_ticket_category(t)

    # Product breakdown — one row per subcategory that actually appears,
    # ordered by ticket count desc. Always include any subcategory the LLM
    # used so we never silently drop a real bucket.
    present_subs = []
    seen_subs = set()
    for t in created:
        sub = t['_product']
        if sub not in seen_subs:
            seen_subs.add(sub)
            present_subs.append(sub)

    product_breakdown = []
    for name in present_subs:
        bucket = [t for t in created if t['_product'] == name]
        if not bucket:
            continue
        b_resolved = [t for t in bucket if t.get('closed_at')]
        b_frts = [t['response_time_mins'] for t in bucket
                  if t.get('response_time_mins') is not None]
        b_breaches = sum(1 for t in bucket if t.get('sla_breached'))
        b_responded = len(b_frts)
        if b_responded == 0:
            sentiment = 'neutral'
        else:
            breach_rate = b_breaches / b_responded
            sentiment = ('negative' if breach_rate >= 0.3
                         else 'mixed' if breach_rate >= 0.1
                         else 'positive')
        # Top issue snippets — first 80 chars of first_user_message
        issues = []
        seen = set()
        for t in bucket:
            msg = (t.get('first_user_message') or t.get('conversation_excerpt') or '').strip()
            if not msg:
                continue
            head = msg[:80].replace('\n', ' ')
            key = head.lower()
            if key in seen:
                continue
            seen.add(key)
            issues.append(head)
            if len(issues) >= 3:
                break

        product_breakdown.append({
            'name': name,
            'group': SUBCATEGORY_TO_GROUP.get(name, 'Other'),
            'tickets': len(bucket),
            'pct': round(len(bucket) / total * 100, 1) if total else 0,
            'resolved': len(b_resolved),
            'avgFRT': _safe_avg(b_frts) or 0,
            'sentiment': sentiment,
            'color': PRODUCT_COLORS.get(name, '#64748b'),
            'issues': issues,
        })
    product_breakdown.sort(key=lambda p: -p['tickets'])

    # Agents — iterate config.SHIFTS. FRT split rule (2026-05-18): when a
    # ticket waits across a shift boundary, EACH on-duty agent during the
    # wait is credited a contribution. metrics.aggregate_per_agent walks
    # every responded ticket and returns those contributions per agent.
    import metrics
    per_agent = metrics.aggregate_per_agent(created)
    # Per-response-event rollup: every (user_msg → agent_reply) gap in the
    # window, including follow-ups. Used to expose avg_response_all alongside
    # the FRT-split avgFRT (first response only).
    response_events = database.get_response_events_in_range(start_utc, end_utc)
    per_agent_resp = metrics.aggregate_response_events(response_events)
    agents = []
    for shift in config.SHIFTS:
        agent_name = shift['agent']
        shift_label = shift['label']
        s_start, s_end = shift['start'], shift['end']
        a = per_agent.get(agent_name, {
            'frts': [], 'missed_mins': [], 'breaches': [],
            'fastest': None, 'slowest': None,
            'on_shift': 0, 'responded': 0, 'missed': 0, 'cross_help': 0,
            'buddy_covered': 0, 'buddy_covered_by': {}, 'buddy_covered_tickets': [],
        })
        summary = metrics.summarize(a)
        resp_summary = metrics.summarize_responses(per_agent_resp.get(agent_name, {
            'all_mins': [], 'first_mins': [], 'followup_mins': [],
            'missed_mins': [], 'cross_help_mins': [], 'responded_mins': [],
        }))
        fastest_pair = summary['fastest']
        slowest_pair = summary['slowest']
        fastest = (
            {'ticket': fastest_pair[0], 'time': fastest_pair[1]}
            if fastest_pair else {'ticket': '-', 'time': 0}
        )
        slowest = (
            {'ticket': slowest_pair[0], 'time': slowest_pair[1]}
            if slowest_pair else {'ticket': '-', 'time': 0}
        )

        # Top product mix for this agent's responses
        top_products: dict[str, int] = {}
        for t in created:
            if t.get('agent_name') == agent_name:
                top_products[t['_product']] = top_products.get(t['_product'], 0) + 1
        top_products_list = [
            {'name': k, 'count': v}
            for k, v in sorted(top_products.items(), key=lambda kv: -kv[1])
        ][:3]

        shift_range = (
            f"{s_start:02d}:00–{s_end:02d}:00 UTC"
        )
        agents.append({
            'name': agent_name,
            'shift': shift_range,
            'shiftLabel': shift_label,
            'onShift': summary['on_shift'],
            'responded': summary['responded'],
            'missed': summary['missed'],
            'crossHelp': summary['cross_help'],
            'avgFRT': summary['avg_frt'] if summary['avg_frt'] is not None else 0,
            'medianFRT': summary['median_frt'] if summary['median_frt'] is not None else 0,
            'avgResponseAll': resp_summary['avg_response_all'] if resp_summary['avg_response_all'] is not None else 0,
            'avgResponseFirst': resp_summary['avg_response_first'] if resp_summary['avg_response_first'] is not None else 0,
            'avgResponseFollowup': resp_summary['avg_response_followup'] if resp_summary['avg_response_followup'] is not None else 0,
            'responseCount': resp_summary['count_all'],
            'followupCount': resp_summary['count_followup'],
            'missedCount': resp_summary['count_missed'],
            'totalMissedMins': resp_summary['total_missed_mins'],
            'totalCrossHelpMins': resp_summary['total_cross_help_mins'],
            'fastest': fastest,
            'slowest': slowest,
            'slaCompliance': summary['sla_compliance'],  # null when no events in window
            'breaches': [tid for (tid, _mins) in summary['breaches']],
            'topProducts': top_products_list,
            'buddyCovered': summary['buddy_covered'],
            'buddyCoveredBy': [
                {'buddy': b, 'count': n}
                for b, n in sorted(
                    summary['buddy_covered_by'].items(),
                    key=lambda kv: -kv[1],
                )
            ],
            'buddyCoveredTickets': summary['buddy_covered_tickets'],
        })

    # Agent action items (heuristic rules)
    agent_actions = []
    for a in agents:
        agent_actions.append({
            'agent': a['name'],
            'items': _agent_action_items(a, product_breakdown),
        })

    # Open tickets needing attention — show ALL currently open, not just
    # last-24h, because old open tickets are exactly the ones likely to be
    # forgotten. Sorted by severity below.
    open_table = []
    for t in open_all:
        if not t.get('created_at'):
            continue
        created_at = datetime.fromisoformat(t['created_at'])
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        age = now_utc - created_at
        if t.get('first_responded_at') is None:
            status = 'No response'
        else:
            last_user = t.get('last_user_msg_at')
            last_agent = t.get('last_agent_msg_at')
            if last_user and (not last_agent or last_user > last_agent):
                status = 'Awaiting agent reply'
            elif last_agent and (not last_user or last_agent > last_user):
                status = 'Awaiting user reply'
            else:
                status = 'In progress'
        open_table.append({
            'id': t['ticket_id'],
            'age': _format_age(age),
            '_age_seconds': int(age.total_seconds()),
            'product': get_ticket_category(t),
            'status': status,
            'onDuty': t.get('on_duty_agent_name') or 'Unknown',
            'severity': _open_ticket_severity(age, status),
        })
    sev_rank = {'high': 0, 'medium': 1, 'low': 2}
    open_table.sort(key=lambda r: (sev_rank[r['severity']], -r['_age_seconds']))
    for r in open_table:
        r.pop('_age_seconds', None)

    # Satisfaction summary across created tickets in the window. Tickets
    # without a classification yet are not counted (the classifier loop will
    # fill them in incrementally).
    sat_counts = Counter(
        t.get('satisfaction_label') for t in created
        if t.get('satisfaction_label') in ('positive', 'neutral',
                                            'negative', 'no_signal')
    )
    sat_classified = sum(sat_counts.values())
    sat_with_signal = sat_classified - sat_counts.get('no_signal', 0)
    satisfaction = {
        'classified': sat_classified,
        'unclassified': total - sat_classified,
        'positive': sat_counts.get('positive', 0),
        'neutral': sat_counts.get('neutral', 0),
        'negative': sat_counts.get('negative', 0),
        'noSignal': sat_counts.get('no_signal', 0),
        # % positive among tickets with a real signal (excludes silent closures).
        'positivePct': (
            round(sat_counts.get('positive', 0) / sat_with_signal * 100, 1)
            if sat_with_signal else None
        ),
    }

    # Review queue — every negative ticket surfaces for the team to look at.
    review_queue = []
    for t in created:
        if t.get('satisfaction_label') != 'negative':
            continue
        try:
            signals = json.loads(t.get('satisfaction_signals') or '[]')
        except (json.JSONDecodeError, TypeError):
            signals = []
        review_queue.append({
            'id': t['ticket_id'],
            'agent': t.get('agent_name') or 'Unknown',
            'product': get_ticket_category(t),
            'score': t.get('satisfaction_score'),
            'signals': signals[:3],
        })
    review_queue.sort(key=lambda r: r['score'] if r['score'] is not None else 0)

    return {
        'lastUpdated': now_utc.strftime('%Y-%m-%d %H:%M UTC'),
        'period': window_label,
        'totalTickets': total,
        'resolved': resolved,
        'open': open_count,
        'avgFRT': avg_frt or 0,
        'medianFRT': median_frt or 0,
        'slaCompliance': sla_compliance,
        'productBreakdown': product_breakdown,
        'agents': agents,
        'agentActions': agent_actions,
        'openTickets': open_table,
        'satisfaction': satisfaction,
        'reviewQueue': review_queue,
    }


# =====================================================
# ROUTES
# =====================================================

@app.get('/api/health')
def health():
    return {'ok': True, 'time': datetime.now(timezone.utc).isoformat()}


@app.get('/api/community')
def api_community(
    start: Optional[str] = Query(None, description="UTC date YYYY-MM-DD inclusive"),
    end: Optional[str] = Query(None, description="UTC date YYYY-MM-DD inclusive"),
):
    start_dt, end_dt, label = _resolve_window(start, end)
    return JSONResponse(build_community_payload(start_dt, end_dt, label))


@app.get('/api/support')
def api_support(
    start: Optional[str] = Query(None, description="UTC date YYYY-MM-DD inclusive"),
    end: Optional[str] = Query(None, description="UTC date YYYY-MM-DD inclusive"),
):
    start_dt, end_dt, label = _resolve_window(start, end)
    return JSONResponse(build_support_payload(start_dt, end_dt, label))


# Mount the built React app last so /api/* takes precedence
if WEB_DIST.exists() and (WEB_DIST / 'index.html').exists():
    app.mount(
        '/',
        StaticFiles(directory=str(WEB_DIST), html=True),
        name='web',
    )
else:
    @app.get('/')
    def index_placeholder():
        return FileResponse(
            str(Path(__file__).parent / 'placeholder.html')
            if (Path(__file__).parent / 'placeholder.html').exists()
            else __file__,
            media_type='text/html'
            if (Path(__file__).parent / 'placeholder.html').exists()
            else 'text/plain',
        )
