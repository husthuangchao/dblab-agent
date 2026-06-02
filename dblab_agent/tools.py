"""The agent's tool belt.

"Smart agent, dumb tools": every tool is a pure function that wraps a SQL
primitive. No tool calls the LLM, none does prompt engineering — all judgement
lives in the model. Tools just execute and return structured dicts.
"""
import time
from concurrent.futures import ThreadPoolExecutor

from .connections import CONNECTIONS
from .db_pool import exec_sql

_BATCH_MAX_QUERIES = 8
_BATCH_MAX_WORKERS = 5


# ── Tool implementations ───────────────────────────────────────────────────
def list_connections() -> dict:
    return {
        "connections": [
            {"id": cid, "label": c["label"], "driver": c["driver"],
             "default_db": c["dbname"]}
            for cid, c in CONNECTIONS.items()
        ]
    }


def list_databases(conn_id: str) -> dict:
    if conn_id not in CONNECTIONS:
        return {"error": f"unknown conn_id: {conn_id}",
                "available": list(CONNECTIONS.keys())}
    driver = CONNECTIONS[conn_id]["driver"]
    if driver == "pg":
        sql = ("SELECT datname FROM pg_database "
               "WHERE datistemplate = false ORDER BY datname")
    else:
        sql = "SHOW DATABASES"
    r = exec_sql(conn_id, sql)
    if not r.get("ok"):
        return {"error": r.get("error")}
    return {"databases": [row[0] for row in r.get("rows", [])]}


def list_tables(conn_id: str, dbname: str | None = None,
                schema: str | None = None, include_system: bool = False) -> dict:
    if conn_id not in CONNECTIONS:
        return {"error": f"unknown conn_id: {conn_id}",
                "available": list(CONNECTIONS.keys())}
    driver = CONNECTIONS[conn_id]["driver"]

    def safe(s: str) -> str:
        return "".join(ch for ch in (s or "") if ch.isalnum() or ch in "_-.")

    where: list[str] = []
    if schema:
        s = safe(schema)
        if s:
            where.append(f"table_schema = '{s}'")
    elif driver == "mysql" and dbname:
        # MySQL's information_schema is server-wide; scope to the chosen db.
        d = safe(dbname)
        if d:
            where.append(f"table_schema = '{d}'")
    elif not include_system:
        if driver == "pg":
            where.append("table_schema NOT IN ('pg_catalog','information_schema')")
        else:
            where.append(
                "table_schema NOT IN "
                "('mysql','information_schema','performance_schema','sys')"
            )
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = (f"SELECT table_schema, table_name, table_type "
           f"FROM information_schema.tables {where_sql} "
           f"ORDER BY table_schema, table_name")
    r = exec_sql(conn_id, sql, dbname, max_rows=500)
    if not r.get("ok"):
        return {"error": r.get("error")}
    items = [{"schema": row[0], "name": row[1], "kind": row[2]}
             for row in r.get("rows", [])]
    by_schema: dict[str, int] = {}
    for it in items:
        by_schema[it["schema"]] = by_schema.get(it["schema"], 0) + 1
    out = {"total": len(items), "schemas": by_schema, "items": items[:200]}
    if len(items) > 200:
        out["note"] = f"items truncated to first 200 of {len(items)}"
    return out


def get_object_detail(conn_id: str, schema: str, name: str,
                      kind: str = "table", dbname: str | None = None) -> dict:
    """Columns + indexes for one table/view. Enough for the agent to reason
    about a schema without a full DDL dump."""
    if conn_id not in CONNECTIONS:
        return {"error": f"unknown conn_id: {conn_id}"}
    driver = CONNECTIONS[conn_id]["driver"]

    def safe(s: str) -> str:
        return "".join(ch for ch in (s or "") if ch.isalnum() or ch in "_-.$")

    sch, nm = safe(schema), safe(name)
    col_sql = (
        "SELECT column_name, data_type, is_nullable, column_default, "
        "character_maximum_length, numeric_precision, numeric_scale "
        "FROM information_schema.columns "
        f"WHERE table_schema = '{sch}' AND table_name = '{nm}' "
        "ORDER BY ordinal_position"
    )
    cr = exec_sql(conn_id, col_sql, dbname, max_rows=500)
    if not cr.get("ok"):
        return {"error": cr.get("error")}
    columns = [
        {"name": r[0], "type": r[1], "nullable": r[2], "default": r[3],
         "char_len": r[4], "precision": r[5], "scale": r[6]}
        for r in cr.get("rows", [])
    ]

    indexes: list = []
    if driver == "pg":
        idx_sql = (f"SELECT indexname, indexdef FROM pg_indexes "
                   f"WHERE schemaname = '{sch}' AND tablename = '{nm}'")
        ir = exec_sql(conn_id, idx_sql, dbname, max_rows=200)
        if ir.get("ok"):
            indexes = [{"name": r[0], "definition": r[1]} for r in ir.get("rows", [])]
    else:
        idx_sql = (
            "SELECT index_name, "
            "GROUP_CONCAT(column_name ORDER BY seq_in_index) AS cols, "
            "MAX(non_unique) AS non_unique "
            "FROM information_schema.statistics "
            f"WHERE table_schema = '{sch}' AND table_name = '{nm}' "
            "GROUP BY index_name"
        )
        ir = exec_sql(conn_id, idx_sql, dbname, max_rows=200)
        if ir.get("ok"):
            indexes = [{"name": r[0], "columns": r[1],
                        "unique": str(r[2]) == "0"} for r in ir.get("rows", [])]

    if not columns:
        return {"error": f"object {schema}.{name} not found or has no columns",
                "schema": schema, "name": name}
    return {"schema": schema, "name": name, "kind": kind,
            "columns": columns, "indexes": indexes}


