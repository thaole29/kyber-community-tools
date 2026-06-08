"""Hybrid retrieval: safe text-to-SQL over tickets.db + semantic search.

- Structured path: the LLM turns a numeric/aggregate question into ONE SQLite
  SELECT, which is executed through a hardened read-only sandbox (SELECT-only,
  table whitelist, single statement, forced LIMIT, mode=ro connection).
- Semantic path: embedding search over indexed ticket/digest text.

Both are returned so the answer step can combine them with citations.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import config  # noqa: E402
from chatbot import embeddings, llm_client  # noqa: E402

# --- SQL sandbox ---------------------------------------------------------------
ALLOWED_TABLES = {
    "tickets", "ticket_response_events", "community_digests", "daily_reports",
}
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|attach|detach|pragma|create|replace|vacuum|reindex|grant)\b",
    re.IGNORECASE,
)
_TABLE_REF = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.IGNORECASE)
_HAS_LIMIT = re.compile(r"\blimit\b", re.IGNORECASE)

SCHEMA_DOC = """\
Tables (SQLite, all datetimes are ISO-8601 UTC strings):

tickets(
  ticket_id TEXT, created_at, first_responded_at, response_time_mins REAL,
  agent_name, ticket_owner, closed_by, closed_at, deleted_at,
  on_duty_agent_name, on_duty_responded, sla_breached, cross_shift_help,
  shift_label,                         -- 'A'|'B'|'C'
  first_user_message, conversation_excerpt, last_user_msg_at, last_agent_msg_at,
  product_group, product_subcategory,  -- LLM ticket category
  satisfaction_label, satisfaction_score, manual_buddy_covered_by
)
ticket_response_events(ticket_id, user_msg_at, agent_msg_at, agent_name, event_type, on_duty_agent, sla_breached)
community_digests(digest_date TEXT 'YYYY-MM-DD', channel, digest_json, message_count)
daily_reports(report_date, payload)

Notes:
- Agents: Dablendo (shift A 02-09 UTC), Mikaelson (B 09-17), TerrorMichael (C 17-02), Reus (floating).
- sla_breached=1 means first-response time > 15 min.
- A 'miss' = on_duty_agent_name set but they didn't first-respond.
"""


def _run_sql(sql):
    """Execute a single read-only SELECT through the sandbox. Returns
    (columns, rows) or raises ValueError on anything unsafe."""
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise ValueError("empty SQL")
    if ";" in sql:
        raise ValueError("multiple statements not allowed")
    if not re.match(r"(?is)^\s*select\b", sql) and not re.match(r"(?is)^\s*with\b", sql):
        raise ValueError("only SELECT/WITH queries are allowed")
    if _FORBIDDEN.search(sql):
        raise ValueError("forbidden keyword in SQL")
    tables = {t.lower() for t in _TABLE_REF.findall(sql)}
    bad = tables - ALLOWED_TABLES
    if bad:
        raise ValueError(f"table(s) not allowed: {', '.join(sorted(bad))}")
    if not _HAS_LIMIT.search(sql):
        sql += " LIMIT 100"
    conn = sqlite3.connect(f"file:{config.DB_FILE}?mode=ro", uri=True)
    try:
        cur = conn.execute(sql)
        cols = [c[0] for c in cur.description] if cur.description else []
        rows = cur.fetchall()
    finally:
        conn.close()
    return cols, rows


_SQL_PLANNER_SYSTEM = (
    "You translate a support-analytics question into exactly ONE SQLite query "
    "(SELECT or WITH...SELECT) over the schema below, or reply with the single "
    "word NONE if the question does not need the database (e.g. it asks about "
    "the content/feel of conversations rather than counts/metrics).\n"
    "Rules: read-only SELECT only; never modify data; always add a LIMIT; "
    "return ONLY the SQL or NONE, with no markdown fences or commentary.\n\n"
    + SCHEMA_DOC
)


def _plan_sql(question):
    """Ask the LLM for a SELECT (or NONE). Returns sql string or None."""
    try:
        raw = llm_client.chat(
            [{"role": "user", "content": question}],
            system=_SQL_PLANNER_SYSTEM,
            max_tokens=400,
            temperature=0.0,
        )
    except llm_client.LLMError:
        return None
    raw = raw.strip()
    # Strip accidental code fences.
    raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", raw).strip()
    if not raw or raw.upper() == "NONE":
        return None
    return raw


def _format_rows(cols, rows, cap=30):
    if not rows:
        return "(no rows)"
    lines = [" | ".join(cols)]
    for r in rows[:cap]:
        lines.append(" | ".join("" if v is None else str(v) for v in r))
    if len(rows) > cap:
        lines.append(f"... ({len(rows) - cap} more rows)")
    return "\n".join(lines)


def retrieve(question, top_k=None):
    """Hybrid retrieve. Returns a dict:
    {
      'semantic': [ {source_type, source_id, title, text, score}, ... ],
      'sql': str | None,           # the SELECT that was run
      'sql_table': str | None,     # rendered result table
      'sql_error': str | None,
    }
    """
    result = {"semantic": [], "sql": None, "sql_table": None, "sql_error": None}

    # Semantic path (best-effort; empty if index not built or embed fails).
    try:
        result["semantic"] = embeddings.search(question, top_k=top_k)
    except Exception:  # noqa: BLE001  — non-fatal; structured path may still answer
        result["semantic"] = []

    # Structured path.
    sql = _plan_sql(question)
    if sql:
        result["sql"] = sql
        try:
            cols, rows = _run_sql(sql)
            result["sql_table"] = _format_rows(cols, rows)
        except ValueError as e:
            result["sql_error"] = f"rejected unsafe SQL: {e}"
        except sqlite3.Error as e:
            result["sql_error"] = f"SQL error: {e}"
    return result


def build_context(retrieved):
    """Render retrieved material into a context block + a sources list."""
    blocks = []
    sources = []
    if retrieved.get("sql_table"):
        blocks.append(
            "## Database query result\n"
            f"Query: {retrieved['sql']}\n\n{retrieved['sql_table']}"
        )
        sources.append({"type": "sql", "ref": retrieved["sql"]})
    elif retrieved.get("sql_error"):
        blocks.append(f"## Database query\n(skipped: {retrieved['sql_error']})")

    if retrieved.get("semantic"):
        lines = ["## Relevant records (semantic search)"]
        for c in retrieved["semantic"]:
            lines.append(f"[{c['title']}] ({c['source_type']}, score {c['score']})\n{c['text']}")
            sources.append({"type": c["source_type"], "ref": c["title"], "score": c["score"]})
        blocks.append("\n\n".join(lines))

    return "\n\n".join(blocks).strip(), sources
