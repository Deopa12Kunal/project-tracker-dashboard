"""
Login ID + password sign-in for internal users (accounts are created by an admin).

Passwords are never stored: only a salted PBKDF2-SHA256 hash (600,000 rounds).
Repeated wrong passwords lock that login ID (and the address it comes from) for a while.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time

ITERATIONS = 600_000
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
ROLES = ("admin", "editor", "viewer")
ROLE_LEVEL = {"viewer": 0, "editor": 1, "admin": 2}
MIN_PASSWORD = 8

# Everyone shares one login, so lock mainly per PC: one person's typos shouldn't lock out the whole team.
MAX_FAILS_PER_USER = 30
MAX_FAILS_PER_IP = 5
LOCK_SECONDS = 15 * 60


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


# Used when the login ID doesn't exist, so a wrong ID takes as long as a wrong password.
_DUMMY_HASH = f"pbkdf2_sha256${ITERATIONS}${'0' * 32}${'0' * 64}"


def check_username(username: str) -> str:
    username = (username or "").strip().lower()
    if not USERNAME_RE.match(username):
        raise ValueError("Login ID must be 3–32 characters: lowercase letters, numbers, dot, dash or underscore, "
                         "starting with a letter or number")
    return username


def check_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise ValueError(f"Password must be at least {MIN_PASSWORD} characters")
    if len(password) > 200:
        raise ValueError("Password is too long")
    return password


def check_role(role: str) -> str:
    if role not in ROLES:
        raise ValueError("Role must be admin, editor or viewer")
    return role


class Throttle:
    """Remembers recent failed logins in memory (cleared when the server restarts)."""

    def __init__(self):
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str) -> list[float]:
        cutoff = time.time() - LOCK_SECONDS
        fails = [t for t in self._fails.get(key, []) if t > cutoff]
        if fails:
            self._fails[key] = fails
        else:
            self._fails.pop(key, None)
        return fails

    def locked(self, username: str, ip: str) -> bool:
        with self._lock:
            return (len(self._recent("u:" + username)) >= MAX_FAILS_PER_USER
                    or len(self._recent("ip:" + ip)) >= MAX_FAILS_PER_IP)

    def fail(self, username: str, ip: str) -> None:
        with self._lock:
            for key in ("u:" + username, "ip:" + ip):
                self._fails.setdefault(key, []).append(time.time())

    def clear(self, username: str) -> None:
        with self._lock:
            self._fails.pop("u:" + username, None)


throttle = Throttle()


def authenticate(user: dict | None, password: str) -> bool:
    """`user` is the account row (with password_hash) or None if the login ID is unknown."""
    ok = verify_password(password or "", user["password_hash"] if user else _DUMMY_HASH)
    return bool(ok and user and user["active"])
