"""
community_digest.py

Daily community discussion digest. Fetches the last 24h of public messages
from configured channels (config.COMMUNITY_CHANNELS), summarizes each channel
via Claude (Opus 4.7), and posts an anonymized digest to the Telegram staff
group. Persists raw digest JSON for the weekly rollup.

Run via cron at 00:05 UTC daily.

Privacy: messages are pre-processed to scrub @mentions; authors are mapped
to opaque aliases (user_1, user_2…) before being sent to the LLM. The
prompt forbids returning usernames; output uses counts only.
"""

import asyncio
import json
import re
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from google import genai
from google.genai import types as genai_types

import config
import database


# LLM provider: Google Gemini (free tier). Model configurable via env.
# gemini-2.5-flash budgets thinking tokens against max_output_tokens, so we
# disable thinking for this structured-output task and raise the cap to give
# JSON room on busy channels (≥30 messages → output can hit ~1500 tokens).
GEMINI_MODEL = config.GEMINI_MODEL
LLM_MAX_TOKENS = 4000
LLM_THINKING_BUDGET = 0

DISCORD_USER_MENTION_RE = re.compile(r"<@!?\d+>")
DISCORD_ROLE_MENTION_RE = re.compile(r"<@&\d+>")
DISCORD_CHANNEL_REF_RE  = re.compile(r"<#\d+>")
DISCORD_CUSTOM_EMOJI_RE = re.compile(r"<a?:(\w+):\d+>")


def clean_message_text(content):
    """Strip Discord-specific markup that's not useful for summarization."""
    if not content:
        return ""
    text = DISCORD_USER_MENTION_RE.sub("[user]", content)
    text = DISCORD_ROLE_MENTION_RE.sub("[role]", text)
    text = DISCORD_CHANNEL_REF_RE.sub("#channel", text)
    text = DISCORD_CUSTOM_EMOJI_RE.sub(lambda m: f":{m.group(1)}:", text)
    return text.strip()


def is_meaningful(text, is_reply):
    if not text:
        return False
    if is_reply:
        return True
    return len(text.split()) >= config.COMMUNITY_MIN_WORDS


def collapse_bursts(messages):
    """
    Group consecutive messages from the same author within
    COMMUNITY_BURST_WINDOW_SECS into a single block.
    `messages` must be sorted oldest-first.
    """
    if not messages:
        return []
    out = []
    current = None
    window = config.COMMUNITY_BURST_WINDOW_SECS
    for m in messages:
        if (current is not None
                and m['author_id'] == current['author_id']
                and (m['created_at'] - current['created_at']).total_seconds() <= window):
            current['content'] += "\n" + m['content']
        else:
            if current is not None:
                out.append(current)
            current = dict(m)
    if current is not None:
        out.append(current)
    return out


def sample_evenly(messages, cap):
    """Return at most `cap` items, sampled evenly across the input."""
    if len(messages) <= cap:
        return messages
    step = len(messages) / cap
    return [messages[int(i * step)] for i in range(cap)]


def anonymize_authors(messages):
    """Assign opaque user_N aliases per channel batch (in-place)."""
    mapping = OrderedDict()
    for m in messages:
        if m['author_id'] not in mapping:
            mapping[m['author_id']] = f"user_{len(mapping) + 1}"
        m['author_alias'] = mapping[m['author_id']]
    return messages


def format_messages_block(messages):
    lines = []
    for m in messages:
        ts = m['created_at'].strftime('%H:%M')
        lines.append(f"[{ts}] {m['author_alias']}: {m['content']}")
    return "\n".join(lines)


