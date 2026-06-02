"""Per-(conn_id, dbname) connection pool and the exec_sql primitive.

exec_sql is the one place SQL actually runs. It always returns a structured
dict — success or failure — so tools and the LLM never see a raw driver
exception or a half-open transaction.
"""
import queue
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal

from .config import DEFAULT_MAX_ROWS, HARD_MAX_ROWS
from .connections import CONNECTIONS, open_conn
from .safety import is_write

_POOL_MAX = 5
_ACQUIRE_TIMEOUT = 10  # seconds to wait for a free slot

_pools: dict = {}
_pools_lock = threading.Lock()


def _coerce(v):
    """Make a DB value JSON-serializable."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        try:
            return bytes(v).decode("utf-8")
        except Exception:
            return f"<{len(v)} bytes>"
    return v


class _Pool:
    def __init__(self, cfg: dict, dbname: str | None):
        self._cfg = cfg
        self._dbname = dbname
        self._idle: queue.LifoQueue = queue.LifoQueue()
        self._sem = threading.Semaphore(_POOL_MAX)

    def _alive(self, conn) -> bool:
        try:
            if self._cfg["driver"] == "pg":
                return not conn.closed
            conn.ping(reconnect=False)
            return True
        except Exception:
            return False

    @contextmanager
    def acquire(self):
        if not self._sem.acquire(timeout=_ACQUIRE_TIMEOUT):
            raise TimeoutError("connection pool exhausted")
        conn = None
        try:
            try:
                while True:
                    candidate = self._idle.get_nowait()
                    if self._alive(candidate):
                        conn = candidate
                        break
                    try:
                        candidate.close()
                    except Exception:
                        pass
            except queue.Empty:
                pass
            if conn is None:
                conn = open_conn(self._cfg, self._dbname)
            yield conn
            # Return a clean connection to the pool. For PG/openGauss a stray
            # aborted transaction would poison the next user, so roll back.
            if self._cfg["driver"] == "pg":
                try:
                    conn.rollback()
                except Exception:
                    pass
            self._idle.put(conn)
            conn = None
        finally:
            if conn is not None:  # an error escaped — discard the connection
                try:
                    conn.close()
                except Exception:
                    pass
            self._sem.release()


def _get_pool(conn_id: str, dbname: str | None) -> _Pool:
    key = (conn_id, dbname or CONNECTIONS[conn_id]["dbname"])
    with _pools_lock:
        pool = _pools.get(key)
        if pool is None:
            pool = _Pool(CONNECTIONS[conn_id], key[1])
            _pools[key] = pool
        return pool


def exec_sql(conn_id: str, sql: str, dbname: str | None = None,
             max_rows: int = DEFAULT_MAX_ROWS) -> dict:
    """Run one SQL statement and return a structured result.

    On success: {ok, columns, rows, rowcount, truncated, elapsed_ms}
    On failure: {ok: False, error, elapsed_ms}
    """
    if conn_id not in CONNECTIONS:
        return {"ok": False, "error": f"unknown conn_id: {conn_id}",
                "available": list(CONNECTIONS.keys())}
    max_rows = max(1, min(int(max_rows or DEFAULT_MAX_ROWS), HARD_MAX_ROWS))
    driver = CONNECTIONS[conn_id]["driver"]
    pool = _get_pool(conn_id, dbname)
    t0 = time.time()
    try:
        with pool.acquire() as conn:
            cur = conn.cursor()
            cur.execute(sql)
            columns: list[str] = []
            rows: list[list] = []
            truncated = False
            if cur.description:  # a result set (SELECT/EXPLAIN/SHOW/...)
                columns = [d[0] for d in cur.description]
                fetched = cur.fetchmany(max_rows)
                rows = [[_coerce(v) for v in r] for r in fetched]
                if len(fetched) == max_rows and cur.fetchone() is not None:
                    truncated = True
            elif driver == "pg" and is_write(sql):
                # MySQL autocommits; PG/openGauss must commit explicit writes.
                conn.commit()
            rowcount = cur.rowcount
            cur.close()
            return {
                "ok": True,
                "columns": columns,
                "rows": rows,
                "rowcount": rowcount,
                "truncated": truncated,
                "elapsed_ms": int((time.time() - t0) * 1000),
            }
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
