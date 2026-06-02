"""Minimal OpenAI-compatible chat client (stdlib only).

Targets any server exposing POST /v1/chat/completions with tool/function
calling: DeepSeek, OpenAI, Qwen/DashScope, Zhipu/BigModel, Moonshot, a local
Ollama, etc. Two entry points share one transport:

    chat()        -> the text/agent model      (config_store.get_text_config)
    vision_chat() -> the multimodal model       (config_store.get_vision_config)

Configs are read at call time, so changes made on the /admin page take effect
on the next request without a restart.
"""
import json
import urllib.error
import urllib.request

from .config_store import get_text_config, get_vision_config


class LLMError(Exception):
    pass


def _post(cfg: dict, messages: list[dict], tools: list[dict] | None,
          temperature: float, max_tokens: int, timeout: int) -> dict:
    if not cfg.get("api_key"):
        raise LLMError(
            "No API key configured. Set it in the /admin page or via environment."
        )
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    req = urllib.request.Request(
        cfg["base_url"],
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="ignore")[:300]
        except Exception:
            detail = ""
        raise LLMError(f"LLM HTTP {e.code}: {detail}")
    except Exception as e:
        raise LLMError(f"{type(e).__name__}: {e}")
    try:
        return data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise LLMError(f"unexpected LLM response shape: {str(data)[:300]}")


def chat(messages: list[dict], tools: list[dict] | None = None,
         temperature: float = 0.2, max_tokens: int = 4000,
         timeout: int = 120) -> dict:
    """One round with the text/agent model. May return `tool_calls`."""
    return _post(get_text_config(), messages, tools, temperature, max_tokens, timeout)


def _strip_data_uri(messages: list[dict]) -> list[dict]:
    """Zhipu GLM-4V wants raw base64 in image_url.url, not an OpenAI-style
    `data:image/...;base64,` URI. Rewrite a copy of the messages for it.
    (OpenAI/Qwen keep the full data URI, so only Zhipu endpoints get stripped.)"""
    out = []
    for m in messages:
        c = m.get("content")
        if not isinstance(c, list):
            out.append(m)
            continue
        parts = []
        for p in c:
            if (isinstance(p, dict) and p.get("type") == "image_url"
                    and isinstance(p.get("image_url"), dict)):
                url = p["image_url"].get("url", "")
                if isinstance(url, str) and url.startswith("data:") and "base64," in url:
                    url = url.split("base64,", 1)[1]
                parts.append({"type": "image_url", "image_url": {"url": url}})
            else:
                parts.append(p)
        out.append({**m, "content": parts})
    return out


def vision_chat(messages: list[dict], temperature: float = 0.3,
                max_tokens: int = 2048, timeout: int = 120) -> dict:
    """One round with the multimodal model (no tools)."""
    cfg = get_vision_config()
    if "bigmodel" in (cfg.get("base_url") or ""):
        messages = _strip_data_uri(messages)
    return _post(cfg, messages, None, temperature, max_tokens, timeout)
