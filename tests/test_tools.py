"""Unit tests that need no database — they exercise pure logic."""
from dblab_agent import safety
from dblab_agent.tools import call_tool, tool_schemas


def test_is_write_classifier():
    assert safety.is_write("INSERT INTO t VALUES (1)")
    assert safety.is_write("  update t set x=1")
    assert safety.is_write("DROP TABLE t")
    assert not safety.is_write("SELECT 1")
    assert not safety.is_write("  explain select * from t")
    assert not safety.is_write("WITH x AS (SELECT 1) SELECT * FROM x")


def test_tool_schemas_shape():
    schemas = tool_schemas()
    names = {s["function"]["name"] for s in schemas}
    assert names == {
        "list_connections", "list_databases", "list_tables",
        "get_object_detail", "exec_sql", "exec_sql_batch",
    }
    for s in schemas:
        assert s["type"] == "function"
        assert "parameters" in s["function"]


def test_call_unknown_tool_returns_error():
    out = call_tool("nope", {})
    assert "error" in out and "unknown tool" in out["error"]


def test_call_tool_unknown_conn_is_graceful():
    out = call_tool("exec_sql", {"conn_id": "does_not_exist", "sql": "SELECT 1"})
    assert "error" in out
