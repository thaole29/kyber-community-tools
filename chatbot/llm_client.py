"""Provider-agnostic chat + embedding client for the chatbot.

The chat LLM is intentionally pluggable so a bring-your-own endpoint can be
wired in later by setting CHATBOT_LLM_* in .env — no code change needed:

    CHATBOT_LLM_PROVIDER=openai
    CHATBOT_LLM_BASE_URL=http://your-host:8000/v1
    CHATBOT_LLM_API_KEY=sk-...
    CHATBOT_LLM_MODEL=your-model

Until then it defaults to Gemini (reusing GEMINI_API_KEY) so the whole bot is
testable today. Embeddings are a separate provider (Gemini free tier default),
independent of the chat model.

Supported chat providers:
  - gemini    : google-genai SDK
  - openai    : any OpenAI-compatible /v1/chat/completions (vLLM, Ollama, TGI,
                LM Studio, OpenAI, ...). This is the usual shape for a
                self-hosted model — point CHATBOT_LLM_BASE_URL at it.
  - anthropic : Anthropic Messages API
"""

from __future__ import annotations

import sys
from pathlib import Path

import requests

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import config  # noqa: E402


class LLMError(RuntimeError):
    pass


# ----------------------------------------------------------------------------
# Chat completion
# ----------------------------------------------------------------------------

def chat(messages, system=None, max_tokens=None, temperature=None):
    """Run a chat completion. `messages` is a list of {role, content} with
    role in {'user','assistant'}. `system` is an optional system prompt.
    Returns the assistant's text. Raises LLMError on failure.
    """
    provider = (config.CHATBOT_LLM_PROVIDER or 'gemini').lower()
    max_tokens = max_tokens or config.CHATBOT_LLM_MAX_TOKENS
    temperature = config.CHATBOT_LLM_TEMPERATURE if temperature is None else temperature

    if provider == 'gemini':
        return _chat_gemini(messages, system, max_tokens, temperature)
    if provider in ('openai', 'custom'):
        return _chat_openai(messages, system, max_tokens, temperature)
    if provider == 'anthropic':
        return _chat_anthropic(messages, system, max_tokens, temperature)
    raise LLMError(f"Unknown CHATBOT_LLM_PROVIDER: {provider!r}")


def _chat_gemini(messages, system, max_tokens, temperature):
    from google import genai
    from google.genai import types as genai_types

    api_key = config.CHATBOT_LLM_API_KEY or config.GEMINI_API_KEY
    if not api_key:
        raise LLMError("No Gemini API key (set GEMINI_API_KEY or CHATBOT_LLM_API_KEY)")
    client = genai.Client(api_key=api_key)
    # Gemini takes a single contents string; fold the turns into one transcript.
    contents = _flatten_messages(messages)
    cfg = genai_types.GenerateContentConfig(
        max_output_tokens=max_tokens,
        temperature=temperature,
        system_instruction=system or None,
        # 2.5 models default to a thinking budget that eats max_output_tokens;
        # pin it to 0 for snappy, fully-emitted answers (same as community_digest).
        thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
    )
    try:
        resp = client.models.generate_content(
            model=config.CHATBOT_LLM_MODEL, contents=contents, config=cfg
        )
        return (resp.text or "").strip()
    except Exception as e:  # noqa: BLE001
        raise LLMError(f"Gemini chat failed: {e}") from e


def _chat_openai(messages, system, max_tokens, temperature):
    base = (config.CHATBOT_LLM_BASE_URL or "https://api.openai.com/v1").rstrip('/')
    api_key = config.CHATBOT_LLM_API_KEY or ""
    payload_msgs = ([{"role": "system", "content": system}] if system else []) + [
        {"role": m["role"], "content": m["content"]} for m in messages
    ]
    body = {
        "model": config.CHATBOT_LLM_MODEL,
        "messages": payload_msgs,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        r = requests.post(f"{base}/chat/completions", json=body, headers=headers, timeout=120)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:  # noqa: BLE001
        raise LLMError(f"OpenAI-compatible chat failed: {e}") from e


def _chat_anthropic(messages, system, max_tokens, temperature):
    base = (config.CHATBOT_LLM_BASE_URL or "https://api.anthropic.com").rstrip('/')
    api_key = config.CHATBOT_LLM_API_KEY or config.ANTHROPIC_API_KEY or ""
    body = {
        "model": config.CHATBOT_LLM_MODEL,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
    }
    if system:
        body["system"] = system
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    try:
        r = requests.post(f"{base}/v1/messages", json=body, headers=headers, timeout=120)
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", [])).strip()
    except Exception as e:  # noqa: BLE001
        raise LLMError(f"Anthropic chat failed: {e}") from e


def _flatten_messages(messages):
    """Render a messages list into a single transcript string (for Gemini)."""
    lines = []
    for m in messages:
        role = m.get("role", "user")
        prefix = "User" if role == "user" else "Assistant"
        lines.append(f"{prefix}: {m['content']}")
    return "\n\n".join(lines)


# ----------------------------------------------------------------------------
# Embeddings (separate provider; Gemini by default)
# ----------------------------------------------------------------------------

def embed(texts):
    """Embed a list of strings → list of float vectors. Gemini only for now."""
    provider = (config.CHATBOT_EMBED_PROVIDER or 'gemini').lower()
    if provider != 'gemini':
        raise LLMError(f"Unsupported embed provider: {provider!r}")
    from google import genai

    api_key = config.GEMINI_API_KEY or config.CHATBOT_LLM_API_KEY
    if not api_key:
        raise LLMError("No Gemini API key for embeddings")
    client = genai.Client(api_key=api_key)
    out = []
    # Small batches + backoff to stay under the free-tier per-minute quota.
    for i in range(0, len(texts), 20):
        batch = texts[i:i + 20]
        out.extend(_embed_batch_with_retry(client, batch))
    return out


def _embed_batch_with_retry(client, batch, max_retries=5):
    import time

    delay = 2.0
    for attempt in range(max_retries):
        try:
            resp = client.models.embed_content(
                model=config.CHATBOT_EMBED_MODEL, contents=batch
            )
            return [list(e.values) for e in resp.embeddings]
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            is_rate = "429" in msg or "RESOURCE_EXHAUSTED" in msg
            if is_rate and attempt < max_retries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise LLMError(f"Embedding failed: {e}") from e
    raise LLMError("Embedding failed after retries")