PROMPT_TEMPLATE = """You are analyzing community discussion from a crypto/DeFi Discord server (Kyber Network). Below are anonymized messages from #{channel_name} in the last 24 hours.

CRITICAL PRIVACY RULES — DO NOT include any of the following anywhere in your output:
- Discord usernames or display names
- The user_N aliases used in the input
- @mentions of any kind
Aggregate by count only (e.g. "3 users asked X").

Respond with ONE JSON object only — no prose, no code fences. Schema:
{{
  "channel": "{channel_name}",
  "message_count": {message_count},
  "themes": [
    {{
      "title": "<short theme title>",
      "summary": "<2-3 sentence summary>",
      "sentiment": "positive|neutral|negative|mixed",
      "message_count": <int>
    }}
  ],
  "feature_requests": ["<request>"],
  "complaints": ["<complaint>"],
  "common_questions": ["<question>"],
  "active_contributor_count": <int — distinct active aliases observed>,
  "action_items": ["<thing staff should follow up on>"],
  "overall_sentiment": "positive|neutral|negative|mixed"
}}

If nothing meaningful was discussed, respond with:
{{"channel": "{channel_name}", "message_count": 0, "themes": [], "skip": true}}

Messages:
---
{messages_block}
---"""


def _strip_code_fence(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


async def fetch_channel_messages(channel, since_utc):
    """Pull human messages from a Discord channel since `since_utc`, oldest first."""
    msgs = []
    async for msg in channel.history(limit=None, after=since_utc, oldest_first=True):
        if msg.author.bot:
            continue
        cleaned = clean_message_text(msg.content)
        is_reply = msg.reference is not None
        if not is_meaningful(cleaned, is_reply):
            continue
        created = msg.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        msgs.append({
            'author_id': str(msg.author.id),
            'created_at': created,
            'content': cleaned,
            'is_reply': is_reply,
        })
    return msgs


async def summarize_channel(gemini, channel_name, raw_messages):
    """Run the full pipeline for a single channel and return parsed JSON."""
    if not raw_messages:
        return {
            "channel": channel_name,
            "message_count": 0,
            "themes": [],
            "skip": True,
        }

    msgs = collapse_bursts(raw_messages)
    msgs = sample_evenly(msgs, config.COMMUNITY_MAX_MESSAGES_PER_CHANNEL)
    msgs = anonymize_authors(msgs)
    block = format_messages_block(msgs)

    prompt = PROMPT_TEMPLATE.format(
        channel_name=channel_name,
        message_count=len(msgs),
        messages_block=block,
    )

    try:
        resp = await gemini.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                max_output_tokens=LLM_MAX_TOKENS,
                response_mime_type="application/json",
                thinking_config=genai_types.ThinkingConfig(
                    thinking_budget=LLM_THINKING_BUDGET,
                ),
            ),
        )
        text = _strip_code_fence(resp.text or "")
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"[ERROR] JSON parse failed for #{channel_name}: {e}")
        return {
            "channel": channel_name,
            "message_count": len(msgs),
            "themes": [],
            "skip": True,
            "error": "json_parse_failed",
        }
    except Exception as e:
        print(f"[ERROR] LLM call failed for #{channel_name}: {e}")
        return {
            "channel": channel_name,
            "message_count": len(msgs),
            "themes": [],
            "skip": True,
            "error": str(e),
        }


def format_telegram_digest(date_label, results):
    """Format a list of per-channel JSON results into a single Telegram message."""
    esc = config.html_escape
    lines = [
        f"📣 <b>Community Digest — {esc(date_label)}</b>",
        "",
    ]
    all_action_items = []
    rendered_any = False

    for r in results:
        if r.get("skip") or (r.get("message_count") or 0) == 0:
            continue
        rendered_any = True
        ch = r.get("channel", "?")
        cnt = r.get("message_count") or 0
        active = r.get("active_contributor_count")

        header = f"📌 <b>#{esc(ch)}</b> ({cnt} messages"
        if isinstance(active, int) and active > 0:
            header += f", {active} active users"
        header += ")"
        lines.append(header)

        for t in (r.get("themes") or [])[:3]:
            title = esc(t.get("title", ""))
            summary = esc(t.get("summary", ""))
            sentiment = esc(t.get("sentiment", "neutral"))
            msg_cnt = t.get("message_count")
            cnt_str = f" [{msg_cnt} msgs]" if isinstance(msg_cnt, int) else ""
            lines.append(f"  • <b>{title}</b>{cnt_str} — {summary} <i>({sentiment})</i>")

        complaints = r.get("complaints") or []
        if complaints:
            lines.append("  🔴 Complaints: " + esc("; ".join(complaints[:3])))
        feats = r.get("feature_requests") or []
        if feats:
            lines.append("  💡 Feature requests: " + esc("; ".join(feats[:3])))
        questions = r.get("common_questions") or []
        if questions:
            lines.append("  ❓ Common questions: " + esc("; ".join(questions[:3])))

        sent = r.get("overall_sentiment")
        if sent:
            lines.append(f"  <i>Overall sentiment: {esc(sent)}</i>")
        lines.append("")

        for ai in r.get("action_items") or []:
            all_action_items.append(ai)

    if all_action_items:
        lines.append("🔧 <b>Action Items</b>")
        seen = set()
        idx = 1
        for ai in all_action_items:
            key = ai.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"  {idx}. {esc(ai)}")
            idx += 1

    if not rendered_any:
        lines.append("<i>No significant community discussion in the last 24 hours.</i>")

    return "\n".join(lines)


