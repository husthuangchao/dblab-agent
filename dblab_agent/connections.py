"""Connection registry + driver opener.

Two sources of connections, merged into CONNECTIONS at import time:
  1. Built-in demo connections (PostgreSQL / MySQL / openGauss), configured
     entirely through environment variables.
  2. User-added connections, persisted (passwords encrypted) to
     data/connections.json and editable at runtime through the HTTP API.

A connection config is a plain dict:
    {"label", "driver" ("pg"|"mysql"), "host", "port", "user", "password", "dbname"}
"""
import json
import os
import re
import uuid

import psycopg2
import pymysql
from pymysql.constants import CLIENT

from .config import CONNECT_TIMEOUT, CUSTOM_CONNECTIONS_PATH, DATA_DIR
from .crypto import decrypt, encrypt


def _cfg(label, driver, host, port, user, password, dbname):
    return {
        "label": label,
        "driver": driver,
        "host": host,
        "port": int(port),
        "user": user,
        "password": password,
        "dbname": dbname,
    }


def _builtin_connections() -> dict:
    """The three demo databases. Hosts/ports/passwords come from env so the
    same image works under docker-compose (service names) or against a host."""
    return {
        "postgres": _cfg(
            "PostgreSQL", "pg",
            os.getenv("PG_HOST", "127.0.0.1"), os.getenv("PG_PORT", "5432"),
            os.getenv("PG_USER", "postgres"), os.getenv("PG_PASSWORD", "postgres"),
            os.getenv("PG_DB", "demo"),
        ),
        "mysql": _cfg(
            "MySQL", "mysql",
            os.getenv("MYSQL_HOST", "127.0.0.1"), os.getenv("MYSQL_PORT", "3306"),
            os.getenv("MYSQL_USER", "root"), os.getenv("MYSQL_PASSWORD", "mysql"),
            os.getenv("MYSQL_DB", "demo"),
        ),
        "opengauss": _cfg(
            "openGauss", "pg",
            os.getenv("OPENGAUSS_HOST", "127.0.0.1"), os.getenv("OPENGAUSS_PORT", "5433"),
            os.getenv("OPENGAUSS_USER", "gaussdb"),
            os.getenv("OPENGAUSS_PASSWORD", "Gauss@bcd1234"),
            os.getenv("OPENGAUSS_DB", "postgres"),
        ),
    }


CONNECTIONS: dict = _builtin_connections()


# ── Custom (user-added) connection persistence ─────────────────────────────
def _read_custom() -> dict:
    if not CUSTOM_CONNECTIONS_PATH.exists():
        return {}
    try:
        data = json.loads(CUSTOM_CONNECTIONS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    for cfg in data.values():
        if isinstance(cfg, dict) and "password" in cfg:
            cfg["password"] = decrypt(cfg.get("password") or "")
    return data


def _write_custom(items: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = {}
    for cid, cfg in items.items():
        c = dict(cfg)
        if "password" in c:
            c["password"] = encrypt(c.get("password") or "")
        out[cid] = c
    tmp = CUSTOM_CONNECTIONS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CUSTOM_CONNECTIONS_PATH)


def _is_builtin(conn_id: str) -> bool:
    return conn_id in _builtin_connections()


def _normalize(body: dict, existing_id: str | None = None) -> tuple[str, dict]:
    driver = (body.get("driver") or "").strip().lower()
    if driver in ("postgres", "postgresql", "pg", "opengauss", "gauss"):
        driver = "pg"
    elif driver in ("mysql", "mariadb"):
        driver = "mysql"
    else:
        raise ValueError("driver must be 'mysql' or 'pg'")
    user = (body.get("user") or "").strip()
    if not user:
        raise ValueError("user is required")
    try:
        port = int(body.get("port") or (5432 if driver == "pg" else 3306))
    except (TypeError, ValueError):
        raise ValueError("port must be a number")
    label = (body.get("label") or body.get("id") or "custom").strip()
    cfg = _cfg(
        label, driver,
        (body.get("host") or "127.0.0.1").strip(), port,
        user, str(body.get("password") or ""),
        (body.get("dbname") or ("postgres" if driver == "pg" else "mysql")).strip(),
    )
    raw_id = existing_id or body.get("id") or label
    conn_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(raw_id).strip().lower()).strip("_")[:48]
    if not conn_id:
        conn_id = f"custom_{uuid.uuid4().hex[:8]}"
    if _is_builtin(conn_id) and conn_id != existing_id:
        conn_id = f"custom_{conn_id}"
    return conn_id, cfg


def add_connection(body: dict) -> str:
    """Validate, persist, and register a user-supplied connection. Returns id."""
    conn_id, cfg = _normalize(body)
    custom = _read_custom()
    custom[conn_id] = cfg
    _write_custom(custom)
    CONNECTIONS[conn_id] = cfg
    return conn_id


def remove_connection(conn_id: str) -> bool:
    """Delete a user-added connection. Built-in demo connections are protected."""
    if _is_builtin(conn_id):
        return False
    custom = _read_custom()
    if conn_id in custom:
        del custom[conn_id]
        _write_custom(custom)
    CONNECTIONS.pop(conn_id, None)
    return True


def list_connections() -> list[dict]:
    """Public view of all connections — never includes passwords."""
    return [
        {
            "id": cid,
            "label": c["label"],
            "driver": c["driver"],
            "host": c["host"],
            "port": c["port"],
            "default_db": c["dbname"],
            "builtin": _is_builtin(cid),
        }
        for cid, c in CONNECTIONS.items()
    ]


# ── Driver opener ──────────────────────────────────────────────────────────
def open_conn(cfg: dict, dbname: str | None = None):
    """Open a raw DBAPI connection. openGauss speaks the PostgreSQL wire
    protocol, so it uses the same psycopg2 path as 'pg'."""
    db = dbname or cfg["dbname"]
    if cfg["driver"] == "pg":
        return psycopg2.connect(
            host=cfg["host"], port=cfg["port"], user=cfg["user"],
            password=cfg["password"], dbname=db, connect_timeout=CONNECT_TIMEOUT,
        )
    return pymysql.connect(
        host=cfg["host"], port=cfg["port"], user=cfg["user"],
        password=cfg["password"], database=db, connect_timeout=CONNECT_TIMEOUT,
        autocommit=True, charset="utf8mb4", client_flag=CLIENT.MULTI_STATEMENTS,
    )


# Merge user-added connections over the built-ins at import time.
CONNECTIONS.update(_read_custom())
