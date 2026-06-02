"""Environment-driven configuration. Everything is overridable via env vars
(or a local .env file) so the same code runs under docker-compose, on a bare
host, or in tests.
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # python-dotenv is optional; env vars still work without it
    pass

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DBLAB_DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

CUSTOM_CONNECTIONS_PATH = DATA_DIR / "connections.json"
SECRET_KEY_PATH = DATA_DIR / ".secret"

# Row caps for exec_sql. The agent should use LIMIT in SQL for real sampling;
# these are a safety net so a stray "SELECT *" can't stream a million rows.
DEFAULT_MAX_ROWS = int(os.getenv("DBLAB_MAX_ROWS", "100"))
HARD_MAX_ROWS = int(os.getenv("DBLAB_HARD_MAX_ROWS", "500"))

# Connection + statement timeouts (seconds).
CONNECT_TIMEOUT = int(os.getenv("DBLAB_CONNECT_TIMEOUT", "5"))

# ── LLM (OpenAI-compatible chat-completions endpoint) ──────────────────────
# Works with DeepSeek, OpenAI, Qwen/DashScope, Moonshot, a local Ollama, or any
# server that speaks POST /v1/chat/completions with function calling.
LLM_API_KEY = os.getenv("DBLAB_LLM_API_KEY", "")
LLM_BASE_URL = os.getenv(
    "DBLAB_LLM_BASE_URL", "https://api.deepseek.com/v1/chat/completions"
)
LLM_MODEL = os.getenv("DBLAB_LLM_MODEL", "deepseek-chat")

# ── Vision / multimodal model (used when a message carries an image) ───────
# Defaults to Zhipu (BigModel) GLM-4V; any OpenAI-compatible vision endpoint
# works. Configurable at runtime from the /admin page.
VISION_API_KEY = os.getenv("DBLAB_VISION_API_KEY", "")
VISION_BASE_URL = os.getenv(
    "DBLAB_VISION_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/chat/completions"
)
VISION_MODEL = os.getenv("DBLAB_VISION_MODEL", "glm-4v-plus")

# Optional shared secret to protect the /admin settings write endpoint.
# Empty = open (fine for a local demo); set it for any shared deployment.
ADMIN_TOKEN = os.getenv("DBLAB_ADMIN_TOKEN", "")
