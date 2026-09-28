"""Runtime configuration. Everything secret comes from environment variables
(GitHub Actions secrets in CI, a local .env file on a laptop)."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
WAREHOUSE_DIR = DATA_DIR / "warehouse"      # append-only JSONL, committed to git
DB_PATH = DATA_DIR / "substrate.db"         # rebuilt from the warehouse, gitignored
SITE_DATA_DIR = ROOT / "site" / "data"      # JSON the dashboard / extension / worker read
BOM_DIR = ROOT / "bom"


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if v[:1] in ('"', "'") and v[0] in v[1:]:
            v = v[1:v.index(v[0], 1)]
        else:
            v = "" if v.startswith("#") else v.split(" #", 1)[0].split("\t#", 1)[0].strip()  # drop inline comments
        os.environ.setdefault(k.strip(), v)


_load_dotenv()


def env(name: str, default: str | None = None) -> str | None:
    v = (os.environ.get(name) or "").strip().lstrip("﻿").strip()  # secrets piped from PowerShell can carry a BOM
    return v or default


# SEC fair-access policy requires a descriptive User-Agent with a contact email.
SEC_CONTACT_EMAIL = env("SEC_CONTACT_EMAIL")
USER_AGENT = "Mozilla/5.0 (compatible; SubstrateBot/0.1; +https://github.com/Shungite-Muncher/Substrate)"

# LLM: Groq free tier (primary), Gemini (optional fallback). Leave both unset to use the template writer.
GROQ_API_KEY = env("GROQ_API_KEY")
GROQ_MODEL = env("GROQ_MODEL")  # optional; default openai/gpt-oss-120b
GEMINI_API_KEY = env("GEMINI_API_KEY")
GEMINI_MODEL = env("GEMINI_MODEL")  # optional pin; otherwise auto-selected

# Part-data providers. Octopart (via Nexar) is primary; Mouser / DigiKey are free fallbacks.
NEXAR_CLIENT_ID = env("NEXAR_CLIENT_ID")
NEXAR_CLIENT_SECRET = env("NEXAR_CLIENT_SECRET")
# Nexar's Evaluation app has a small *lifetime* part allowance, so budget it hard.
NEXAR_MONTHLY_PART_BUDGET = int(env("NEXAR_MONTHLY_PART_BUDGET", "40"))
MOUSER_API_KEY = env("MOUSER_API_KEY")
DIGIKEY_CLIENT_ID = env("DIGIKEY_CLIENT_ID")
DIGIKEY_CLIENT_SECRET = env("DIGIKEY_CLIENT_SECRET")
# Re-query a part at most this often (days). Cached snapshots are reused otherwise.
PART_REFRESH_DAYS = int(env("PART_REFRESH_DAYS", "3"))

# Customer profile used to tailor reports.
CUSTOMER_NAME = env("SUBSTRATE_CUSTOMER", "Demo Distributor")
CUSTOMER_PEERS = [t.strip() for t in env("SUBSTRATE_PEERS", "ARW,AVT").split(",") if t.strip()]

HTTP_CACHE_DIR = DATA_DIR / ".http_cache"
