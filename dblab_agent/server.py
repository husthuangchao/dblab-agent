"""FastAPI app: a streaming agent endpoint + connection management + the UI.

Endpoints
  GET    /                       → the single-page chat UI
  GET    /api/connections        → list connections (no passwords)
  POST   /api/connections        → add a custom connection
  DELETE /api/connections/{id}   → remove a custom connection
  POST   /api/agent              → run the agent, streamed as Server-Sent Events
  GET    /api/health             → liveness probe
"""
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .agent import run_agent
from .config import ADMIN_TOKEN
from .config_store import public_settings, save_section
from .connections import add_connection, list_connections, remove_connection

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="dblab-agent", version="0.1.0")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/")
def home():
    return FileResponse(WEB_DIR / "home.html")


@app.get("/chat")
@app.get("/app")
def chat_ui():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/admin")
def admin_page():
    return FileResponse(WEB_DIR / "admin.html")


@app.get("/api/admin/settings")
def api_get_settings():
    """Masked LLM settings for the admin page (never returns full keys)."""
    return public_settings()


@app.post("/api/admin/settings")
async def api_save_settings(request: Request):
    if ADMIN_TOKEN and request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    for name in ("text", "vision"):
        sec = body.get(name)
        if isinstance(sec, dict):
            save_section(
                name,
                api_key=sec.get("api_key"),
                base_url=sec.get("base_url"),
                model=sec.get("model"),
            )
    return {"ok": True, "settings": public_settings()}


@app.get("/api/connections")
def api_list_connections():
    return {"connections": list_connections()}


@app.get("/api/ping/{conn_id}")
def api_ping(conn_id: str):
    """Cheap liveness probe for one connection — runs SELECT 1."""
    from .db_pool import exec_sql
    r = exec_sql(conn_id, "SELECT 1")
    return {"ok": bool(r.get("ok")), "elapsed_ms": r.get("elapsed_ms"),
            "error": r.get("error")}


@app.get("/api/databases/{conn_id}")
def api_databases(conn_id: str):
    """List databases on a connection (powers the UI db dropdown)."""
    from .tools import list_databases
    return list_databases(conn_id)


@app.post("/api/connections")
async def api_add_connection(request: Request):
    body = await request.json()
    try:
        conn_id = add_connection(body)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"id": conn_id, "connections": list_connections()}


@app.delete("/api/connections/{conn_id}")
def api_remove_connection(conn_id: str):
    ok = remove_connection(conn_id)
    if not ok:
        return JSONResponse(
            {"error": "cannot remove a built-in demo connection"}, status_code=400
        )
    return {"ok": True, "connections": list_connections()}


@app.post("/api/agent")
async def api_agent(request: Request):
    body = await request.json()
    messages = body.get("messages") or []
    selected_conn = body.get("connection")
    selected_db = body.get("dbname")
    if not isinstance(messages, list) or not messages:
        return JSONResponse({"error": "messages must be a non-empty list"}, status_code=400)

    # Starlette runs this sync generator in a worker thread, so the blocking
    # SQL/LLM calls inside run_agent don't stall the event loop.
    def event_stream():
        for event in run_agent(messages, selected_conn, selected_db):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
