"""Standalone FastAPI chatbot server.

    venv/bin/uvicorn chatbot.server:app --port 8100
    # or: venv/bin/python -m chatbot.server

Endpoints:
  GET  /          -> minimal chat UI
  GET  /health    -> liveness + LLM/index status
  GET  /stats     -> index stats
  POST /chat      -> {question, history?} -> {answer, sources, sql}

Runs on its own port; touches nothing in dashboard/ or bot.py.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import config  # noqa: E402
from chatbot import embeddings, llm_client, retriever  # noqa: E402

WEB_DIR = Path(__file__).resolve().parent / "web"

_basic = HTTPBasic()


def require_auth(credentials: HTTPBasicCredentials = Depends(_basic)):
    """HTTP Basic Auth gate. Fail-closed: 503 if no credentials are configured
    so the public tunnel never serves an unprotected endpoint by accident."""
    user, pw = config.CHATBOT_AUTH_USER, config.CHATBOT_AUTH_PASS
    if not user or not pw:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth not configured — set CHATBOT_AUTH_USER and CHATBOT_AUTH_PASS in .env",
        )
    ok = secrets.compare_digest(credentials.username, user) & secrets.compare_digest(
        credentials.password, pw
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True


# Auth applied to every route in the app.
app = FastAPI(title="Project Data Chatbot", dependencies=[Depends(require_auth)])

ANSWER_SYSTEM = (
    "You are a support-analytics assistant for the Kyber community support team. "
    "Answer the user's question using ONLY the context provided (database query "
    "results and relevant records). If the context is insufficient, say so plainly "
    "rather than guessing. Be concise. Cite ticket ids / channels you relied on. "
    "Numbers come from the database query result; treat it as authoritative. "
    "Answer in the same language as the question."
)


class ChatRequest(BaseModel):
    question: str
    history: list[dict] | None = None  # [{role, content}, ...]


@app.get("/")
def index():
    return FileResponse(str(WEB_DIR / "index.html"))


@app.get("/health")
def health():
    return {
        "ok": True,
        "llm_provider": config.CHATBOT_LLM_PROVIDER,
        "llm_model": config.CHATBOT_LLM_MODEL,
        "embed_model": config.CHATBOT_EMBED_MODEL,
        "index": embeddings.stats(),
    }


@app.get("/stats")
def stats():
    return embeddings.stats()


@app.post("/chat")
def chat(req: ChatRequest):
    question = (req.question or "").strip()
    if not question:
        return JSONResponse({"error": "empty question"}, status_code=400)

    retrieved = retriever.retrieve(question)
    context, sources = retriever.build_context(retrieved)

    user_block = (
        f"Context:\n{context if context else '(no context found)'}\n\n"
        f"Question: {question}"
    )
    messages = list(req.history or [])
    messages.append({"role": "user", "content": user_block})

    try:
        answer = llm_client.chat(messages, system=ANSWER_SYSTEM)
    except llm_client.LLMError as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    return {
        "answer": answer,
        "sources": sources,
        "sql": retrieved.get("sql"),
        "sql_error": retrieved.get("sql_error"),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=config.CHATBOT_PORT)
