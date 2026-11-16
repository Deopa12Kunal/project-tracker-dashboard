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

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
AUTO_SYNC_HOURS = float(os.environ.get("AUTO_SYNC_HOURS", "0"))

DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.environ.get("DB_PATH") or os.path.join(DATA_DIR, "tracker.db")

# Public address people open, e.g. https://tracker.test.webel.io (no trailing slash).
# When it starts with https://, login cookies are marked Secure (HTTPS only).
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
SECURE_COOKIES = PUBLIC_URL.startswith("https://")

# How long a sign-in lasts, in days.
SESSION_DAYS = float(os.environ.get("SESSION_DAYS", "7"))
