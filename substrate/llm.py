"""Minimal Gemini REST client (no SDK). Picks the best available free-tier Flash
model at runtime so the code keeps working as Google renames models."""
from __future__ import annotations

import logging
import re

from . import config, http

log = logging.getLogger(__name__)
BASE = "https://generativelanguage.googleapis.com/v1beta"
_model_cache: dict[str, str] = {}


def available() -> bool:
    return bool(config.GEMINI_API_KEY)


def _version(name: str) -> tuple:
    m = re.search(r"gemini-(\d+)(?:\.(\d+))?", name)
    return (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)


def pick_model(lite: bool = False) -> str:
    if config.GEMINI_MODEL:
        return config.GEMINI_MODEL
    key = "lite" if lite else "flash"
    if key in _model_cache:
        return _model_cache[key]
    fallback = "gemini-flash-lite-latest" if lite else "gemini-flash-latest"
    try:
        r = http.get(f"{BASE}/models?pageSize=200", headers={"x-goog-api-key": config.GEMINI_API_KEY})
        names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
        cands = [n for n in names if "flash" in n and ("lite" in n) == lite
                 and not re.search(r"preview|exp|image|tts|live|audio|thinking", n)
                 and not n.endswith("latest")]
        _model_cache[key] = max(cands, key=_version) if cands else fallback
    except Exception as e:
        log.warning("gemini model list failed (%s); using %s", e, fallback)
        _model_cache[key] = fallback
    return _model_cache[key]


def generate(prompt: str, *, system: str | None = None, lite: bool = False, temperature: float = 0.4,
             max_tokens: int = 2048) -> tuple[str, str]:
    """Returns (text, model_name). Raises on failure so callers can fall back."""
    model = pick_model(lite)
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    r = http.post(f"{BASE}/models/{model}:generateContent", json=body,
                  headers={"x-goog-api-key": config.GEMINI_API_KEY}, timeout=120)
    j = r.json()
    cands = j.get("candidates") or []
    text = "".join(p.get("text", "") for p in (cands[0].get("content", {}).get("parts", []) if cands else []))
    if not text.strip():
        raise RuntimeError(f"empty Gemini response: {str(j)[:300]}")
    return text.strip(), model
