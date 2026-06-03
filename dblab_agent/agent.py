"""The plan-and-execute agent loop.

The LLM decides when to call tools and when to answer. Tools run real SQL.
The loop yields a stream of events so the UI can show each step live:

    {"type": "tool_call",   "name", "args"}
    {"type": "tool_result", "name", "result"}   # compact preview
    {"type": "final",       "content"}
    {"type": "error",       "error"}
"""
import json

from .llm import LLMError, chat, vision_chat
from .tools import call_tool, tool_schemas

MAX_ITERATIONS = 24          # hard ceiling so the loop can't run away
TOOL_RESULT_CHAR_CAP = 16000  # how much tool output we feed back to the model

SYSTEM_PROMPT = """You are **dblab-agent**, a database behaviour investigator.
You answer questions about **PostgreSQL, MySQL, and openGauss** by RUNNING REAL
SQL against live databases — never from memory alone.

Connections (call `list_connections` for the live list and exact ids):
- `postgres`  — PostgreSQL
- `mysql`     — MySQL
- `opengauss` — openGauss (PostgreSQL-derived; speaks the PG protocol)
Users may add their own connections too.

## THE IRON RULE — no claim without evidence
Before you state that any function, syntax, data type, or feature is
"supported" / "not supported" / behaves a certain way, you MUST run a minimal
test with `exec_sql` and read the real result. **Untested = no conclusion.**
Never guess from training knowledge and present it as fact about a specific
database.

## How to work
1. Use the connection the user asked about (or the one selected in the UI). If
   unsure which connections exist, call `list_connections` first.
2. "Is X supported / how does Y behave?" → write the smallest SQL that proves it
   and run it. For a comparison across databases, run the SAME probe on each
   connection and lay the results side by side (a great fit for `exec_sql_batch`
   when the probes are independent).
3. Prefer `SELECT` / `EXPLAIN`. Run `INSERT/UPDATE/DELETE/DDL` only if the user
   explicitly asks; clean up after yourself when you do.
4. Sample with `LIMIT` inside the SQL, not by relying on the row cap.
5. After a tool returns, summarise in plain language — don't paste raw rows back.
6. If a tool errors, change your approach and retry; don't loop on the same error.

## Source labelling (important for trust)
- For anything you actually ran, say **"tested:"** and state what you observed.
- For background you state from standard knowledge, say **"per the X manual"** or
  **"by the SQL standard"** — and prefer to verify it with a query when it matters.
- Never blur tested facts and remembered facts together.

## exec_sql vs exec_sql_batch
- `exec_sql_batch`: you already know several INDEPENDENT things to check at once
  (e.g. `version()`, a setting, and a feature probe) — one round trip.
- `exec_sql`: each step depends on the previous result. When in doubt, single-step.

Answer in the user's language. Be concrete and concise."""


VISION_SYSTEM_PROMPT = """You are the vision module of dblab-agent. The user
has attached a screenshot — typically a SQL error, a table schema, a monitoring
panel, a slow query, or console output from PostgreSQL, MySQL, or openGauss.

Your job:
1. Read the key facts in the image precisely (error codes, SQL text, table and
   column names, config values, stack traces, metric numbers).
2. Diagnose or advise using your knowledge of PostgreSQL / MySQL / openGauss.
3. Label clearly whether each statement is "from the image" or "general
   knowledge". If the image is too small or ambiguous to be sure, say what extra
   detail you'd need (full error text, the table DDL, etc.).
Answer in the user's language; summarise in plain words rather than transcribing.
"""


def _has_image(messages: list[dict]) -> bool:
    """The frontend sends content as a list when an image is attached:
    [{type:'text',...}, {type:'image_url', image_url:{url:'data:...'}}]."""
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
    return False


def _trim_for_vision(messages: list[dict], char_budget: int = 3000) -> list[dict]:
    """Keep the final (image-bearing) message intact; walk backwards over older
    turns accumulating their TEXT only (old images dropped) until the budget is
    hit. Vision models read the image reliably only while the prompt stays small."""
    if len(messages) <= 1:
        return messages
    kept: list[dict] = []
    used = 0
    for m in reversed(messages[:-1]):
        c = m.get("content")
        if isinstance(c, list):
            text = " ".join(p.get("text", "") for p in c
                            if isinstance(p, dict) and p.get("type") == "text")
        else:
            text = c or ""
        if used + len(text) > char_budget:
            break
        used += len(text)
        kept.append({"role": m.get("role", "user"), "content": text})
    kept.reverse()
    return kept + [messages[-1]]


def _run_vision(user_messages: list[dict]):
    messages = [{"role": "system", "content": VISION_SYSTEM_PROMPT}]
    messages.extend(_trim_for_vision(user_messages))
    try:
        msg = vision_chat(messages)
    except LLMError as e:
        yield {"type": "error", "error": str(e)}
        return
    yield {"type": "final", "content": msg.get("content") or ""}


def _preview(result: dict) -> dict:
    """Trim a tool result for the UI event (the full result still goes to the
    model). Keeps row counts and a few sample rows, drops bulky payloads."""
    if not isinstance(result, dict):
        return {"value": str(result)[:400]}
    out = {k: v for k, v in result.items() if k not in ("rows", "results")}
    if "rows" in result and isinstance(result["rows"], list):
        out["rows"] = result["rows"][:5]
        out["row_sample_of"] = len(result["rows"])
    if "results" in result and isinstance(result["results"], list):
        out["results"] = [
            {kk: vv for kk, vv in r.items() if kk != "rows"}
            for r in result["results"][:8]
        ]
    return out


def run_agent(user_messages: list[dict], selected_conn: str | None = None,
              selected_db: str | None = None):
    """Drive the loop. `user_messages` is the running chat history
    ([{role, content}, ...]); the system prompt is prepended here.
    `selected_conn` / `selected_db` are what the user picked in the UI, if any."""
    # Image attached → route this turn to the multimodal model (no SQL tools).
    if _has_image(user_messages):
        yield from _run_vision(user_messages)
        return

    system = SYSTEM_PROMPT
    if selected_conn:
        system += (f"\n\nThe user has selected the connection `{selected_conn}` in "
                   f"the UI. Prefer it for questions that don't name another database.")
        if selected_db:
            system += f" Their selected database is `{selected_db}`."
    messages: list[dict] = [{"role": "system", "content": system}]
    messages.extend(user_messages)

    for _ in range(MAX_ITERATIONS):
        try:
            msg = chat(messages, tools=tool_schemas())
        except LLMError as e:
            yield {"type": "error", "error": str(e)}
            return

        tool_calls = msg.get("tool_calls")
        if tool_calls:
            messages.append({"role": "assistant",
                             "content": msg.get("content") or "",
                             "tool_calls": tool_calls})
        else:
            messages.append({"role": "assistant", "content": msg.get("content") or ""})
            yield {"type": "final", "content": msg.get("content") or ""}
            return

        for tc in tool_calls:
            fn_name = tc.get("function", {}).get("name", "")
            raw_args = tc.get("function", {}).get("arguments") or "{}"
            try:
                args = json.loads(raw_args)
            except Exception:
                args = {}
            yield {"type": "tool_call", "name": fn_name, "args": args}

            result = call_tool(fn_name, args)
            yield {"type": "tool_result", "name": fn_name, "result": _preview(result)}

            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id"),
                "content": json.dumps(result, ensure_ascii=False)[:TOOL_RESULT_CHAR_CAP],
            })

    yield {"type": "final",
           "content": "Reached the maximum number of steps without a final answer. "
                      "Try narrowing the question."}
