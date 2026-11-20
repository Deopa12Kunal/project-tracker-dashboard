"""
SQLite storage for the tracker (one file: data/tracker.db by default).

Tables
    crm_projects, crm_snapshots, crm_months, sync_log   - imported from the CRM (sync only)
    tracking                                           - AMC / renewal / domain, edited in the portal:
                                                         one typed column per field (see FIELDS)
    change_log                                         - every change: who, when, field, old -> new
    users, sessions                                    - logins created by an admin
    meta                                               - settings, last sync time, etc.

User accounts and backups are managed with manage.py.

To add a field to the project panel: add it to FIELDS (and CHOICES if it has fixed values),
and add an input named "<section>.<field>" to static/index.html. The column is added to the
database the next time the server starts.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone

import config

DB_PATH = config.DB_PATH
LEGACY_STORE = os.path.join(config.DATA_DIR, "crm_store.json")
LEGACY_TRACKING = os.path.join(config.DATA_DIR, "tracking.json")

DEFAULT_WHATSAPP_TEMPLATE = (
    "Dear {client},\n\n"
    "This is a friendly reminder from Webelio that the Annual Maintenance Contract (AMC) "
    "for your website *{project}* is due for renewal.\n\n"
    "AMC amount: *{amount}* (incl. GST)\n"
    "Due date: {due_date}\n\n"
    "Kindly arrange the payment at your earliest convenience. "
    "Please reach out if you have any questions.\n\n"
    "Thank you,\nTeam Webelio"
)
DEFAULT_SETTINGS = {
    "domain_warning_days": 30,
    "delayed_statuses": ["On Hold", "Disputed", "Not Responding"],
    "no_deadline_grace_days": 30,
    "currency": "₹",
    # AMC schedule (website projects): due N months after the project's first CRM
    # month, automatically "under AMC" after a further grace period.
    "amc_default_amount": 11800,   # incl. GST
    "amc_due_months": 12,
    "amc_grace_months": 2,
    "whatsapp_template": DEFAULT_WHATSAPP_TEMPLATE,
}
ROLES = ("admin", "editor", "viewer")

# --------------------------------------------------------------------------- #
# Fields people fill in for each project. Each one is its own column in the
# `tracking` table, named <section>_<field> (e.g. amc_status, domain_expiry_date).
#   str  - text           date - YYYY-MM-DD (a real calendar date)
#   num  - amount, >= 0   bool - yes/no (stored as 1/0)
# A blank value means "not recorded" and is stored as NULL.
# --------------------------------------------------------------------------- #
FIELDS = {
    "amc": {"applies": "str", "status": "str", "pitched_date": "date", "quoted_amount": "num",
            "received_amount": "num", "received_date": "date", "period_end": "date",
            "informed_date": "date", "notes": "str"},
    "renewal": {"status": "str", "due_date": "date", "renewed_date": "date", "amount": "num",
                "notes": "str"},
    "domain": {"name": "str", "ownership": "str", "registrar": "str", "expiry_date": "date",
               "auto_renew": "bool", "notes": "str"},
}
# Fields that only accept these values (blank is always allowed).
CHOICES = {
    "amc.applies": ("yes", "no"),           # blank = automatic (website projects only)
    "amc.status": ("not_pitched", "pitched", "received", "declined"),
    "renewal.status": ("not_due", "pending", "renewed", "lost"),
    "domain.ownership": ("webelio", "client"),
}
TRACKED_SECTIONS = tuple(FIELDS)
TRACKING_COLUMNS = [(sec, key, kind) for sec, fs in FIELDS.items() for key, kind in fs.items()]
MAX_TEXT = 2000       # per text field
MAX_NOTES = 4000      # project-level notes
SCHEMA_VERSION = 2    # 1 = AMC / renewal / domain stored as JSON text; 2 = one column per field
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _col(sec: str, key: str) -> str:
    return f"{sec}_{key}"


def _column_ddl(sec: str, key: str, kind: str) -> str:
    """Column definition with a CHECK, so the database itself refuses wrong values."""
    col = _col(sec, key)
    choices = CHOICES.get(f"{sec}.{key}")
    if choices:
        return f"{col} TEXT CHECK ({col} IN ({', '.join(repr(v) for v in choices)}))"
    if kind == "num":
        return f"{col} REAL CHECK ({col} IS NULL OR (typeof({col}) = 'real' AND {col} >= 0))"
    if kind == "date":
        return (f"{col} TEXT CHECK ({col} IS NULL OR ({col} GLOB "
                f"'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date({col}) IS {col}))")
    if kind == "bool":
        return f"{col} INTEGER CHECK ({col} IN (0, 1))"
    return f"{col} TEXT CHECK ({col} IS NULL OR (typeof({col}) = 'text' AND length({col}) BETWEEN 1 AND {MAX_TEXT}))"


TRACKING_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS tracking (\n"
    "    project_id TEXT PRIMARY KEY,\n"
    + "".join(f"    {_column_ddl(*f)},\n" for f in TRACKING_COLUMNS)
    + f"    notes      TEXT NOT NULL DEFAULT '' CHECK (length(notes) <= {MAX_NOTES}),\n"
    "    updated_at TEXT,\n"
    "    updated_by TEXT\n"
    ")"
)
_TRACKING_INSERT = (
    f"INSERT OR REPLACE INTO tracking (project_id, "
    f"{', '.join(_col(s, k) for s, k, _ in TRACKING_COLUMNS)}, notes, updated_at, updated_by) "
    f"VALUES ({', '.join('?' * (len(TRACKING_COLUMNS) + 4))})"
)


def _clean_value(name: str, kind: str, v, strict: bool):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if kind == "num":
        try:
            v = float(str(v).replace(",", ""))
        except ValueError:
            raise ValueError(f"'{name}' must be a number")
        if not math.isfinite(v) or v < 0:
            raise ValueError(f"'{name}' must be zero or more")
    elif kind == "date":
        v = str(v).strip()
        try:
            ok = bool(_DATE_RE.match(v)) and bool(date.fromisoformat(v))
        except ValueError:
            ok = False
        if not ok:
            raise ValueError(f"'{name}' must be a real date (YYYY-MM-DD)")
    elif kind == "bool":
        v = bool(v)
    else:
        v = str(v).strip()
        choices = CHOICES.get(name)
        if choices and v not in choices:
            raise ValueError(f"Unknown value '{v}' for '{name}' (allowed: {', '.join(choices)})")
        if len(v) > MAX_TEXT:
            if strict:
                raise ValueError(f"'{name}' is longer than {MAX_TEXT} characters")
            v = v[:MAX_TEXT]
    return v


def clean_entry(body: dict | None, strict: bool = False) -> dict:
    """Check one project's AMC / renewal / domain data and give every field its type.
    Blank values are left out. Unknown fields are ignored, or refused when `strict`
    (used for data already stored, so nothing is ever dropped without notice)."""
    body = body or {}
    out = {}
    for sec, fields in FIELDS.items():
        given = body.get(sec) or {}
        if not isinstance(given, dict):
            raise ValueError(f"'{sec}' must be an object")
        if strict:
            extra = [f"{sec}.{k}" for k, v in given.items() if k not in fields and v not in (None, "")]
            if extra:
                raise ValueError(f"unknown field(s): {', '.join(extra)}")
        out[sec] = {}
        for key, kind in fields.items():
            v = _clean_value(f"{sec}.{key}", kind, given.get(key), strict)
            if v is not None:
                out[sec][key] = v
    notes = str(body.get("notes") or "").strip()
    if len(notes) > MAX_NOTES:
        if strict:
            raise ValueError(f"notes are longer than {MAX_NOTES} characters")
        notes = notes[:MAX_NOTES]
    out["notes"] = notes
    return out


def _tracking_row(pid: str, e: dict, at: str | None, by: str | None) -> list:
    vals = [pid]
    for sec, key, kind in TRACKING_COLUMNS:
        v = e[sec].get(key)
        vals.append(int(v) if kind == "bool" and v is not None else v)
    return vals + [e["notes"], at, by]

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crm_projects (
    id              TEXT PRIMARY KEY,
    crm_project_id  TEXT,
    crm_project_url TEXT,
    crm_client_id   TEXT,
    first_seen      TEXT
);
CREATE TABLE IF NOT EXISTS crm_snapshots (
    project_id TEXT NOT NULL REFERENCES crm_projects(id),
    month      TEXT NOT NULL,
    data       TEXT NOT NULL,
    PRIMARY KEY (project_id, month)
);
CREATE TABLE IF NOT EXISTS crm_months (
    month       TEXT PRIMARY KEY,
    url         TEXT,
    fetched_at  TEXT,
    summary     TEXT,
    project_ids TEXT
);
CREATE TABLE IF NOT EXISTS sync_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    triggered_by TEXT,
    entry        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS change_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    username   TEXT,
    project_id TEXT,
    field      TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT
);
CREATE INDEX IF NOT EXISTS change_log_project ON change_log (project_id, id);
CREATE TABLE IF NOT EXISTS users (
    username            TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    role                TEXT NOT NULL,
    password_hash       TEXT NOT NULL,
    active              INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL,
    created_by          TEXT,
    password_changed_at TEXT,
    last_login          TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    username   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""


class Conflict(Exception):
    """Someone else saved the same record after this person opened it."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextlib.contextmanager
