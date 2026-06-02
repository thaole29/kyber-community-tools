"""
classify_satisfaction.py

Per-ticket user-satisfaction classification (prototype).

For each ticket in a target window, pulls the full transcript from
#archieved, extracts user-side messages POST first agent reply, and asks
Gemini whether the user seemed satisfied with the agent's support. Caches
the verdict on `tickets` (satisfaction_label / score / signals / source /
classified_at).

Run:
    venv/bin/python classify_satisfaction.py --days 7
    venv/bin/python classify_satisfaction.py --ticket ticket-2073

Reuses transcript fetching helpers from transcript_backfill and the
Gemini call pattern from classify_tickets.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from google import genai
from google.genai import types as genai_types

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import config  # noqa: E402
import database  # noqa: E402
from transcript_backfill import (  # noqa: E402
    parse_transcript_html,
    split_user_vs_agent,
    ARCHIEVED_CHANNEL_ID,
    TRANSCRIPT_FILE_RE,
)


VALID_LABELS = {"positive", "neutral", "negative", "no_signal"}


SYSTEM_PROMPT = """You judge whether a KyberSwap support-ticket user was \
satisfied with the agent's help.

INPUT for ONE ticket:
  - The user's initial complaint (for context).
  - The user's follow-up messages AFTER the first agent reply, in order.
  - (Optional) The agent's first reply for context.

You must judge USER SATISFACTION ONLY — do NOT judge whether the agent \
was correct or fast. Focus on the user's tone and what they actually \
said after being helped.

Return ONE JSON object with these fields:
  - label      : one of "positive", "neutral", "negative", "no_signal"
  - score      : float in [-1.0, 1.0]   (−1 strongly unhappy → +1 strongly happy)
  - signals    : array of 1-4 SHORT phrases citing what made you decide \
(e.g. "thanked agent", "said 'fixed!'", "complained about wait time", \
"no follow-up messages at all").
  - confidence : float in [0.0, 1.0]    (how sure you are)

LABEL RULES:
  - "positive"  : user thanked the agent, confirmed resolution, used \
gratitude language ("thanks", "got it", "fixed", "appreciate", 🙏, ❤️).
  - "negative"  : user expressed frustration, said it was unresolved, \
complained about quality/speed, used hostile language.
  - "neutral"   : user replied factually with no clear positive/negative \
tone (e.g. provided more info, asked unrelated question).
  - "no_signal" : user wrote NOTHING after the agent's first reply. \
The ticket closed silently. Do NOT guess — use this label.

Language: the user may write English, Vietnamese, or other. Judge based \
on meaning, not surface keywords.

