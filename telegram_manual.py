"""
telegram_manual.py

Manual entry for Telegram responsiveness, for use before (or alongside) the
automatic watcher in telegram_watch.py.

Rows land in the SAME `telegram_mentions` table the watcher writes to — only
`source` differs ('manual' vs 'watcher') — so the daily report and any future
dashboard read one dataset. Switching the watcher on later needs no migration
and no backfill.

Same rule as the watcher: a tag is only judged when it lands inside that
agent's own shift. Off-shift tags are stored with on_shift=0 and never counted
as slow.

Times are LOCAL (UTC+7) unless --utc is passed. Accepted forms:
    14:30                 today at 14:30 local
    2026-07-27 14:30      explicit date
    30m / 2h              that long ago
    now                   right now

Usage
-----
  # tag and reply in one line (the common case, filled in after the fact)
  python telegram_manual.py log Mikaelson --at 14:30 --resp 15:05 --by @lead \
      --note "hỏi về refund"

  # open a tag now, close it when they answer
  python telegram_manual.py add Mikaelson --by @lead --note "ping vụ payout"
  python telegram_manual.py respond Mikaelson

  python telegram_manual.py list --days 7        # everything this week
  python telegram_manual.py list --open          # still waiting
  python telegram_manual.py rm 12                # delete a mistyped row
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import config     # noqa: E402
import database   # noqa: E402

MANUAL_CHAT = 'manual'
REL_RE = re.compile(r'^(\d+)\s*(m|min|mins|h|hour|hours)$', re.IGNORECASE)


def parse_when(text, utc=False):
    """Turn a human time string into an aware UTC datetime."""
    if not text or text.lower() == 'now':
        return datetime.now(timezone.utc)
    text = text.strip()

    rel = REL_RE.match(text)
    if rel:
        n = int(rel.group(1))
        unit = rel.group(2).lower()
        delta = timedelta(hours=n) if unit.startswith('h') else timedelta(minutes=n)
        return datetime.now(timezone.utc) - delta

    tz = timezone.utc if utc else config.LOCAL_TZ
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%H:%M:%S', '%H:%M'):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        bare_time = fmt.startswith('%H')
        if bare_time:
            today = datetime.now(tz)
            dt = dt.replace(year=today.year, month=today.month, day=today.day)
        out = dt.replace(tzinfo=tz).astimezone(timezone.utc)
        now = datetime.now(timezone.utc)
        if out > now + timedelta(minutes=5):
            if bare_time:
                # "22:30" typed at 15:00 means last night, not tonight — you log
                # what already happened. A future row would silently vanish from
                # every report, which filters on `mentioned_at < now`.
                out -= timedelta(days=1)
                print(f"ℹ️  {text} là giờ tương lai hôm nay → hiểu là hôm qua "
                      f"({out.astimezone(config.LOCAL_TZ):%Y-%m-%d %H:%M} local).")
            else:
                print(f"⚠️  {text} nằm ở tương lai — bản ghi này sẽ không hiện "
                      f"trong report cho tới khi tới giờ đó.")
        return out

    raise SystemExit(f"Không hiểu thời gian {text!r}. Dùng 14:30, "
                     f"'2026-07-27 14:30', 30m, 2h, hoặc now.")


def canonical(agent):
    """Accept any spelling the project already knows and normalise it."""
    name = config.normalize_agent(agent)
    known = {s['agent'] for s in config.SHIFTS} | set(config.AGENT_DISCORD_IDS.values())
    if name not in known:
        raise SystemExit(f"Agent không hợp lệ: {agent!r}. Hợp lệ: {', '.join(sorted(known))}")
    return name


def _synthetic_id(at_utc):
    """Manual rows have no Telegram message. Use a negative id derived from the
    timestamp so the UNIQUE(chat, message, agent) guard still stops accidental
    double entry of the same tag."""
    return -int(at_utc.timestamp())


def _fmt_local(iso):
    return datetime.fromisoformat(iso).astimezone(config.LOCAL_TZ).strftime('%m-%d %H:%M')


def cmd_add(args, at=None):
    at_utc = at or parse_when(args.at, args.utc)
    agent = canonical(args.agent)
    shift_label, on_duty = config.get_on_duty_agent(at_utc)
    on_shift = (on_duty == agent)
    added = database.record_telegram_mention(
        chat_id=MANUAL_CHAT,
        message_id=_synthetic_id(at_utc),
        agent_name=agent,
        mentioned_by=args.by,
        mentioned_at=at_utc.isoformat(),
        mention_text=args.note,
        shift_label=shift_label,
        on_shift=on_shift,
        source='manual',
    )
    if not added:
        print(f"⚠️  Đã có tag của {agent} tại đúng thời điểm này — bỏ qua (chống nhập trùng).")
        return None
    where = (f"trong ca {shift_label}" if on_shift
             else f"NGOÀI ca (ca {shift_label} là của {on_duty}) — sẽ không bị chấm slow")
    print(f"✅ Ghi tag {agent} lúc {_fmt_local(at_utc.isoformat())} — {where}")
    return at_utc


def cmd_respond(args, at=None):
    at_utc = at or parse_when(args.at, args.utc)
    agent = canonical(args.agent)
    closed = database.resolve_telegram_mentions(
        MANUAL_CHAT, agent, at_utc.isoformat(), args.kind)
    if not closed:
        print(f"⚠️  {agent} không có tag nào đang mở trước {_fmt_local(at_utc.isoformat())}.")
        return
    for c in closed:
        flag = ' ⚠️ SLOW' if c['slow'] else ''
        print(f"✅ {agent}: {config.fmt_mins(c['response_mins'])}{flag} "
              f"(tag lúc {_fmt_local(c['mentioned_at'])})")


def cmd_log(args):
    """One-shot: record the tag and its reply together."""
    at_utc = cmd_add(args)
    if at_utc is None:
        return
    resp_utc = parse_when(args.resp, args.utc)
    if resp_utc < at_utc:
        raise SystemExit("Thời điểm trả lời sớm hơn thời điểm bị tag — kiểm tra lại.")
    args.kind = args.kind or 'message'
    cmd_respond(args, at=resp_utc)


def cmd_list(args):
    # Stamp overdue-but-unanswered tags before reading, so the CLI is truthful
    # even when bot.py (which owns the 5-minute sweep loop) is not running.
    database.flag_overdue_telegram_mentions()
    end = datetime.now(timezone.utc) + timedelta(minutes=1)
    start = end - timedelta(days=args.days)
    rows = database.get_telegram_mentions_in_range(start, end)
    if args.open:
        rows = [r for r in rows if r['responded_at'] is None]
    if args.agent:
        rows = [r for r in rows if r['agent_name'] == canonical(args.agent)]
    if not rows:
        print("(không có bản ghi nào)")
        return
    print(f"{'id':>4}  {'agent':14}{'tagged':12}{'ca':4}{'mins':>8}  {'trạng thái':12}{'nguồn':8}by")
    now = datetime.now(timezone.utc)
    for r in rows:
        if r['responded_at']:
            mins = f"{r['response_mins']:.1f}"
            state = 'SLOW' if r['slow'] else 'ok'
        else:
            waited = (now - datetime.fromisoformat(r['mentioned_at'])).total_seconds() / 60
            mins = f"{waited:.1f}"
            state = 'SLOW/chờ' if r['slow'] else 'đang chờ'
        scope = r['shift_label'] if r['on_shift'] else '—'
        print(f"{r['id']:>4}  {r['agent_name']:14}{_fmt_local(r['mentioned_at']):12}"
              f"{scope or '-':4}{mins:>8}  {state:12}{(r['source'] or '?'):8}{r['mentioned_by'] or ''}")
    counted = [r for r in rows if r['on_shift']]
    slow = [r for r in counted if r['slow']]
    print(f"\n{len(rows)} tag | {len(counted)} tính điểm (trong ca) | {len(slow)} slow "
          f"(ngưỡng {config.TELEGRAM_MENTION_SLA_MINS}′)")


def cmd_remind(args):
    """CLI twin of /record, for when you are at the terminal instead of Telegram."""
    at_utc = parse_when(args.at, args.utc)
    agent = canonical(args.agent)
    shift_label, on_duty = config.get_on_duty_agent(at_utc)
    on_shift = (on_duty == agent)
    rid = database.record_agent_reminder(
        agent_name=agent, note=args.note, recorded_by=args.by or 'cli',
        recorded_at=at_utc.isoformat(), shift_label=shift_label,
        on_shift=on_shift, source='cli')
    where = (f"during their shift {shift_label}" if on_shift
             else f"outside their shift (shift {shift_label} belongs to {on_duty})")
    print(f"📝 Reminder #{rid} recorded — {agent} at "
          f"{_fmt_local(at_utc.isoformat())} — {where}")


def cmd_reminders(args):
    end = datetime.now(timezone.utc) + timedelta(minutes=1)
    rows = database.get_agent_reminders_in_range(
        end - timedelta(days=args.days), end,
        agent_name=canonical(args.agent) if args.agent else None)
    if not rows:
        print("(no reminders)")
        return
    per = {}
    for r in rows:
        per.setdefault(r['agent_name'], []).append(r)
    print(f"Reminders — last {args.days} day(s) — {len(rows)} total")
    for name in sorted(per, key=lambda k: -len(per[k])):
        print(f"  {name}: {len(per[name])}")
    print()
    for r in rows:
        note = f" — {r['note']}" if r['note'] else ""
        print(f"  #{r['id']:>4} {_fmt_local(r['recorded_at'])} {r['agent_name']:14}"
              f"{(r['recorded_by'] or '?'):12}{note}")


def cmd_rm(args):
    conn = database.get_connection()
    try:
        row = conn.execute("SELECT * FROM telegram_mentions WHERE id = ?",
                           (args.id,)).fetchone()
        if not row:
            raise SystemExit(f"Không có row id={args.id}")
        conn.execute("DELETE FROM telegram_mentions WHERE id = ?", (args.id,))
        conn.commit()
        print(f"🗑️  Đã xoá id={args.id}: {row['agent_name']} "
              f"tag lúc {_fmt_local(row['mentioned_at'])}")
    finally:
        conn.close()


def build_parser():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--utc', action='store_true',
                   help='giờ nhập vào là UTC (mặc định là giờ local UTC+7)')
    sub = p.add_subparsers(dest='cmd', required=True)

    a = sub.add_parser('add', help='mở một tag (chưa có câu trả lời)')
    a.add_argument('agent')
    a.add_argument('--at', default='now', help='lúc bị tag (mặc định: bây giờ)')
    a.add_argument('--by', help='ai tag, vd @teamlead')
    a.add_argument('--note', help='nội dung / ngữ cảnh ngắn')
    a.set_defaults(func=cmd_add)

    r = sub.add_parser('respond', help='đóng tag đang mở của một agent')
    r.add_argument('agent')
    r.add_argument('--at', default='now', help='lúc trả lời (mặc định: bây giờ)')
    r.add_argument('--kind', default='message', choices=['message', 'reaction'])
    r.set_defaults(func=cmd_respond)

    l = sub.add_parser('log', help='ghi cả tag lẫn câu trả lời trong một lệnh')
    l.add_argument('agent')
    l.add_argument('--at', required=True, help='lúc bị tag')
    l.add_argument('--resp', required=True, help='lúc trả lời')
    l.add_argument('--by', help='ai tag')
    l.add_argument('--note', help='nội dung ngắn')
    l.add_argument('--kind', default='message', choices=['message', 'reaction'])
    l.set_defaults(func=cmd_log)

    s = sub.add_parser('list', help='xem các bản ghi gần đây')
    s.add_argument('--days', type=int, default=1)
    s.add_argument('--open', action='store_true', help='chỉ hiện tag chưa trả lời')
    s.add_argument('--agent')
    s.set_defaults(func=cmd_list)

    rem = sub.add_parser('remind', help='log one response-time reminder (same as /record)')
    rem.add_argument('agent')
    rem.add_argument('--at', default='now')
    rem.add_argument('--by', help='who logged it')
    rem.add_argument('--note')
    rem.set_defaults(func=cmd_remind)

    rems = sub.add_parser('reminders', help='list logged reminders')
    rems.add_argument('--days', type=int, default=7)
    rems.add_argument('--agent')
    rems.set_defaults(func=cmd_reminders)

    d = sub.add_parser('rm', help='xoá một row nhập nhầm')
    d.add_argument('id', type=int)
    d.set_defaults(func=cmd_rm)
    return p


if __name__ == '__main__':
    database.init_db()
    args = build_parser().parse_args()
    args.func(args)