def exec_sql_tool(conn_id: str, sql: str, dbname: str | None = None,
                  max_rows: int = 100) -> dict:
    if conn_id not in CONNECTIONS:
        return {"error": f"unknown conn_id: {conn_id}"}
    return exec_sql(conn_id, sql, dbname, max_rows)


def exec_sql_batch(conn_id: str, queries: list[str], dbname: str | None = None,
                   max_rows: int = 100) -> dict:
    """Run several independent SQLs in parallel on one connection — one round
    trip instead of N. Only for queries that don't depend on each other."""
    if conn_id not in CONNECTIONS:
        return {"error": f"unknown conn_id: {conn_id}"}
    if not isinstance(queries, list) or not queries:
        return {"error": "queries must be a non-empty list of SQL strings"}
    if len(queries) > _BATCH_MAX_QUERIES:
        return {"error": f"batch too large ({len(queries)} > {_BATCH_MAX_QUERIES}); "
                         "split into multiple calls"}
    t0 = time.time()
    workers = min(len(queries), _BATCH_MAX_WORKERS)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(lambda q: exec_sql(conn_id, q, dbname, max_rows), queries))
    return {
        "total_ms": int((time.time() - t0) * 1000),
        "count": len(queries),
        "results": [{"sql": queries[i], **results[i]} for i in range(len(queries))],
    }


# ── Registry: name -> (callable, JSON-Schema) ──────────────────────────────
_REGISTRY = {
    "list_connections": (
        list_connections,
        "List all configured database connections (id, label, driver, default db).",
        {"type": "object", "properties": {}, "required": []},
    ),
    "list_databases": (
        list_databases,
        "List databases/catalogs on a connection. Call list_connections first "
        "if you don't know the conn_id.",
        {"type": "object",
         "properties": {"conn_id": {"type": "string",
                                    "description": "e.g. 'postgres', 'mysql', 'opengauss'"}},
         "required": ["conn_id"]},
    ),
    "list_tables": (
        list_tables,
        "List tables and views in a database. Returns {total, schemas, items}. "
        "MySQL connections are auto-scoped to dbname; PG/openGauss are scoped by "
        "catalog. Use schema= to focus one schema, include_system=true for system schemas.",
        {"type": "object",
         "properties": {
             "conn_id": {"type": "string"},
             "dbname": {"type": "string", "description": "Database/catalog to look in."},
             "schema": {"type": "string", "description": "Restrict to one schema."},
             "include_system": {"type": "boolean", "default": False},
         },
         "required": ["conn_id"]},
    ),
    "get_object_detail": (
        get_object_detail,
        "Get columns and indexes for one table or view.",
        {"type": "object",
         "properties": {
             "conn_id": {"type": "string"},
             "schema": {"type": "string", "description": "Schema name; 'public' for PG default."},
             "name": {"type": "string"},
             "kind": {"type": "string", "enum": ["table", "view"], "default": "table"},
             "dbname": {"type": "string"},
         },
         "required": ["conn_id", "schema", "name"]},
    ),
    "exec_sql": (
        exec_sql_tool,
        "Run ONE SQL statement and return columns/rows (or an error). This is how "
        "you get evidence: to prove a function/syntax/feature works, run the "
        "smallest query that demonstrates it. Default 100 rows, max 500 — use "
        "LIMIT in SQL for real sampling.",
        {"type": "object",
         "properties": {
             "conn_id": {"type": "string"},
             "sql": {"type": "string"},
             "dbname": {"type": "string"},
             "max_rows": {"type": "integer", "default": 100},
         },
         "required": ["conn_id", "sql"]},
    ),
    "exec_sql_batch": (
        exec_sql_batch,
        "Run up to 8 INDEPENDENT read-only SQLs in parallel on one connection. "
        "Use when you already know several things to check at once (e.g. version() "
        "+ a setting + a feature probe). For step-by-step exploration use exec_sql.",
        {"type": "object",
         "properties": {
             "conn_id": {"type": "string"},
             "queries": {"type": "array", "items": {"type": "string"}},
             "dbname": {"type": "string"},
             "max_rows": {"type": "integer", "default": 100},
         },
         "required": ["conn_id", "queries"]},
    ),
}


def tool_schemas() -> list[dict]:
    """OpenAI function-calling tool list."""
    return [
        {"type": "function",
         "function": {"name": name, "description": desc, "parameters": params}}
        for name, (_fn, desc, params) in _REGISTRY.items()
    ]


def call_tool(name: str, args: dict) -> dict:
    entry = _REGISTRY.get(name)
    if entry is None:
        return {"error": f"unknown tool: {name}"}
    fn = entry[0]
    try:
        return fn(**(args or {}))
    except TypeError as e:
        return {"error": f"bad arguments for {name}: {e}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
