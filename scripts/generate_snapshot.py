"""
scripts/generate_snapshot.py

Generates static JSON snapshots of the dashboard payload for 3 preset
ranges (24h / 7d / 30d) and writes them into dashboard/web/public/data/.

Used by the daily 09:00 UTC+7 cron (Option B1). After running this, the
Vite build picks the JSON files up via `public/` and the GitHub Pages
deploy workflow serves them at:

    /data/community_24h.json
    /data/community_7d.json
    /data/community_30d.json
    /data/support_24h.json
    /data/support_7d.json
    /data/support_30d.json
    /data/meta.json

The frontend reads `/data/meta.json` to discover available ranges and
the last refresh time.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from dashboard import api as dashboard_api  # noqa: E402

OUT_DIR = PROJECT_DIR / 'dashboard' / 'web' / 'public' / 'data'

RANGES = [
    ('24h', timedelta(hours=24), 'Last 24 hours'),
    ('7d',  timedelta(days=7),   'Last 7 days'),
    ('30d', timedelta(days=30),  'Last 30 days'),
]


def _atomic_write(path: Path, payload: dict) -> None:
    """Write JSON atomically — temp file + rename — so a concurrent reader
    never observes a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, separators=(',', ':'))
    tmp.replace(path)


def main() -> int:
    now = datetime.now(tz=timezone.utc)
    print(f'[snapshot] generating at {now.isoformat()}', flush=True)

    meta = {
        'generated_at_utc': now.isoformat(),
        'ranges': [],
    }
    for key, delta, label in RANGES:
        start_dt = now - delta
        community = dashboard_api.build_community_payload(start_dt, now, label)
        support = dashboard_api.build_support_payload(start_dt, now, label)
        _atomic_write(OUT_DIR / f'community_{key}.json', community)
        _atomic_write(OUT_DIR / f'support_{key}.json', support)
        meta['ranges'].append({
            'key': key,
            'label': label,
            'community_tickets': community.get('totalMessages', 0),
            'support_tickets': support.get('totalTickets', 0),
        })
        print(f'[snapshot] {key:4} → community({community.get("totalMessages")} msgs) '
              f'support({support.get("totalTickets")} tickets)', flush=True)

    _atomic_write(OUT_DIR / 'meta.json', meta)
    print(f'[snapshot] wrote {len(RANGES)*2 + 1} files to {OUT_DIR}', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