def _db(write: bool = False):
    """One connection per call. Every call is a single transaction."""
    c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    try:
        c.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield c
        c.execute("COMMIT")
    except BaseException:
        if c.in_transaction:
            c.execute("ROLLBACK")
        raise
    finally:
        c.close()


def _get_meta(c, key, default=None):
    r = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def _set_meta(c, key, value):
    c.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
              "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
              (key, json.dumps(value, ensure_ascii=False)))


def _log(c, at, username, project_id, rows):
    c.executemany("INSERT INTO change_log (at, username, project_id, field, old_value, new_value) "
                  "VALUES (?, ?, ?, ?, ?, ?)", [(at, username, project_id, *r) for r in rows])


# --------------------------------------------------------------------------- #
# Setup, upgrades of older databases, one-time import of the old JSON files
# --------------------------------------------------------------------------- #
def init(log=print) -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        cols = lambda t: [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
        # Earlier version used Google sign-in (users keyed by email). No passwords to keep.
        if "email" in cols("users"):
            c.execute("DROP TABLE users")
        if "email" in cols("sessions"):
            c.execute("DROP TABLE sessions")
        if "user_email" in cols("change_log"):
            c.execute("ALTER TABLE change_log RENAME COLUMN user_email TO username")
        c.commit()
        c.executescript(SCHEMA)   # (the tracking table is set up below)
        old_json_format = "amc" in cols("tracking")
        if not old_json_format:
            c.execute(TRACKING_SCHEMA)
            have = set(cols("tracking"))
            for sec, key, kind in TRACKING_COLUMNS:     # fields added to FIELDS since this database was made
                if _col(sec, key) not in have:
                    c.execute(f"ALTER TABLE tracking ADD COLUMN {_column_ddl(sec, key, kind)}")
                    log(f"Added database column tracking.{_col(sec, key)}")
            c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            c.commit()
    finally:
        c.close()
    if old_json_format:
        _migrate_tracking_to_columns(log)
    with _db() as c:
        imported = _get_meta(c, "legacy_imported", False)
    if not imported:
        _import_legacy(log)


def _migrate_tracking_to_columns(log) -> None:
    """Move AMC / renewal / domain data from JSON text into one typed column per field.
    Takes a backup first and runs as one transaction: if any stored value doesn't fit,
    nothing is changed and the server doesn't start until it is fixed."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    copy = os.path.join(os.path.dirname(DB_PATH), f"tracker-before-columns-{stamp}.db")
    backup(copy)
    log(f"Upgrading the tracking table to one column per field (backup: {copy})")
    try:
        with _db(write=True) as c:
            rows = c.execute("SELECT * FROM tracking").fetchall()
            c.execute("ALTER TABLE tracking RENAME TO tracking_json_v1")
            c.execute(TRACKING_SCHEMA)
            for r in rows:
                raw = {s: json.loads(r[s] or "{}") for s in FIELDS}
                raw["notes"] = r["notes"]
                try:
                    e = clean_entry(raw, strict=True)
                except ValueError as err:
                    raise ValueError(f"project {r['project_id']}: {err}")
                c.execute(_TRACKING_INSERT, _tracking_row(r["project_id"], e, r["updated_at"], r["updated_by"]))
            c.execute("DROP TABLE tracking_json_v1")
            c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    except (ValueError, sqlite3.DatabaseError) as err:
        raise SystemExit(f"Could not upgrade the tracking table ({err}). Nothing was changed. "
                         f"Correct that value in the old JSON column, then start again.")
    log(f"Moved {len(rows)} tracking records into typed columns")


def _import_legacy(log) -> None:
    store = tracking = None
    if os.path.exists(LEGACY_STORE):
        with open(LEGACY_STORE, encoding="utf-8") as f:
            store = json.load(f)
    if os.path.exists(LEGACY_TRACKING):
        with open(LEGACY_TRACKING, encoding="utf-8") as f:
            tracking = json.load(f)
    if store:
        save_store(store)
        with _db(write=True) as c:
            for e in reversed(store.get("sync_log", [])):
                c.execute("INSERT INTO sync_log (at, triggered_by, entry) VALUES (?, ?, ?)",
                          (e.get("at") or now(), "import", json.dumps(e, ensure_ascii=False)))
        log(f"Imported {len(store.get('projects', {}))} CRM projects from {LEGACY_STORE}")
    with _db(write=True) as c:
        if tracking:
            _set_meta(c, "settings", {**DEFAULT_SETTINGS, **tracking.get("settings", {})})
            for pid, t in tracking.get("projects", {}).items():
                try:
                    e = clean_entry(t, strict=True)
                except ValueError as err:
                    raise SystemExit(f"Could not import {LEGACY_TRACKING}: project {pid}: {err}")
                c.execute(_TRACKING_INSERT, _tracking_row(pid, e, t.get("updated_at"), "import"))
            log(f"Imported settings and {len(tracking.get('projects', {}))} tracking records "
                f"from {LEGACY_TRACKING}")
        _set_meta(c, "legacy_imported", True)


# --------------------------------------------------------------------------- #
# CRM store (same shape the sync code and the dashboard always used)
# --------------------------------------------------------------------------- #
def project_count() -> int:
    with _db() as c:
        return c.execute("SELECT COUNT(*) FROM crm_projects").fetchone()[0]


def load_store(include_log: bool = True) -> dict:
    with _db() as c:
        projects = {}
        for r in c.execute("SELECT * FROM crm_projects"):
            projects[r["id"]] = {"id": r["id"], "crm_project_id": r["crm_project_id"],
                                 "crm_project_url": r["crm_project_url"],
                                 "crm_client_id": r["crm_client_id"],
                                 "first_seen": r["first_seen"], "history": {}}
        for r in c.execute("SELECT project_id, month, data FROM crm_snapshots"):
            p = projects.get(r["project_id"])
            if p is not None:
                p["history"][r["month"]] = json.loads(r["data"])
        for pid in [k for k, p in projects.items() if not p["history"]]:
            del projects[pid]
        for p in projects.values():
            p["latest_month"] = max(p["history"])
            p["first_month"] = min(p["history"])
            p["latest"] = p["history"][p["latest_month"]]
        months = {}
        for r in c.execute("SELECT * FROM crm_months"):
            months[r["month"]] = {"url": r["url"], "fetched_at": r["fetched_at"],
                                  "summary": json.loads(r["summary"] or "{}"),
                                  "project_ids": json.loads(r["project_ids"] or "[]")}
        sync_log = []
        if include_log:
            for r in c.execute("SELECT s.at, s.triggered_by, s.entry, u.name FROM sync_log s "
                               "LEFT JOIN users u ON u.username = s.triggered_by "
                               "ORDER BY s.id DESC LIMIT 50"):
                e = json.loads(r["entry"])
                e["triggered_by"] = r["triggered_by"]
                e["triggered_by_name"] = r["name"]
                sync_log.append(e)
        return {"source": _get_meta(c, "source"), "last_synced": _get_meta(c, "last_synced"),
                "months_available": _get_meta(c, "months_available", []),
                "months": months, "projects": projects, "sync_log": sync_log}


def save_store(store: dict, sync_entry: dict | None = None, triggered_by: str | None = None) -> None:
    with _db(write=True) as c:
        _set_meta(c, "source", store.get("source"))
        _set_meta(c, "last_synced", store.get("last_synced"))
        _set_meta(c, "months_available", store.get("months_available", []))
        c.executemany(
            "INSERT INTO crm_projects (id, crm_project_id, crm_project_url, crm_client_id, first_seen) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "crm_project_id = excluded.crm_project_id, crm_project_url = excluded.crm_project_url, "
            "crm_client_id = excluded.crm_client_id",
            [(p["id"], p.get("crm_project_id"), p.get("crm_project_url"), p.get("crm_client_id"),
              p.get("first_seen")) for p in store["projects"].values()])
        c.executemany(
            "INSERT INTO crm_snapshots (project_id, month, data) VALUES (?, ?, ?) "
            "ON CONFLICT(project_id, month) DO UPDATE SET data = excluded.data",
            [(p["id"], m, json.dumps(snap, ensure_ascii=False))
             for p in store["projects"].values() for m, snap in p["history"].items()])
        c.executemany(
            "INSERT INTO crm_months (month, url, fetched_at, summary, project_ids) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(month) DO UPDATE SET url = excluded.url, fetched_at = excluded.fetched_at, "
            "summary = excluded.summary, project_ids = excluded.project_ids",
            [(m, v.get("url"), v.get("fetched_at"), json.dumps(v.get("summary") or {}, ensure_ascii=False),
              json.dumps(v.get("project_ids") or [])) for m, v in store["months"].items()])
        if sync_entry is not None:
            c.execute("INSERT INTO sync_log (at, triggered_by, entry) VALUES (?, ?, ?)",
                      (sync_entry["at"], triggered_by, json.dumps(sync_entry, ensure_ascii=False)))


# --------------------------------------------------------------------------- #
# Manual tracking (AMC / renewal / domain) with a change log
# --------------------------------------------------------------------------- #
def _entry(r, name=None) -> dict:
    """A tracking row in the shape the dashboard uses: {"amc": {...}, "renewal": {...}, ...}."""
    e = {sec: {} for sec in FIELDS}
    for sec, key, kind in TRACKING_COLUMNS:
        v = r[_col(sec, key)]
        if v is not None:
            e[sec][key] = bool(v) if kind == "bool" else v
    return {**e, "notes": r["notes"] or "",
            "updated_at": r["updated_at"], "updated_by": r["updated_by"], "updated_by_name": name}


def load_tracking() -> dict:
    with _db() as c:
        settings = {**DEFAULT_SETTINGS, **_get_meta(c, "settings", {})}
        projects = {r["project_id"]: _entry(r, r["name"]) for r in c.execute(
            "SELECT t.*, u.name FROM tracking t LEFT JOIN users u ON u.username = t.updated_by")}
    return {"settings": settings, "projects": projects}


def _flat(entry: dict | None) -> dict:
    out = {}
    for s in TRACKED_SECTIONS:
        for k, v in ((entry or {}).get(s) or {}).items():
            out[f"{s}.{k}"] = v
    if (entry or {}).get("notes"):
        out["notes"] = entry["notes"]
    return out


def _text(v) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _diff(old: dict, new: dict) -> list[tuple[str, str | None, str | None]]:
    out = []
    for k in sorted(set(old) | set(new)):
        a, b = _text(old.get(k)), _text(new.get(k))
        if a != b:
            out.append((k, a, b))
    return out


def _write_tracking(c, pid: str, old: dict | None, new: dict, username: str) -> dict | None:
    new = clean_entry(new)
    changes = _diff(_flat(old), _flat(new))
    if not changes:
        return old
    at = now()
    if not any(new[s] for s in FIELDS) and not new["notes"]:
        c.execute("DELETE FROM tracking WHERE project_id = ?", (pid,))
        result = None
    else:
        c.execute(_TRACKING_INSERT, _tracking_row(pid, new, at, username))
        u = c.execute("SELECT name FROM users WHERE username = ?", (username,)).fetchone()
        result = {**new, "updated_at": at, "updated_by": username, "updated_by_name": u["name"] if u else None}
    _log(c, at, username, pid, changes)
    return result


def _current(c, pid: str) -> dict | None:
    if not c.execute("SELECT 1 FROM crm_projects WHERE id = ?", (pid,)).fetchone():
        raise KeyError(pid)
    r = c.execute("SELECT t.*, u.name FROM tracking t LEFT JOIN users u ON u.username = t.updated_by "
                  "WHERE project_id = ?", (pid,)).fetchone()
    return _entry(r, r["name"]) if r else None


def save_tracking(pid: str, new: dict, username: str, base_updated_at: str | None = "any") -> dict | None:
    """Save one project's manual data and log each changed field. `new` is checked and
    typed with clean_entry, so a wrong value raises ValueError and nothing is saved.
    base_updated_at is the version the person was editing; if someone else has
    saved since, Conflict is raised instead of overwriting their change."""
    with _db(write=True) as c:
        old = _current(c, pid)
        if base_updated_at != "any" and (old or {}).get("updated_at") != base_updated_at:
            who = (old or {}).get("updated_by_name") or (old or {}).get("updated_by") or "someone"
            raise Conflict(f"{who} changed this project while you were editing it. "
                           "Close it and open it again to see their changes, then redo yours.")
        return _write_tracking(c, pid, old, new, username)


def mark_informed(pid: str, date: str, username: str) -> dict | None:
    """Record that the client was sent the AMC reminder (only this one field changes)."""
    with _db(write=True) as c:
        old = _current(c, pid)
        new = json.loads(json.dumps(old or {s: {} for s in TRACKED_SECTIONS}))
        new.setdefault("amc", {})["informed_date"] = date
        return _write_tracking(c, pid, old, new, username)


def save_settings(settings: dict, username: str) -> dict:
    with _db(write=True) as c:
        old = {**DEFAULT_SETTINGS, **_get_meta(c, "settings", {})}
        flat = lambda s: {f"settings.{k}": (", ".join(v) if isinstance(v, list) else v) for k, v in s.items()}
        changes = _diff(flat(old), flat(settings))
        _set_meta(c, "settings", settings)
        _log(c, now(), username, None, changes)
    return settings


def changes(project_id: str | None = None, limit: int = 100) -> list[dict]:
    sql = ("SELECT l.*, u.name AS user_name FROM change_log l "
           "LEFT JOIN users u ON u.username = l.username")
    args: list = []
    if project_id:
        sql += " WHERE l.project_id = ?"
        args.append(project_id)
    sql += " ORDER BY l.id DESC LIMIT ?"
    args.append(max(1, min(1000, limit)))
    with _db() as c:
        return [dict(r) for r in c.execute(sql, args)]


def version() -> dict:
    """Cheap fingerprint so open dashboards can tell when someone else changed something."""
    with _db() as c:
        return {"changes": c.execute("SELECT COALESCE(MAX(id), 0) FROM change_log").fetchone()[0],
                "syncs": c.execute("SELECT COALESCE(MAX(id), 0) FROM sync_log").fetchone()[0]}


# --------------------------------------------------------------------------- #
# Users (created by an admin) and sessions (only a hash of the token is stored)
# --------------------------------------------------------------------------- #
PUBLIC_USER_FIELDS = "username, name, role, active, created_at, created_by, password_changed_at, last_login"


def user_count(active_admins_only: bool = False) -> int:
    sql = "SELECT COUNT(*) FROM users" + (" WHERE role = 'admin' AND active = 1" if active_admins_only else "")
    with _db() as c:
        return c.execute(sql).fetchone()[0]


def list_users() -> list[dict]:
    with _db() as c:
        return [dict(r) for r in c.execute(f"SELECT {PUBLIC_USER_FIELDS} FROM users ORDER BY username")]


def get_user(username: str, with_hash: bool = False) -> dict | None:
    cols = "*" if with_hash else PUBLIC_USER_FIELDS
    with _db() as c:
        r = c.execute(f"SELECT {cols} FROM users WHERE username = ?", (username,)).fetchone()
        return dict(r) if r else None


def create_user(username: str, name: str, role: str, password_hash: str, by: str) -> dict:
    at = now()
    with _db(write=True) as c:
        if c.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            raise ValueError(f"The login ID '{username}' is already taken")
        c.execute("INSERT INTO users (username, name, role, password_hash, active, created_at, created_by, "
                  "password_changed_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                  (username, name, role, password_hash, at, by, at))
        _log(c, at, by, None, [(f"user:{username}", None, f"Created: {name} ({role})")])
    return get_user(username)


def update_user(username: str, by: str, *, name=None, role=None, active=None, password_hash=None) -> dict:
    """Admin changes to an account. Signs the person out when they lose access or get a new password."""
    with _db(write=True) as c:
        old = c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not old:
            raise KeyError(username)
        new_role = role if role is not None else old["role"]
        new_active = int(active) if active is not None else old["active"]
        if old["role"] == "admin" and old["active"] and (new_role != "admin" or not new_active):
            admins = c.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1").fetchone()[0]
            if admins <= 1:
                raise ValueError("This is the only active admin. Make someone else an admin first.")
        at = now()
        rows = []
        if name is not None and name != old["name"]:
            c.execute("UPDATE users SET name = ? WHERE username = ?", (name, username))
            rows.append((f"user:{username}.name", old["name"], name))
        if new_role != old["role"]:
            c.execute("UPDATE users SET role = ? WHERE username = ?", (new_role, username))
            rows.append((f"user:{username}.role", old["role"], new_role))
        if new_active != old["active"]:
            c.execute("UPDATE users SET active = ? WHERE username = ?", (new_active, username))
            rows.append((f"user:{username}.active", "Active" if old["active"] else "Deactivated",
                         "Active" if new_active else "Deactivated"))
        if password_hash:
            c.execute("UPDATE users SET password_hash = ?, password_changed_at = ? WHERE username = ?",
                      (password_hash, at, username))
            rows.append((f"user:{username}.password", None, "Password changed" if by == username else "Password reset"))
        if password_hash or not new_active:
            c.execute("DELETE FROM sessions WHERE username = ?", (username,))
        _log(c, at, by, None, rows)
    return get_user(username)


def set_single_login(username: str, name: str, password_hash: str | None) -> None:
    """Make `username` the one active admin login and switch every other account off.
    `password_hash` is None when the stored password already matches."""
    at = now()
    rows = []
    with _db(write=True) as c:
        old = c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not old:
            c.execute("INSERT INTO users (username, name, role, password_hash, active, created_at, created_by, "
                      "password_changed_at) VALUES (?, ?, 'admin', ?, 1, ?, 'system', ?)",
                      (username, name, password_hash, at, at))
            rows.append((f"user:{username}", None, f"Created: {name} (admin)"))
        else:
            c.execute("UPDATE users SET name = ?, role = 'admin', active = 1 WHERE username = ?", (name, username))
            if password_hash:
                c.execute("UPDATE users SET password_hash = ?, password_changed_at = ? WHERE username = ?",
                          (password_hash, at, username))
                c.execute("DELETE FROM sessions WHERE username = ?", (username,))
                rows.append((f"user:{username}.password", None, "Password changed in .env"))
        for (other,) in c.execute("SELECT username FROM users WHERE username != ? AND active = 1",
                                  (username,)).fetchall():
            c.execute("UPDATE users SET active = 0 WHERE username = ?", (other,))
            rows.append((f"user:{other}.active", "Active", "Deactivated"))
        c.execute("DELETE FROM sessions WHERE username != ?", (username,))
        if rows:
            _log(c, at, "system", None, rows)


def touch_login(username: str) -> None:
    with _db(write=True) as c:
        c.execute("UPDATE users SET last_login = ? WHERE username = ?", (now(), username))


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(username: str, days: float) -> str:
    token = secrets.token_urlsafe(32)
    at = datetime.now(timezone.utc)
    with _db(write=True) as c:
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (at.isoformat(timespec="seconds"),))
        c.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)",
                  (_hash(token), username, at.isoformat(timespec="seconds"),
                   (at + timedelta(days=days)).isoformat(timespec="seconds")))
    return token


def session_user(token: str | None) -> dict | None:
    """The signed-in user for this session token, if the session and the account are still valid."""
    if not token:
        return None
    with _db() as c:
        r = c.execute("SELECT u.username, u.name, u.role FROM sessions s JOIN users u ON u.username = s.username "
                      "WHERE s.token_hash = ? AND s.expires_at > ? AND u.active = 1",
                      (_hash(token), now())).fetchone()
        return dict(r) if r else None


def delete_session(token: str | None) -> None:
    if token:
        with _db(write=True) as c:
            c.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))


def backup(dest: str) -> None:
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
