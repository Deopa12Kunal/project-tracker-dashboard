"""
Settings for the tracker, read from environment variables or a `.env` file
next to this script (see .env.example). Real environment variables win over .env.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.abspath(__file__))


def _load_env_file(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file(os.path.join(ROOT, ".env"))


def _set(name: str) -> set[str]:
    return {x.strip().lower() for x in os.environ.get(name, "").split(",") if x.strip()}


HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
AUTO_SYNC_HOURS = float(os.environ.get("AUTO_SYNC_HOURS", "0"))

DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.environ.get("DB_PATH") or os.path.join(DATA_DIR, "tracker.db")

# Public address people open, e.g. https://tracker.test.webel.io (no trailing slash).
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

# Google Workspace sign-in. Leaving GOOGLE_CLIENT_ID empty turns login off,
# which is only allowed for local use on this machine.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
ALLOWED_DOMAIN = os.environ.get("ALLOWED_DOMAIN", "webel.io").strip().lower().lstrip("@")
ALLOWED_EMAILS = _set("ALLOWED_EMAILS")    # empty = anyone @ALLOWED_DOMAIN
READONLY_EMAILS = _set("READONLY_EMAILS")  # these people can view but not edit
SESSION_DAYS = float(os.environ.get("SESSION_DAYS", "14"))

AUTH_ENABLED = bool(GOOGLE_CLIENT_ID)
SECURE_COOKIES = PUBLIC_URL.startswith("https://")
