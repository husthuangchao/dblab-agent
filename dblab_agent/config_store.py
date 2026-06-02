"""Runtime-editable LLM settings, persisted to data/settings.json.

Two sections — "text" (the tool-calling agent model) and "vision" (the
multimodal model used for image messages). Each holds api_key / base_url /
model. Stored values override the env defaults from config.py, so an operator
can change keys from the /admin page without editing files or restarting.

API keys are Fernet-encrypted at rest (see crypto.py); the file never holds
plaintext keys and is gitignored.
"""
import json
import threading

from . import config
from .config import DATA_DIR
from .crypto import decrypt, encrypt

SETTINGS_PATH = DATA_DIR / "settings.json"
_lock = threading.Lock()


def _read() -> dict:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_PATH)


def _section(name: str, env_key: str, env_base: str, env_model: str) -> dict:
    stored = _read().get(name, {})
    key = decrypt(stored["api_key"]) if stored.get("api_key") else env_key
    return {
        "api_key": key,
        "base_url": stored.get("base_url") or env_base,
        "model": stored.get("model") or env_model,
    }


def get_text_config() -> dict:
    return _section("text", config.LLM_API_KEY, config.LLM_BASE_URL, config.LLM_MODEL)


def get_vision_config() -> dict:
    return _section("vision", config.VISION_API_KEY, config.VISION_BASE_URL,
                    config.VISION_MODEL)


def save_section(name: str, *, api_key=None, base_url=None, model=None) -> None:
    """Update one section. An empty/None api_key leaves the stored key as-is so
    the operator can change base_url/model without re-entering the secret."""
    if name not in ("text", "vision"):
        raise ValueError("section must be 'text' or 'vision'")
    with _lock:
        data = _read()
        sec = data.get(name, {})
        if api_key:  # non-empty → replace (encrypted)
            sec["api_key"] = encrypt(api_key)
        if base_url is not None:
            sec["base_url"] = base_url.strip()
        if model is not None:
            sec["model"] = model.strip()
        data[name] = sec
        _write(data)


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return key[:4] + "****" + key[-4:]


def public_settings() -> dict:
    """Masked view for the admin page — never returns full keys."""
    t, v = get_text_config(), get_vision_config()
    return {
        "text": {"base_url": t["base_url"], "model": t["model"],
                 "key_masked": _mask(t["api_key"]), "has_key": bool(t["api_key"])},
        "vision": {"base_url": v["base_url"], "model": v["model"],
                   "key_masked": _mask(v["api_key"]), "has_key": bool(v["api_key"])},
    }
