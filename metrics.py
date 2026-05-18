"""
metrics.py

Shared FRT-split aggregator used by `dashboard/api.py` and the daily/weekly
report scripts.

The "FRT split" rule (per user request 2026-05-18):
  - When a ticket waits across a shift boundary, each shift's on-duty agent
    is accountable for the slice of time the user waited on their watch.
  - The responding agent is credited with `response_time - their_shift_start`
    (or `response_time - last_user_msg` if the response happens within a
    single shift).
  - The missing agent(s) are blamed for `shift_end - last_user_msg` (or
    `shift_end - shift_start` for fully-missed middle shifts).

This module reads `tickets` rows and returns per-agent rollups that
faithfully reflect that split, instead of attributing the whole ticket's
FRT to the single responding agent.
"""

from datetime import datetime, timezone
from statistics import median

import config


def _to_utc(s):
    if not s:
        return None
    dt = datetime.fromisoformat(s) if isinstance(s, str) else s
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def contributions_for_ticket(ticket):
    """Return the per-agent FRT contribution list for one ticket, or [] if
    the ticket has no agent response yet (FRT is undefined)."""
    end = _to_utc(ticket.get('first_responded_at'))
    if end is None:
        return []
    # Prefer the user's last activity before the response as the wait clock
    # start (so re-pings reset). Fall back to ticket creation.
    start = _to_utc(ticket.get('last_user_msg_at')) or _to_utc(ticket.get('created_at'))
    if start is None or start >= end:
        return []
    responder = ticket.get('agent_name')
    return config.compute_frt_contributions(start, end, responding_agent=responder)


def aggregate_per_agent(tickets, sla_threshold_mins=None):
    """Walk every responded ticket, split FRT across shift boundaries, and
    bucket the contributions by agent.

    Returns dict keyed by agent_name:
      {
        'frts':       list of contribution minutes that count toward this
                       agent's avg/median (includes BOTH their on-shift work
                       AND any cross-shift help they gave),
        'missed_mins':list of minutes for tickets where this agent was on
                       duty but did NOT respond before someone else did,
        'breaches':   list of (ticket_id, mins) where contribution > SLA,
        'fastest':    (ticket_id, mins) lowest-mins contribution this agent
                       had — None if no contributions,
        'slowest':    same, highest-mins,
        'on_shift':   count of tickets where this agent was the on-duty
                       agent (i.e. the wait started during their shift),
        'responded':  count of tickets this agent actually responded to,
        'missed':     count of tickets where they were on duty for a segment
                       but did not respond,
        'cross_help': count of tickets they responded to OUTSIDE their own
                       on-duty shift.
      }
    """
    out = {}
    sla = sla_threshold_mins if sla_threshold_mins is not None else config.SLA_FRT_THRESHOLD_MINS

    def slot(name):
        if name not in out:
            out[name] = {
                'frts': [],
                'missed_mins': [],
                'breaches': [],
                'fastest': None,
                'slowest': None,
                'on_shift': 0,
                'responded': 0,
                'missed': 0,
                'cross_help': 0,
            }
        return out[name]

    for t in tickets:
        contribs = contributions_for_ticket(t)
        if not contribs:
            continue
        tid = t['ticket_id']
        on_duty = t.get('on_duty_agent_name')
        responder = t.get('agent_name')
        if on_duty:
            slot(on_duty)['on_shift'] += 1
        if responder and on_duty and responder == on_duty:
            slot(responder)['responded'] += 1
        elif responder and on_duty and responder != on_duty:
            slot(responder)['cross_help'] += 1
            slot(on_duty)['missed'] += 1
        elif responder and not on_duty:
            slot(responder)['responded'] += 1

        for c in contribs:
            agent = c['agent']
            if not agent:
                continue
            s = slot(agent)
            mins = c['mins']
            ctype = c['type']
            if ctype in ('responded', 'cross_help'):
                s['frts'].append(mins)
            elif ctype == 'missed':
                s['missed_mins'].append(mins)
                # Missed contributions are also counted as a "wait you owned"
                # for FRT purposes, per user rule.
                s['frts'].append(mins)
            if mins > sla:
                s['breaches'].append((tid, round(mins, 2)))
            if s['fastest'] is None or mins < s['fastest'][1]:
                s['fastest'] = (tid, round(mins, 2))
            if s['slowest'] is None or mins > s['slowest'][1]:
                s['slowest'] = (tid, round(mins, 2))

    return out


def summarize(agg_for_agent):
    """Turn one agent's aggregate dict into a display-ready summary."""
    frts = agg_for_agent['frts']
    n = len(frts)
    avg = round(sum(frts) / n, 2) if n else None
    med = round(median(frts), 2) if n else None
    p90 = None
    if frts:
        s = sorted(frts)
        idx = min(int(len(s) * 0.9), len(s) - 1)
        p90 = round(s[idx], 2)
    sla = (
        round((1 - len(agg_for_agent['breaches']) / n) * 100, 1)
        if n else None
    )
    return {
        'count_with_frt': n,
        'avg_frt': avg,
        'median_frt': med,
        'p90_frt': p90,
        'sla_compliance': sla,
        'breaches': agg_for_agent['breaches'],
        'fastest': agg_for_agent['fastest'],
        'slowest': agg_for_agent['slowest'],
        'on_shift': agg_for_agent['on_shift'],
        'responded': agg_for_agent['responded'],
        'missed': agg_for_agent['missed'],
        'cross_help': agg_for_agent['cross_help'],
    }
