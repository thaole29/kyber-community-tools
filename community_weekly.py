"""
community_weekly.py

Weekly community rollup. Reads the last 7 days of per-channel digest JSON
from the community_digests table, asks Claude Opus 4.7 to surface trending
topics, recurring complaints, sentiment trend, and contributor counts
(no usernames), then posts the result to the Telegram staff group.

Run via cron every Monday at 00:10 UTC.
"""

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

from google import genai
from google.genai import types as genai_types

import config
import database
from community_digest import send_telegram, _strip_code_fence, GEMINI_MODEL


WEEKLY_MAX_TOKENS = 3000


WEEKLY_PROMPT = """You are aggregating 7 days of daily community digests from the Kyber Network Discord. Each daily digest is itself a JSON object summarizing one channel's activity for that day. Below is the full set of digests, grouped by date.

CRITICAL PRIVACY RULES — DO NOT include any usernames, user_N aliases, or @mentions in your output. Aggregate by counts only.

Produce ONE JSON object only — no prose, no code fences. Schema:
{{
  "week_start": "{start_date}",
  "week_end": "{end_date}",
  "trending_topics": [
    {{
      "title": "<short title>",
      "appeared_in_days": <int 1-7>,
      "channels": ["<channel>"],
      "summary": "<2-3 sentences>",
      "sentiment": "positive|neutral|negative|mixed"
    }}
  ],
  "recurring_complaints": [
    {{"complaint": "<text>", "days_observed": <int>, "channels": ["<channel>"]}}
  ],
  "top_feature_requests": ["<request>"],
  "sentiment_trend": {{
    "direction": "improving|declining|stable|mixed",
    "explanation": "<1-2 sentences referencing what changed>"
  }},
  "contributor_volume": {{
    "total_active_count": <int across the week, deduped if possible from per-day numbers>,
    "trend": "growing|shrinking|stable"
  }},
  "weekly_action_items": ["<staff follow-up>"]
}}

Only include "trending_topics" entries that appeared on 3+ different days.
Only include "recurring_complaints" that appeared on 2+ different days.

Daily digests:
---
{digests_block}
---"""


def build_digests_block(rows):
    """Group stored digests by date and produce a compact text block."""
    by_date = {}
    for row in rows:
        by_date.setdefault(row['digest_date'], []).append({
            'channel': row['channel'],
            'digest': row['digest'],
        })

    parts = []
    for date in sorted(by_date.keys()):
        parts.append(f"### {date}")
        for item in by_date[date]:
            digest = item['digest']
            parts.append(f"  channel: {item['channel']}")
            parts.append("  " + json.dumps(digest, ensure_ascii=False))
        parts.append("")
    return "\n".join(parts)


def format_telegram_weekly(start_date, end_date, summary):
    esc = config.html_escape
    lines = [
        f"📅 <b>Weekly Community Rollup — {esc(start_date)} → {esc(end_date)}</b>",
        "",
    ]

    trending = summary.get("trending_topics") or []
    if trending:
        lines.append("<b>🔥 Trending Topics</b>")
        for t in trending[:5]:
            title = esc(t.get("title", ""))
            days = t.get("appeared_in_days")
            chans = ", ".join(esc(c) for c in (t.get("channels") or [])[:4])
            sent = esc(t.get("sentiment", "neutral"))
            summ = esc(t.get("summary", ""))
            meta = []
            if isinstance(days, int):
                meta.append(f"{days}/7 days")
            if chans:
                meta.append(chans)
            meta_str = " · ".join(meta)
            lines.append(f"  • <b>{title}</b> <i>({meta_str}; {sent})</i>")
            if summ:
                lines.append(f"      {summ}")
        lines.append("")

    complaints = summary.get("recurring_complaints") or []
    if complaints:
        lines.append("<b>🔴 Recurring Complaints</b>")
        for c in complaints[:5]:
            text = esc(c.get("complaint", ""))
            days = c.get("days_observed")
            chans = ", ".join(esc(ch) for ch in (c.get("channels") or [])[:4])
            meta = []
            if isinstance(days, int):
                meta.append(f"{days} days")
            if chans:
                meta.append(chans)
            meta_str = f" <i>({' · '.join(meta)})</i>" if meta else ""
            lines.append(f"  • {text}{meta_str}")
        lines.append("")

    feats = summary.get("top_feature_requests") or []
    if feats:
        lines.append("<b>💡 Top Feature Requests</b>")
        for f in feats[:5]:
            lines.append(f"  • {esc(f)}")
        lines.append("")

    trend = summary.get("sentiment_trend") or {}
    if trend:
        direction = esc(trend.get("direction", "stable"))
        explanation = esc(trend.get("explanation", ""))
        arrow = {
            "improving": "📈",
            "declining": "📉",
            "stable":    "→",
            "mixed":     "⇅",
        }.get(trend.get("direction"), "→")
        lines.append(f"<b>{arrow} Sentiment Trend:</b> {direction}")
        if explanation:
            lines.append(f"  <i>{explanation}</i>")
        lines.append("")

    contrib = summary.get("contributor_volume") or {}
    if contrib:
        total = contrib.get("total_active_count")
        ctrend = esc(contrib.get("trend", ""))
        if isinstance(total, int) or ctrend:
            parts = []
            if isinstance(total, int):
                parts.append(f"{total} active contributors")
            if ctrend:
                parts.append(ctrend)
            lines.append(f"<b>👥 Contributors:</b> {esc(' — '.join(parts))}")
            lines.append("")

    actions = summary.get("weekly_action_items") or []
    if actions:
        lines.append("<b>🔧 Weekly Action Items</b>")
        for i, a in enumerate(actions, 1):
            lines.append(f"  {i}. {esc(a)}")
        lines.append("")

    if len(lines) <= 2:
        lines.append("<i>No significant trends in the past week.</i>")

    return "\n".join(lines)


async def run_weekly():
    if not config.GEMINI_API_KEY:
        print("❌ GEMINI_API_KEY not set.")
        return

    database.init_db()

    end_utc = datetime.now(tz=timezone.utc)
    start_utc = end_utc - timedelta(days=7)
    start_date = start_utc.strftime('%Y-%m-%d')
    end_date = end_utc.strftime('%Y-%m-%d')

    rows = database.get_community_digests_in_range(start_date, end_date)
    if not rows:
        print(f"⚠️  No community digests found between {start_date} and {end_date}.")
        await send_telegram(
            f"<i>Weekly community rollup: no digests stored between "
            f"{config.html_escape(start_date)} and {config.html_escape(end_date)}.</i>"
        )
        return

    digests_block = build_digests_block(rows)
    prompt = WEEKLY_PROMPT.format(
        start_date=start_date,
        end_date=end_date,
        digests_block=digests_block,
    )

    gemini = genai.Client(api_key=config.GEMINI_API_KEY)
    try:
        resp = await gemini.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                max_output_tokens=WEEKLY_MAX_TOKENS,
                response_mime_type="application/json",
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
            ),
        )
        text = _strip_code_fence(resp.text or "")
        summary = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"[ERROR] JSON parse failed: {e}")
        await send_telegram(
            "<i>Weekly community rollup failed: LLM returned invalid JSON.</i>"
        )
        return
    except Exception as e:
        print(f"[ERROR] LLM call failed: {e}")
        await send_telegram(
            f"<i>Weekly community rollup failed: {config.html_escape(str(e))}</i>"
        )
        return

    digest_text = format_telegram_weekly(start_date, end_date, summary)
    await send_telegram(digest_text)
    print("✅ Weekly community rollup delivered.")


if __name__ == "__main__":
    asyncio.run(run_weekly())
