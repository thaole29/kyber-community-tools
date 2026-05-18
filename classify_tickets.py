"""
classify_tickets.py

LLM-based product categorization for support tickets. Reads tickets that
have user text but no `product_group` yet, sends them in batches to
Gemini 2.5 Flash with a CLOSED set of categories, and caches the result
back to the DB (`product_group`, `product_subcategory`, `category_source`,
`category_confidence`, `classified_at`).

Designed to be called from `bot.py:classify_loop` every 15 minutes. Also
runnable directly as a one-shot CLI.

Privacy: wallet addresses are stripped before sending to the LLM. Author
identities are not included in the input.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from google import genai
from google.genai import types as genai_types

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import config  # noqa: E402
import database  # noqa: E402


# ---------------------------------------------------------------------------
# Closed-set category map. The LLM is forced to return EXACTLY one of these
# (group, subcategory) pairs. Frontend renders the same labels.
# ---------------------------------------------------------------------------
CATEGORY_MAP = {
    "Trade": [
        "Aggregator Swap",
        "Cross-chain Swap",
        "Limit Order",
    ],
    "Earn": [
        "Kyber Earn / LP",
        "ZAP",
        "Smart Exit",
    ],
    "Infrastructure": [
        "Wallet / Connect",
        "Transaction",
        "Token Approval",
        "Gas & MEV",
        "Bridge",
    ],
    "Business": [
        "Integration (B2B)",
        "Community & Rewards",
    ],
    "Other": [
        "Uncategorized",
    ],
}

# Lookup: subcategory string → its group (used to validate model output).
SUBCATEGORY_TO_GROUP = {
    sub: group for group, subs in CATEGORY_MAP.items() for sub in subs
}


SYSTEM_PROMPT = """You classify KyberSwap support tickets.

KyberSwap is a decentralized exchange aggregator. Users open tickets about \
trading, earning yield, wallet/transaction issues, B2B integrations, or \
community/rewards programs.

For EACH ticket below, return a JSON object with these fields:
  - ticket_id        : the exact ticket_id string given
  - group            : ONE of {groups}
  - subcategory      : ONE of {subs}
  - confidence       : 0.0 to 1.0 — how clearly the text signals this category

RULES:
  1. The subcategory MUST belong to the group (see mapping below).
  2. If the ticket text is too vague, empty, or contains only wallet addresses \
or one-word replies, set group="Other" subcategory="Uncategorized" \
confidence<=0.2 — do NOT guess.
  3. If the user describes a multi-hop trade between different chains or \
networks, use "Cross-chain Swap" (NOT "Bridge"). "Bridge" is for raw asset \
bridging without swap.
  4. A complaint about transaction stuck / failed / pending → "Transaction" \
(unless the cause is clearly slippage on a swap → "Aggregator Swap").
  5. A B2B inquiry from a company / API / RPC / partner → "Integration (B2B)".

Subcategory → group mapping (must match):
{mapping}

