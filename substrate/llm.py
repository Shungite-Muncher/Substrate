"""LLM client (no SDKs). Groq's free tier is the default; Gemini is an optional
fallback. Both are called over plain REST so there is nothing to install."""
from __future__ import annotations

import logging
import re

from . import config, http

log = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# Free tier: 30 req/min, 1K req/day, 8K tokens/min -> keep prompts compact.
GROQ_DEFAULT_MODEL = "openai/gpt-oss-120b"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
MAX_PROMPT_CHARS = 16000   # ~4K tokens, leaves room for the answer inside 8K TPM
_gemini_models: dict[str, str] = {}


def available() -> bool:
    return bool(config.GROQ_API_KEY or config.GEMINI_API_KEY)


def generate(prompt: str, *, system: str | None = None, lite: bool = False, temperature: float = 0.4,
             max_tokens: int = 1500) -> tuple[str, str]:
    """Returns (text, model_name). Raises on failure so callers can fall back to templates."""
    errors = []
    if config.GROQ_API_KEY:
        try:
            return _groq(prompt, system, temperature, max_tokens)
        except Exception as e:
            log.warning("groq failed: %s", e)
            errors.append(f"groq: {e}")
    if config.GEMINI_API_KEY:
        try:
            return _gemini(prompt, system, lite, temperature, max_tokens)
        except Exception as e:
            log.warning("gemini failed: %s", e)
            errors.append(f"gemini: {e}")
    raise RuntimeError("; ".join(errors) or "no LLM key configured")


def _groq(prompt, system, temperature, max_tokens) -> tuple[str, str]:
    model = config.GROQ_MODEL or GROQ_DEFAULT_MODEL
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    body = {"model": model, "messages": msgs, "temperature": temperature, "max_completion_tokens": max_tokens}
    if "gpt-oss" in model:
        # reasoning tokens count against the free-tier budget; keep them small and out of the reply
        body.update(reasoning_effort="low", include_reasoning=False)
    headers = {"Authorization": f"Bearer {config.GROQ_API_KEY}"}
    try:
        r = http.post(GROQ_URL, json=body, headers=headers, timeout=90)
    except Exception as e:
        if "400" not in str(e) or "reasoning_effort" not in body:
            raise
        for k in ("reasoning_effort", "include_reasoning"):  # older models reject these params
            body.pop(k, None)
        r = http.post(GROQ_URL, json=body, headers=headers, timeout=90)
    j = r.json()
    text = (j.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    if not text.strip():
        raise RuntimeError(f"empty Groq response: {str(j)[:300]}")
    return text.strip(), f"groq/{model}"


def _version(name: str) -> tuple:
    m = re.search(r"gemini-(\d+)(?:\.(\d+))?", name)
    return (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)


def _gemini_model(lite: bool) -> str:
    if config.GEMINI_MODEL:
        return config.GEMINI_MODEL
    key = "lite" if lite else "flash"
    if key not in _gemini_models:
        fallback = "gemini-flash-lite-latest" if lite else "gemini-flash-latest"
        try:
            r = http.get(f"{GEMINI_BASE}/models?pageSize=200", headers={"x-goog-api-key": config.GEMINI_API_KEY})
            names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                     if "generateContent" in m.get("supportedGenerationMethods", [])]
            cands = [n for n in names if "flash" in n and ("lite" in n) == lite
                     and not re.search(r"preview|exp|image|tts|live|audio|thinking", n) and not n.endswith("latest")]
            _gemini_models[key] = max(cands, key=_version) if cands else fallback
        except Exception:
            _gemini_models[key] = fallback
    return _gemini_models[key]


def _gemini(prompt, system, lite, temperature, max_tokens) -> tuple[str, str]:
    model = _gemini_model(lite)
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    r = http.post(f"{GEMINI_BASE}/models/{model}:generateContent", json=body,
                  headers={"x-goog-api-key": config.GEMINI_API_KEY}, timeout=120)
    j = r.json()
    cands = j.get("candidates") or []
    text = "".join(p.get("text", "") for p in (cands[0].get("content", {}).get("parts", []) if cands else []))
    if not text.strip():
        raise RuntimeError(f"empty Gemini response: {str(j)[:300]}")
    return text.strip(), f"gemini/{model}"