Respond with JSON only — no markdown, no code fence."""


_WALLET_RE = re.compile(r"\b0x[a-fA-F0-9]{20,}\b")
_LONG_HEX_RE = re.compile(r"\b[a-fA-F0-9]{40,}\b")


def _clean(text: str, cap: int = 500) -> str:
    if not text:
        return ""
    text = _WALLET_RE.sub("[addr]", text)
    text = _LONG_HEX_RE.sub("[hex]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:cap]


def _build_prompt(ticket_id: str, initial: str,
                  agent_first_reply: str,
                  post_reply_user_msgs: list[str]) -> str:
    lines = [SYSTEM_PROMPT, "", f"TICKET: {ticket_id}", "",
             "INITIAL USER COMPLAINT:",
             _clean(initial) or "(empty)",
             "",
             "AGENT FIRST REPLY (for context only):",
             _clean(agent_first_reply, cap=400) or "(none recorded)",
             "",
             "USER MESSAGES AFTER FIRST AGENT REPLY:"]
    if not post_reply_user_msgs:
        lines.append("(none — user did not reply after agent's first response)")
    else:
        for i, m in enumerate(post_reply_user_msgs, 1):
            lines.append(f"  {i}. {_clean(m, cap=300)}")
    return "\n".join(lines)


class _TransientLLMError(Exception):
    pass


async def _call_gemini(gemini, prompt: str, retries: int = 2):
    last_err = None
    for attempt in range(retries + 1):
        try:
            return await gemini.aio.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    max_output_tokens=600,
                    response_mime_type="application/json",
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                ),
            )
        except Exception as e:
            msg = str(e)
            transient = any(s in msg for s in ("429", "RESOURCE_EXHAUSTED",
                                                "503", "500", "UNAVAILABLE"))
            if not transient or attempt >= retries:
                last_err = e
                break
            delay = 16
            m = re.search(r"retry in (\d+(?:\.\d+)?)s", msg)
            if m:
                delay = min(30, float(m.group(1)) + 1)
            print(f"[SAT] transient {msg[:80]}; retry in {delay:.0f}s", flush=True)
            await asyncio.sleep(delay)
    raise _TransientLLMError(str(last_err) if last_err else "exhausted")


def _validate(parsed: dict) -> dict | None:
    label = parsed.get("label")
    if label not in VALID_LABELS:
        return None
    try:
        score = float(parsed.get("score", 0.0))
        conf = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        return None
    score = max(-1.0, min(1.0, score))
    conf = max(0.0, min(1.0, conf))
    signals = parsed.get("signals") or []
    if not isinstance(signals, list):
        signals = []
    signals = [str(s)[:120] for s in signals[:4]]
    return {"label": label, "score": score, "signals": signals, "confidence": conf}


def _save(ticket_id: str, verdict: dict, source: str) -> None:
    import sqlite3
    conn = sqlite3.connect(config.DB_FILE)
    conn.execute(
        "UPDATE tickets SET "
        "  satisfaction_label = ?, "
        "  satisfaction_score = ?, "
        "  satisfaction_signals = ?, "
        "  satisfaction_source = ?, "
        "  satisfaction_classified_at = ? "
        "WHERE ticket_id = ?",
        (
            verdict["label"],
            verdict["score"],
            json.dumps(verdict["signals"], ensure_ascii=False),
            source,
            datetime.now(timezone.utc).isoformat(),
            ticket_id,
        ),
    )
    conn.commit()
    conn.close()


async def _fetch_transcripts(client, target_ids: set[int]) -> dict[int, bytes]:
    """Scan #archieved newest→oldest, return {ticket_num: html_bytes} for
    transcripts whose ticket number is in target_ids."""
    ch = client.get_channel(ARCHIEVED_CHANNEL_ID) or \
         await client.fetch_channel(ARCHIEVED_CHANNEL_ID)
    found: dict[int, tuple[datetime, discord.Attachment]] = {}
    async for msg in ch.history(limit=None, oldest_first=False):
        for att in msg.attachments:
            m = TRANSCRIPT_FILE_RE.search(att.filename or "")
            if not m:
                continue
            n = int(m.group(1))
            if n not in target_ids:
                continue
            ts = msg.created_at.replace(tzinfo=timezone.utc)
            cur = found.get(n)
            # Prefer the EARLIEST transcript post (closer to actual close).
            if cur and cur[0] < ts:
                continue
            found[n] = (ts, att)
        if len(found) >= len(target_ids):
            break
    out: dict[int, bytes] = {}
    for n, (_ts, att) in found.items():
        out[n] = await att.read()
    return out


def _post_reply_user_msgs(user_msgs, agent_msgs) -> tuple[str, list[str], str]:
    """Return (initial_complaint, post_reply_user_texts, agent_first_reply)."""
    initial = (user_msgs[0].get("content") or "").strip() if user_msgs else ""
    if not agent_msgs:
        return initial, [], ""
    first_agent_at = agent_msgs[0].get("created", 0)
    first_agent_text = (agent_msgs[0].get("content") or "").strip()
    post = [
        (m.get("content") or "").strip()
        for m in user_msgs
        if m.get("created", 0) > first_agent_at and (m.get("content") or "").strip()
    ]
    return initial, post, first_agent_text


async def main(days: int, only_ticket: str | None):
    if not config.GEMINI_API_KEY:
        print("[SAT] GEMINI_API_KEY not set", file=sys.stderr)
        return 1

    # Pick target tickets.
    import sqlite3
    conn = sqlite3.connect(config.DB_FILE)
    conn.row_factory = sqlite3.Row
    if only_ticket:
        rows = conn.execute(
            "SELECT ticket_id, created_at, agent_name, conversation_excerpt "
            "FROM tickets WHERE ticket_id = ?", (only_ticket,)
        ).fetchall()
    else:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        rows = conn.execute(
            "SELECT ticket_id, created_at, agent_name, conversation_excerpt "
            "FROM tickets WHERE created_at >= ? AND created_at < ? "
            "ORDER BY ticket_id",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    conn.close()

    if not rows:
        print("[SAT] no tickets in target window")
        return 0

    target_nums = {int(r["ticket_id"].rsplit("-", 1)[1]) for r in rows
                   if r["ticket_id"].startswith("ticket-")}
    print(f"[SAT] target: {len(rows)} tickets (nums={sorted(target_nums)})")

    # Pull transcripts in one Discord pass.
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    transcripts: dict[int, bytes] = {}

    @client.event
    async def on_ready():
        nonlocal transcripts
        try:
            transcripts = await _fetch_transcripts(client, target_nums)
        finally:
            await client.close()

    await client.start(config.DISCORD_TOKEN)
    print(f"[SAT] fetched {len(transcripts)} transcripts "
          f"(missing: {sorted(target_nums - set(transcripts))})")

    # Classify each.
    gemini = genai.Client(api_key=config.GEMINI_API_KEY)
    results = []
    for i, r in enumerate(rows):
        tid = r["ticket_id"]
        num = int(tid.rsplit("-", 1)[1]) if tid.startswith("ticket-") else None
        source = "no_text"
        initial, post, agent_first = "", [], ""

        if num is not None and num in transcripts:
            messages = parse_transcript_html(transcripts[num])
            if messages:
                user_msgs, agent_msgs = split_user_vs_agent(messages)
                initial, post, agent_first = _post_reply_user_msgs(user_msgs, agent_msgs)
                source = "transcript"

        if source == "no_text":
            initial = r["conversation_excerpt"] or ""
            if initial:
                source = "excerpt"
            else:
                # Nothing to classify.
                verdict = {"label": "no_signal", "score": 0.0,
                           "signals": ["no transcript and no excerpt"],
                           "confidence": 1.0}
                _save(tid, verdict, source)
                results.append((tid, verdict, source))
                continue

        prompt = _build_prompt(tid, initial, agent_first, post)
        try:
            resp = await _call_gemini(gemini, prompt)
        except _TransientLLMError as e:
            print(f"[SAT] {tid} transient give-up: {e}")
            continue
        except Exception as e:
            print(f"[SAT] {tid} permanent: {e}")
            continue

        try:
            parsed = json.loads((resp.text or "").strip())
        except json.JSONDecodeError:
            print(f"[SAT] {tid} JSON parse failed: {(resp.text or '')[:120]!r}")
            continue

        verdict = _validate(parsed)
        if not verdict:
            print(f"[SAT] {tid} invalid verdict: {parsed!r}")
            continue
        _save(tid, verdict, source)
        results.append((tid, verdict, source))
        # Pace under free-tier RPM.
        if i + 1 < len(rows):
            await asyncio.sleep(5)

    # Summary
    print()
    print(f"{'ticket_id':<14} {'label':<10} {'score':>6}  src        signals")
    print("-" * 80)
    for tid, v, src in results:
        sig = "; ".join(v["signals"])[:50]
        print(f"{tid:<14} {v['label']:<10} {v['score']:>+6.2f}  {src:<10} {sig}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--ticket", default=None,
                   help="Classify just this one ticket_id (skip --days)")
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.days, args.ticket)))
