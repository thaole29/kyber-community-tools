# Project Data Chatbot (Section 3)

Standalone RAG Q&A chatbot over the project's existing data (`tickets.db` +
`community_digests`). **Self-contained** — runs on its own port and does not
touch `dashboard/` or `bot.py`. Not wired into the dashboard UI (by design,
until requested).

## What it does

Hybrid retrieval, then answer with your LLM:

1. **Structured (text-to-SQL)** — numeric/aggregate questions ("which agent
   missed most this week?") become ONE sandboxed read-only `SELECT` over
   `tickets.db` (SELECT-only, table whitelist, single statement, forced LIMIT,
   `mode=ro` connection).
2. **Semantic (RAG)** — content questions ("what do users complain about on
   limit orders?") hit an embedding index built from ticket text and digests.

Both results are fed to the LLM, which answers with citations.

## Architecture

```
chatbot/
  llm_client.py   # provider-agnostic chat + Gemini embeddings
  embeddings.py   # tiny SQLite vector store (cosine in numpy)
  index_build.py  # build/refresh the index from tickets.db (read-only)
  retriever.py    # hybrid: safe text-to-SQL + semantic search
  server.py       # FastAPI: /chat, /health, /stats
  web/index.html  # minimal chat UI
```

The vector index lives in its own file (`chatbot/chatbot_index.db`, gitignored)
so `tickets.db` schema is never modified.

## Setup & run

```bash
source venv/bin/activate
pip install -r requirements.txt          # adds fastapi, uvicorn, numpy

# 1) Build the semantic index (uses GEMINI_API_KEY for embeddings)
python -m chatbot.index_build            # add --limit 50 to test small

# 2) Run the server
uvicorn chatbot.server:app --port 8100
# open http://localhost:8100
```

Refresh the index on a cron (e.g. after the daily snapshot):

```cron
30 0 * * * cd "/path/Project" && venv/bin/python -m chatbot.index_build >> logs/chatbot_index.log 2>&1
```

## Bring your own LLM

The chat model is pluggable via `.env` — no code change. Until set, it falls
back to **Gemini** (reusing `GEMINI_API_KEY`) so the bot works today.

```env
# OpenAI-compatible (vLLM / Ollama / TGI / LM Studio / OpenAI ...)
CHATBOT_LLM_PROVIDER=openai
CHATBOT_LLM_BASE_URL=http://your-host:8000/v1
CHATBOT_LLM_API_KEY=sk-...
CHATBOT_LLM_MODEL=your-model

# or Anthropic-style
# CHATBOT_LLM_PROVIDER=anthropic
# CHATBOT_LLM_MODEL=claude-...
# CHATBOT_LLM_API_KEY=...

# or keep Gemini (default)
# CHATBOT_LLM_PROVIDER=gemini
# CHATBOT_LLM_MODEL=gemini-2.5-flash
```

If your endpoint isn't one of these shapes, add a small branch in
`llm_client.chat()` — that's the only place that knows about providers.

All settings live in the root `config.py` (`CHATBOT_*`), per the project's
single-source-of-truth rule.

## Auth

The server is **HTTP Basic Auth, fail-closed**: every route returns `503`
until both credentials are set in `.env`, and `401` on a wrong login. Always
set these before exposing the server (e.g. via a tunnel):

```env
CHATBOT_AUTH_USER=someuser
CHATBOT_AUTH_PASS=a-strong-password
```

The password is a secret — add it the same hidden way as the API key:

```bash
read -rs -p "auth pass: " P && printf '\nCHATBOT_AUTH_PASS=%s\n' "$P" >> .env && unset P
```

## Public URL

Exposed on its own Cloudflare Quick Tunnel (separate from the dashboard):

```bash
uvicorn chatbot.server:app --host 127.0.0.1 --port 8100   # terminal 1
cloudflared tunnel --url http://127.0.0.1:8100            # terminal 2 -> prints a *.trycloudflare.com URL
```

Quick Tunnel URLs are ephemeral (change on restart). For a stable URL + auto-run
on boot, use a named Cloudflare tunnel + a launchd plist.