async def send_telegram(text):
    token = config.TELEGRAM_TOKEN
    chat_ids = config.TELEGRAM_CHAT_IDS
    if not token or not chat_ids:
        print("⚠️  Telegram not configured — skipping send.")
        return

    chunks = []
    if len(text) <= 4096:
        chunks = [text]
    else:
        cur = ""
        for line in text.split("\n"):
            if len(cur) + len(line) + 1 > 4000:
                chunks.append(cur)
                cur = line + "\n"
            else:
                cur += line + "\n"
        if cur:
            chunks.append(cur)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with aiohttp.ClientSession() as session:
        for raw_chat_id in chat_ids:
            chat_id = raw_chat_id
            thread_id = None
            if '/' in chat_id:
                chat_id, thread_id = chat_id.split('/', 1)
            if not chat_id.startswith('-'):
                chat_id = f"-100{chat_id}"

            for chunk in chunks:
                payload = {
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }
                if thread_id:
                    payload["message_thread_id"] = thread_id
                async with session.post(url, json=payload) as r:
                    if r.status >= 400:
                        body = await r.text()
                        print(f"❌ Telegram error {r.status} for {chat_id}: {body[:300]}")


async def run_digest():
    """Main entry — connect to Discord, run pipeline, send to Telegram, exit."""
    if not config.GEMINI_API_KEY:
        print("❌ GEMINI_API_KEY not set.")
        return
    if not config.DISCORD_TOKEN:
        print("❌ DISCORD_TOKEN not set.")
        return

    database.init_db()

    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = False  # not needed for community fetch

    client = discord.Client(intents=intents)
    gemini = genai.Client(api_key=config.GEMINI_API_KEY)

    now_utc = datetime.now(tz=timezone.utc)
    since = now_utc - timedelta(hours=24)
    digest_date = now_utc.strftime('%Y-%m-%d')

    @client.event
    async def on_ready():
        print(f"[READY] Logged in as {client.user}")
        try:
            guild = client.get_guild(config.GUILD_ID)
            if guild is None:
                print(f"❌ Guild {config.GUILD_ID} not found.")
                return

            channels_by_name = {c.name: c for c in guild.text_channels}
            results = []
            for ch_name in config.COMMUNITY_CHANNELS:
                channel = channels_by_name.get(ch_name)
                if channel is None:
                    print(f"⚠️  Channel #{ch_name} not found in guild.")
                    results.append({
                        "channel": ch_name, "message_count": 0,
                        "themes": [], "skip": True, "error": "channel_not_found",
                    })
                    continue

                print(f"[FETCH] #{ch_name}…")
                try:
                    raw = await fetch_channel_messages(channel, since)
                except discord.Forbidden:
                    print(f"⚠️  Missing permission to read #{ch_name}.")
                    results.append({
                        "channel": ch_name, "message_count": 0,
                        "themes": [], "skip": True, "error": "forbidden",
                    })
                    continue
                print(f"[FETCH] #{ch_name}: {len(raw)} meaningful messages.")
                result = await summarize_channel(gemini, ch_name, raw)
                results.append(result)
                database.save_community_digest(digest_date, ch_name, result)

            local_label = now_utc.astimezone(config.LOCAL_TZ).strftime('%b %d, %Y')
            digest_text = format_telegram_digest(local_label, results)
            await send_telegram(digest_text)
            print("✅ Community digest delivered.")
        finally:
            await client.close()

    await client.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(run_digest())
