"""Tiny SQL classifier. Used to label statements and to decide whether a
PostgreSQL/openGauss transaction needs an explicit COMMIT.

This is intentionally NOT a hard gate: the agent is allowed to run writes when
the user explicitly asks. The system prompt is what keeps it read-only by
default; this module just helps the executor and the UI reason about intent.
"""
import re

_WRITE_RE = re.compile(
    r"^\s*(insert|update|delete|merge|replace|create|alter|drop|truncate|"
    r"rename|grant|revoke|comment|call|do|vacuum|analyze)\b",
    re.IGNORECASE,
)


def is_write(sql: str) -> bool:
    """True if the statement may modify data or schema (best-effort)."""
    return bool(_WRITE_RE.match(sql or ""))
