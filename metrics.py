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
    # FRT clock = first user message that triggered the ticket (= created_at).
    # Do NOT use last_user_msg_at: that field is rolling and gets overwritten
    # by user follow-ups AFTER first_responded_at, which would either zero out
    # the wait or invert it (start > end).
    start = _to_utc(ticket.get('created_at'))
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
                # Tickets where this agent was on-duty but a shift buddy
                # covered the reply (no miss, no responded credit). Useful
                # for surfacing buddy support on the dashboard.
                'buddy_covered': 0,
                'buddy_covered_by': {},  # {buddy_name: count}
                'buddy_covered_tickets': [],  # list of ticket ids
                # Tickets where this agent was on-duty when a PREVIOUS shift's
                # owner answered their own ticket late, spilling into this
                # agent's shift. Not a miss — just on their radar to follow up.
                'followup': 0,
                'followup_tickets': [],  # list of ticket ids
            }
        return out[name]

    for t in tickets:
        contribs = contributions_for_ticket(t)
        if not contribs:
            continue
        tid = t['ticket_id']
        on_duty = t.get('on_duty_agent_name')  # on-duty at ticket creation
        if on_duty:
            slot(on_duty)['on_shift'] += 1

        # Per-ticket manual override: if a reviewer flagged this ticket as
        # buddy-covered by someone (e.g. Reus handled a community-rewards
        # ticket during Mikaelson's shift but outside the time-windowed
        # buddy table), rewrite the on-duty's 'missed' contribution into a
        # 'buddy_covered' marker so the dashboard does not count it as a
        # miss. The cross-helper's own contribution stays intact.
        manual_buddy = t.get('manual_buddy_covered_by')
        if manual_buddy and on_duty:
            contribs = [
                {**c, 'type': 'buddy_covered', 'mins': 0.0,
                 'buddy': manual_buddy}
                if c['type'] == 'missed' and c['agent'] == on_duty
                else c
                for c in contribs
            ]

        # Derive responded / cross_help / missed counts directly from the
        # shift-split contributions: an agent's segment type IS the verdict.
        # Cross-help = agent replied OUTSIDE their own shift; we never label
        # a same-shift response as cross-help even if the ticket originated
        # in another shift.
        for c in contribs:
            ctype = c['type']
            if ctype == 'responded':
                slot(c['agent'])['responded'] += 1
            elif ctype == 'cross_help':
                slot(c['agent'])['cross_help'] += 1
            elif ctype == 'buddy_covered' and c.get('agent'):
                s = slot(c['agent'])
                s['buddy_covered'] += 1
                buddy = c.get('buddy')
                if buddy:
                    s['buddy_covered_by'][buddy] = s['buddy_covered_by'].get(buddy, 0) + 1
                s['buddy_covered_tickets'].append(tid)
            elif ctype == 'followup' and c.get('agent'):
                s = slot(c['agent'])
                s['followup'] += 1
                s['followup_tickets'].append(tid)
        for ag in {c['agent'] for c in contribs if c['type'] == 'missed' and c['agent']}:
            slot(ag)['missed'] += 1

        for c in contribs:
            agent = c['agent']
            if not agent:
                continue
            s = slot(agent)
            mins = c['mins']
            ctype = c['type']
            # buddy_covered / followup are markers only (mins=0); skip
            # FRT/SLA scoring so they never count toward a miss or average.
            if ctype in ('buddy_covered', 'followup'):
                continue
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


def aggregate_response_events(events):
    """Apply the FRT-split accountability rule to ticket_response_events
    rows and bucket by agent.

    For each event row, runs config.compute_frt_contributions over
    (user_msg_at, agent_msg_at, responder) so each agent's segment
    contribution is tallied — including 'missed' segments charged to the
    on-duty agent up to their shift end when a cross-helper picked up.

    Returns dict keyed by canonical agent_name:
      {
        'all_mins':       list[float]  — every contribution (responded /
                                          cross_help / missed),
        'first_mins':     list[float]  — contributions from event_type == 'first',
        'followup_mins':  list[float]  — contributions from 'followup' events,
        'missed_mins':    list[float]  — strictly the 'missed' contributions,
        'cross_help_mins':list[float]  — strictly the 'cross_help' contributions,
        'responded_mins': list[float]  — strictly the 'responded' contributions,
      }
    """
    out = {}
    for e in events:
        responder = e.get('agent_name')
        if not responder:
            continue
        user_at = _to_utc(e.get('user_msg_at'))
        agent_at = _to_utc(e.get('agent_msg_at'))
        if user_at is None or agent_at is None or user_at >= agent_at:
            continue
        contribs = config.compute_frt_contributions(user_at, agent_at, responding_agent=responder)
        # One-off waiver: a ticket flagged manual_buddy_covered_by is treated
        # as fully covered — rewrite EVERY 'missed' segment (any agent) on this
        # ticket into a zero-minute buddy_covered marker so it never charges a
        # miss, mirroring aggregate_per_agent (which waives the on-duty agent).
        manual_buddy = e.get('manual_buddy_covered_by')
        if manual_buddy:
            contribs = [
                {**c, 'type': 'buddy_covered', 'mins': 0.0, 'buddy': manual_buddy}
                if c.get('type') == 'missed' else c
                for c in contribs
            ]
        et = e.get('event_type')
        for c in contribs:
            agent = c.get('agent')
            mins = c.get('mins')
            ctype = c.get('type')
            if not agent or mins is None:
                continue
            # buddy_covered is a zero-minute marker — never count it toward any
            # response-time stat (all/first/followup/missed/cross_help).
            if ctype == 'buddy_covered':
                continue
            slot = out.setdefault(agent, {
                'all_mins': [],
                'first_mins': [],
                'followup_mins': [],
                'missed_mins': [],
                'cross_help_mins': [],
                'responded_mins': [],
            })
            slot['all_mins'].append(mins)
            if et == 'first':
                slot['first_mins'].append(mins)
            elif et == 'followup':
                slot['followup_mins'].append(mins)
            if ctype == 'missed':
                slot['missed_mins'].append(mins)
            elif ctype == 'cross_help':
                slot['cross_help_mins'].append(mins)
            elif ctype == 'responded':
                slot['responded_mins'].append(mins)
    return out


def summarize_responses(agg_for_agent):
    """Mean / count stats for one agent across split contributions."""
    def _mean(lst):
        return round(sum(lst) / len(lst), 2) if lst else None
    def _sum(lst):
        return round(sum(lst), 2) if lst else 0.0
    return {
        'count_all':             len(agg_for_agent['all_mins']),
        'count_first':           len(agg_for_agent['first_mins']),
        'count_followup':        len(agg_for_agent['followup_mins']),
        'count_missed':          len(agg_for_agent['missed_mins']),
        'count_cross_help':      len(agg_for_agent['cross_help_mins']),
        'count_responded':       len(agg_for_agent['responded_mins']),
        'avg_response_all':      _mean(agg_for_agent['all_mins']),
        'avg_response_first':    _mean(agg_for_agent['first_mins']),
        'avg_response_followup': _mean(agg_for_agent['followup_mins']),
        'total_missed_mins':     _sum(agg_for_agent['missed_mins']),
        'total_cross_help_mins': _sum(agg_for_agent['cross_help_mins']),
    }


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
        'buddy_covered': agg_for_agent.get('buddy_covered', 0),
        'buddy_covered_by': agg_for_agent.get('buddy_covered_by', {}),
        'buddy_covered_tickets': agg_for_agent.get('buddy_covered_tickets', []),
    }
