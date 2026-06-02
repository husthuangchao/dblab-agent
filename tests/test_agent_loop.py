"""Drive the agent loop with a fake LLM and fake tools — no network, no DB."""
import dblab_agent.agent as agent


def test_agent_runs_tool_then_finalizes(monkeypatch):
    # First LLM turn asks for a tool; second turn returns a final answer.
    turns = [
        {"role": "assistant", "content": "",
         "tool_calls": [{
             "id": "call_1",
             "function": {"name": "exec_sql",
                          "arguments": '{"conn_id": "postgres", "sql": "SELECT 1"}'},
         }]},
        {"role": "assistant", "content": "tested: SELECT 1 returned 1."},
    ]
    calls = iter(turns)
    monkeypatch.setattr(agent, "chat", lambda *a, **k: next(calls))
    monkeypatch.setattr(agent, "call_tool",
                        lambda name, args: {"ok": True, "columns": ["?column?"],
                                            "rows": [[1]], "rowcount": 1})

    events = list(agent.run_agent([{"role": "user", "content": "is SELECT 1 ok?"}]))
    types = [e["type"] for e in events]
    assert types == ["tool_call", "tool_result", "final"]
    assert events[0]["name"] == "exec_sql"
    assert events[-1]["content"].startswith("tested:")


def test_agent_surfaces_llm_error(monkeypatch):
    def boom(*a, **k):
        raise agent.LLMError("no api key")
    monkeypatch.setattr(agent, "chat", boom)
    events = list(agent.run_agent([{"role": "user", "content": "hi"}]))
    assert events == [{"type": "error", "error": "no api key"}]


def test_preview_trims_rows():
    big = {"ok": True, "columns": ["n"], "rows": [[i] for i in range(50)]}
    p = agent._preview(big)
    assert len(p["rows"]) == 5
    assert p["row_sample_of"] == 50
