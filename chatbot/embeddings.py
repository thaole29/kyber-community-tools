"""Tiny vector store backed by its own SQLite file.

Data volume is small (a few thousand tickets + digests), so embeddings are kept
in one table and similarity is a brute-force cosine in numpy — zero extra infra.
Swap in sqlite-vec / FAISS later if the corpus grows.

The index DB (config.CHATBOT_INDEX_DB) is SEPARATE from tickets.db so the
project's schema is never touched.
"""

from __future__ import annotations

import sqlite3
import struct
import sys
from pathlib import Path

import numpy as np

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import config  # noqa: E402
from chatbot import llm_client  # noqa: E402


def _connect():
    path = Path(config.CHATBOT_INDEX_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def init_index():
    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL,   -- 'ticket' | 'digest'
                source_id   TEXT NOT NULL,   -- ticket_id or digest_date/channel
                title       TEXT,            -- human-friendly citation label
                text        TEXT NOT NULL,
                embedding   BLOB NOT NULL,
                dim         INTEGER NOT NULL,
                UNIQUE (source_type, source_id)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _pack(vec):
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob, dim):
    return np.array(struct.unpack(f"{dim}f", blob), dtype=np.float32)


def upsert_chunks(rows):
    """rows: list of dicts {source_type, source_id, title, text}. Embeds and
    stores them, replacing any existing chunk with the same (source_type,
    source_id). Returns the number of chunks written."""
    if not rows:
        return 0
    init_index()
    vectors = llm_client.embed([r["text"] for r in rows])
    conn = _connect()
    try:
        for r, vec in zip(rows, vectors):
            conn.execute(
                """
                INSERT INTO chunks (source_type, source_id, title, text, embedding, dim)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_type, source_id) DO UPDATE SET
                    title=excluded.title, text=excluded.text,
                    embedding=excluded.embedding, dim=excluded.dim
                """,
                (r["source_type"], r["source_id"], r.get("title"),
                 r["text"], _pack(vec), len(vec)),
            )
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def search(query, top_k=None):
    """Return top-k chunks most similar to `query` as list of dicts with
    {source_type, source_id, title, text, score}. Empty if index is missing."""
    top_k = top_k or config.CHATBOT_TOP_K
    path = Path(config.CHATBOT_INDEX_DB)
    if not path.exists():
        return []
    qvec = np.array(llm_client.embed([query])[0], dtype=np.float32)
    qn = np.linalg.norm(qvec) or 1.0
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT source_type, source_id, title, text, embedding, dim FROM chunks"
        ).fetchall()
    finally:
        conn.close()
    scored = []
    for row in rows:
        vec = _unpack(row["embedding"], row["dim"])
        denom = (np.linalg.norm(vec) or 1.0) * qn
        score = float(np.dot(vec, qvec) / denom)
        scored.append((score, row))
    scored.sort(key=lambda x: -x[0])
    return [
        {
            "source_type": r["source_type"],
            "source_id": r["source_id"],
            "title": r["title"],
            "text": r["text"],
            "score": round(s, 4),
        }
        for s, r in scored[:top_k]
    ]


def existing_keys():
    """Return the set of (source_type, source_id) already indexed."""
    path = Path(config.CHATBOT_INDEX_DB)
    if not path.exists():
        return set()
    conn = _connect()
    try:
        rows = conn.execute("SELECT source_type, source_id FROM chunks").fetchall()
    finally:
        conn.close()
    return {(r["source_type"], r["source_id"]) for r in rows}


def stats():
    path = Path(config.CHATBOT_INDEX_DB)
    if not path.exists():
        return {"chunks": 0}
    conn = _connect()
    try:
        n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        by_type = dict(
            conn.execute("SELECT source_type, COUNT(*) FROM chunks GROUP BY source_type").fetchall()
        )
    finally:
        conn.close()
    return {"chunks": n, "by_type": by_type}