Respond with a JSON ARRAY (no markdown, no code fence). Example:
[
  {{"ticket_id":"ticket-2049","group":"Trade","subcategory":"Limit Order","confidence":0.92}},
  {{"ticket_id":"ticket-2042","group":"Other","subcategory":"Uncategorized","confidence":0.05}}
]
"""

# Strip 0x... wallet addresses & long hex blobs before sending to LLM.
_WALLET_RE = re.compile(r"\b0x[a-fA-F0-9]{20,}\b")
_LONG_HEX_RE = re.compile(r"\b[a-fA-F0-9]{40,}\b")


def _clean_text(text: str) -> str:
    if not text:
        return ""
    text = _WALLET_RE.sub("[addr]", text)
    text = _LONG_HEX_RE.sub("[hex]", text)
    return text.strip()[:600]  # cap each ticket's input at 600 chars


def _build_prompt(rows: list[dict]) -> str:
    groups = ", ".join(CATEGORY_MAP.keys())
    subs = ", ".join(SUBCATEGORY_TO_GROUP.keys())
    mapping_lines = []
    for grp, subs_list in CATEGORY_MAP.items():
        for s in subs_list:
            mapping_lines.append(f"  {s}  →  {grp}")
    header = SYSTEM_PROMPT.format(
        groups=groups,
        subs=subs,
        mapping=chr(10).join(mapping_lines),
    )

    lines = [header, "", "TICKETS TO CLASSIFY:"]
    for r in rows:
        text = _clean_text(
            r.get("first_user_message") or r.get("conversation_excerpt") or ""
        )
        lines.append(f"---- {r['ticket_id']} ----")
        lines.append(text)
    return "\n".join(lines)


def _validate_item(item: dict) -> tuple[str, str, str, float] | None:
    """Return (ticket_id, group, subcategory, confidence) if the item is well-formed
    and lands in the closed set. Otherwise None (will fall through to default)."""
    tid = item.get("ticket_id")
    sub = item.get("subcategory")
    grp = item.get("group")
    conf = item.get("confidence", 0.0)
    if not isinstance(tid, str) or not sub or not grp:
        return None
    # Trust the mapping table over what the model said for 'group' — pick
    # the canonical group for the subcategory the model chose.
    canonical_group = SUBCATEGORY_TO_GROUP.get(sub)
    if not canonical_group:
        return None
    try:
        conf = max(0.0, min(1.0, float(conf)))
    except (TypeError, ValueError):
        conf = 0.0
    return tid, canonical_group, sub, conf


class _TransientLLMError(Exception):
    """Rate-limit or temporary backend failure. Caller should NOT mark
    tickets and should retry later."""


async def _call_gemini(gemini, model: str, prompt: str, max_retries: int = 2):
    """Make a single Gemini call with one retry on 429/5xx. Raises
    _TransientLLMError if the call cannot recover; raises other exceptions
    for permanent failures (auth, etc.) so they bubble up."""
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await gemini.aio.models.generate_content(
                model=model,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    max_output_tokens=4000,
                    response_mime_type="application/json",
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                ),
            )
        except Exception as e:  # genai SDK raises plain Exception for HTTP errors
            msg = str(e)
            is_transient = (
                "429" in msg or "RESOURCE_EXHAUSTED" in msg or "503" in msg
                or "500" in msg or "UNAVAILABLE" in msg
            )
            if not is_transient or attempt >= max_retries:
                last_err = e
                break
            # Try to honor the retry-after hint if present in the error text.
            delay = 16
            m = re.search(r"retry in (\d+(?:\.\d+)?)s", msg)
            if m:
                delay = min(30, float(m.group(1)) + 1)
            print(f"[CLASSIFY] transient {msg[:80]}; retry in {delay:.0f}s", flush=True)
            await asyncio.sleep(delay)
    raise _TransientLLMError(str(last_err) if last_err else "exhausted retries")


async def classify_batch(rows: list[dict], gemini: genai.Client | None = None,
                         model: str | None = None) -> dict[str, dict]:
    """Send `rows` (list of dicts with ticket_id + text fields) to Gemini and
    return a mapping ticket_id → {group, subcategory, source, confidence}.

    On transient errors (429 / 5xx) → returns empty dict (caller should NOT
    persist anything; next loop tick will retry).
    Tickets the model omits from a SUCCESSFUL response are mapped to
    Other/Uncategorized so we don't keep retrying them forever.
    """
    if not rows:
        return {}
    if gemini is None:
        gemini = genai.Client(api_key=config.GEMINI_API_KEY)
    model = model or config.GEMINI_MODEL

    prompt = _build_prompt(rows)
    try:
        resp = await _call_gemini(gemini, model, prompt)
    except _TransientLLMError as e:
        print(f"[CLASSIFY] giving up batch ({len(rows)} tickets) — transient: {e}",
              flush=True)
        return {}
    except Exception as e:
        print(f"[CLASSIFY] permanent Gemini failure: {e}", flush=True)
        return {}

    try:
        raw = (resp.text or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
            raw = re.sub(r"```$", "", raw).strip()
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            print(f"[CLASSIFY] expected list, got {type(parsed).__name__}; "
                  f"raw head: {raw[:200]!r}", flush=True)
            parsed = []
    except json.JSONDecodeError as e:
        print(f"[CLASSIFY] JSON parse failed: {e}; raw head: {(resp.text or '')[:200]!r}",
              flush=True)
        parsed = []

    out: dict[str, dict] = {}
    for item in parsed:
        v = _validate_item(item)
        if v is None:
            continue
        tid, grp, sub, conf = v
        out[tid] = {
            "group": grp,
            "subcategory": sub,
            "source": "llm-gemini",
            "confidence": conf,
        }

    # Anyone the model didn't return in a SUCCESSFUL response → Uncategorized,
    # so subsequent loop ticks don't waste calls on the same tickets.
    for r in rows:
        if r["ticket_id"] not in out:
            out[r["ticket_id"]] = {
                "group": "Other",
                "subcategory": "Uncategorized",
                "source": "llm-gemini-missing",
                "confidence": 0.0,
            }
    return out


async def classify_unclassified(batch_size: int = 15, max_batches: int = 5) -> int:
    """Pull unclassified tickets with text and run them through the LLM.
    Writes each result back to DB. Returns total ticket count classified.

    `max_batches` caps the work per invocation to keep loop runs bounded.
    """
    if not config.GEMINI_API_KEY:
        print("[CLASSIFY] GEMINI_API_KEY not set — skipping", flush=True)
        return 0

    gemini = genai.Client(api_key=config.GEMINI_API_KEY)
    total = 0
    for batch_idx in range(max_batches):
        rows = database.get_tickets_needing_classification(limit=batch_size)
        if not rows:
            break
        result = await classify_batch(rows, gemini=gemini)
        if not result:
            # Transient failure; stop this run and let the next loop tick retry.
            print(f"[CLASSIFY] batch returned empty (rate-limited?); aborting run",
                  flush=True)
            break
        for tid, info in result.items():
            database.save_ticket_classification(
                tid,
                info["group"],
                info["subcategory"],
                info["source"],
                info["confidence"],
            )
        total += len(result)
        print(f"[CLASSIFY] processed batch of {len(result)} (running total: {total})",
              flush=True)
        # Pace ourselves under the free-tier 5 RPM limit when chaining batches.
        if batch_idx + 1 < max_batches:
            await asyncio.sleep(13)
    return total


if __name__ == "__main__":
    n = asyncio.run(classify_unclassified())
    print(f"Classified {n} ticket(s).")
