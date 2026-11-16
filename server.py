"""
Web server for the Project Status, AMC & Domain Tracking Dashboard.

    python server.py                       # http://127.0.0.1:8765
    python server.py --port 9000
    python server.py --auto-sync-hours 6   # also re-sync from the CRM every 6 hours

Configuration (Google sign-in, public URL, database path) comes from environment
variables or a .env file; see .env.example. Data is stored in SQLite (db.py).
Without GOOGLE_CLIENT_ID the server runs in local mode: no login, and it only
accepts requests made on this machine.
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import auth
import config
import crm_sync
import db

STATIC = f"{config.ROOT}/static"
SESSION_COOKIE = "ptd_session"
STATE_COOKIE = "ptd_oauth_state"
PUBLIC_FILES = {"/login.html", "/styles.css"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
LOCAL_USER = {"email": "local", "name": "Local mode (no login)", "picture": None, "role": "editor"}

AMC_STATUSES = {"", "not_pitched", "pitched", "received", "declined"}
AMC_APPLIES = {"", "yes", "no"}          # "" = automatic (website projects only)
RENEWAL_STATUSES = {"", "not_due", "pending", "renewed", "lost"}
DOMAIN_OWNERS = {"", "webelio", "client"}

AMC_FIELDS = {"applies": "str", "status": "str", "pitched_date": "date", "quoted_amount": "num",
              "received_amount": "num", "received_date": "date", "period_end": "date",
              "notes": "str"}
RENEWAL_FIELDS = {"status": "str", "due_date": "date", "renewed_date": "date",
                  "amount": "num", "notes": "str"}
DOMAIN_FIELDS = {"name": "str", "ownership": "str", "registrar": "str", "expiry_date": "date",
                 "auto_renew": "bool", "notes": "str"}

_sync_lock = threading.Lock()


def _clean_section(sec: dict | None, allowed: dict) -> dict:
    """Keep only known keys; coerce numbers/dates; blank -> absent."""
    out = {}
    for key, kind in allowed.items():
        v = (sec or {}).get(key)
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        if kind == "num":
            try:
                v = float(str(v).replace(",", ""))
            except ValueError:
                raise ValueError(f"'{key}' must be a number")
        elif kind == "date":
            v = str(v).strip()
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
                raise ValueError(f"'{key}' must be a date (YYYY-MM-DD)")
        elif kind == "bool":
            v = bool(v)
        else:
            v = str(v).strip()[:2000]
        out[key] = v
    return out


class Forbidden(Exception):
    pass


class Handler(SimpleHTTPRequestHandler):
    server_version = "ProjectTracker"
    sys_version = ""

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC, **kw)

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else "") or "/auth/" in (args[0] if args else ""):
            super().log_message(fmt, *args)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    # ---- helpers ----
    @property
    def route(self) -> str:
        return urllib.parse.urlsplit(self.path).path

    def _query(self) -> dict:
        return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}

    def _json(self, obj, status=200, cookies=()):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for c in cookies:
            self.send_header("Set-Cookie", c)
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg, status=400):
        self._json({"error": msg}, status)

    def _redirect(self, location, cookies=()):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        for c in cookies:
            self.send_header("Set-Cookie", c)
        self.end_headers()

    def _body(self):
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            raise Forbidden("Expected a JSON request")
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            raise ValueError("Request too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def _cookie(self, name):
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except Exception:
            return None
        return jar[name].value if name in jar else None

    @staticmethod
    def _set_cookie(name, value, max_age, path="/"):
        parts = [f"{name}={value}", f"Path={path}", f"Max-Age={int(max_age)}", "HttpOnly", "SameSite=Lax"]
        if config.SECURE_COOKIES:
            parts.append("Secure")
        return "; ".join(parts)

    def _local_request(self) -> bool:
        """Local mode only serves requests made on this machine, not via a proxy."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        proxied = self.headers.get("X-Forwarded-For") or self.headers.get("X-Real-IP")
        return host in LOCAL_HOSTS and not proxied and self.client_address[0] in {"127.0.0.1", "::1"}

    def _user(self) -> dict | None:
        if not config.AUTH_ENABLED:
            return LOCAL_USER if self._local_request() else None
        email = db.session_email(self._cookie(SESSION_COOKIE))
        if not email or auth.access_error(email):
            return None
        u = db.get_user(email) or {}
        return {"email": email, "name": u.get("name") or email, "picture": u.get("picture"),
                "role": auth.role_for(email)}

    def _require_editor(self):
        """For requests that change data: signed in, editor, and sent from our own page."""
        user = self._user()
        if not user:
            raise PermissionError("Not signed in")
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
            raise Forbidden("Request came from another site")
        if user["role"] != "editor":
            raise Forbidden("Your account can view but not edit")
        return user

    # ---- sign-in ----
    def _login(self):
        if not config.AUTH_ENABLED:
            return self._redirect("/")
        state = secrets.token_urlsafe(24)
        self._redirect(auth.login_url(state), [self._set_cookie(STATE_COOKIE, state, 600, "/auth")])

    def _callback(self):
        q = self._query()
        clear = self._set_cookie(STATE_COOKIE, "", 0, "/auth")
        expected = self._cookie(STATE_COOKIE)
        if q.get("error"):
            return self._redirect("/login.html?error=cancelled", [clear])
        if not expected or not secrets.compare_digest(expected, q.get("state", "")) or not q.get("code"):
            return self._redirect("/login.html?error=state", [clear])
        try:
            claims = auth.exchange_code(q["code"])
        except auth.AuthError as e:
            print("Sign-in refused:", e)
            return self._redirect(f"/login.html?error={e.code}", [clear])
        db.upsert_user(claims["email"], claims.get("name"), claims.get("picture"))
        token = db.create_session(claims["email"], config.SESSION_DAYS)
        print(f"Signed in: {claims['email']}")
        self._redirect("/", [clear, self._set_cookie(SESSION_COOKIE, token, config.SESSION_DAYS * 86400)])

    # ---- routes ----
    def _gate_static(self) -> bool:
        """True if the static file may be served; otherwise sends a redirect."""
        if self.route in PUBLIC_FILES:
            return True
        if self._user():
            return True
        if not config.AUTH_ENABLED:
            self.send_error(403, "Login is not configured, so this dashboard only works on the server itself")
        else:
            self._redirect("/login.html")
        return False

    def do_HEAD(self):
        if self._gate_static():
            super().do_HEAD()

    def do_GET(self):
        r = self.route
        if r == "/auth/login":
            return self._login()
        if r == "/auth/callback":
            return self._callback()
        if r.startswith("/api/"):
            user = self._user()
            if not user:
                return self._err("Not signed in", 401)
            if r == "/api/me":
                return self._json({**user, "auth_enabled": config.AUTH_ENABLED})
            if r == "/api/data":
                return self._json({"crm": db.load_store(), "tracking": db.load_tracking(),
                                   "server_time": db.now(), "user": user})
            if r == "/api/changes":
                q = self._query()
                try:
                    limit = int(q.get("limit", 100))
                except ValueError:
                    limit = 100
                return self._json({"changes": db.changes(q.get("project") or None, limit)})
            return self._err("Not found", 404)
        if self._gate_static():
            super().do_GET()

    def do_POST(self):
        try:
            if self.route == "/auth/logout":
                origin = self.headers.get("Origin")
                if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
                    raise Forbidden("Request came from another site")
                db.delete_session(self._cookie(SESSION_COOKIE))
                return self._json({"ok": True}, cookies=[self._set_cookie(SESSION_COOKIE, "", 0)])
            if self.route == "/api/sync":
                user = self._require_editor()
                body = self._body()
                months = [m for m in body.get("months", []) if m]
                urls = [u for u in body.get("urls", []) if u]
                for u in urls:
                    if not u.startswith(crm_sync.BASE_URL):
                        return self._err(f"Only {crm_sync.BASE_URL} links can be synced")
                if not _sync_lock.acquire(blocking=False):
                    return self._err("A sync is already running", 409)
                try:
                    lines = []
                    result = crm_sync.sync(months, urls, log=lines.append, user=user["email"])
                finally:
                    _sync_lock.release()
                return self._json({"result": result, "log": lines})
            return self._err("Not found", 404)
        except Forbidden as e:
            return self._err(str(e), 403)
        except PermissionError as e:
            if str(e) == "Not signed in":
                return self._err(str(e), 401)
            return self._err(str(e), 502)
        except (ValueError, json.JSONDecodeError) as e:
            return self._err(str(e))
        except Exception as e:
            return self._err(f"Sync failed: {e}", 502)

    def do_PUT(self):
        try:
            m = re.fullmatch(r"/api/tracking/project/([A-Za-z0-9_-]{1,64})", self.route)
            if m:
                user = self._require_editor()
                pid = m.group(1)
                body = self._body()
                amc = _clean_section(body.get("amc"), AMC_FIELDS)
                ren = _clean_section(body.get("renewal"), RENEWAL_FIELDS)
                dom = _clean_section(body.get("domain"), DOMAIN_FIELDS)
                if amc.get("status", "") not in AMC_STATUSES:
                    return self._err("Unknown AMC status")
                if amc.get("applies", "") not in AMC_APPLIES:
                    return self._err("Unknown AMC 'applies' value")
                if dom.get("ownership", "") not in DOMAIN_OWNERS:
                    return self._err("Unknown domain ownership")
                if ren.get("status", "") not in RENEWAL_STATUSES:
                    return self._err("Unknown renewal status")
                entry = {"amc": amc, "renewal": ren, "domain": dom,
                         "notes": str(body.get("notes") or "").strip()[:4000]}
                try:
                    saved = db.save_tracking(pid, entry, user["email"])
                except KeyError:
                    return self._err("Unknown project id", 404)
                return self._json({"ok": True, "entry": saved})
            if self.route == "/api/settings":
                user = self._require_editor()
                body = self._body()
                s = {}
                s["domain_warning_days"] = max(1, min(365, int(body.get("domain_warning_days", 30))))
                s["no_deadline_grace_days"] = max(0, min(3650, int(body.get("no_deadline_grace_days", 30))))
                s["delayed_statuses"] = [str(x) for x in body.get("delayed_statuses", [])][:20]
                s["currency"] = str(body.get("currency", "₹"))[:5]
                s["amc_default_amount"] = max(0.0, float(body.get("amc_default_amount", 11800)))
                s["amc_due_months"] = max(1, min(120, int(body.get("amc_due_months", 12))))
                s["amc_grace_months"] = max(0, min(60, int(body.get("amc_grace_months", 2))))
                return self._json({"ok": True, "settings": db.save_settings(s, user["email"])})
            return self._err("Not found", 404)
        except Forbidden as e:
            return self._err(str(e), 403)
        except PermissionError as e:
            return self._err(str(e), 401)
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            return self._err(str(e))
        except Exception as e:
            print("Save failed:", repr(e))
            return self._err("Save failed on the server. Nothing was changed.", 500)


def auto_sync_loop(hours: float):
    while True:
        time.sleep(hours * 3600)
        if _sync_lock.acquire(blocking=False):
            try:
                print(f"[{db.now()}] auto-sync starting")
                crm_sync.sync()
            except Exception as e:
                print("auto-sync failed:", e)
            finally:
                _sync_lock.release()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=config.HOST)
    ap.add_argument("--port", type=int, default=config.PORT)
    ap.add_argument("--auto-sync-hours", type=float, default=config.AUTO_SYNC_HOURS)
    args = ap.parse_args()

    if config.AUTH_ENABLED:
        missing = [k for k, v in {"GOOGLE_CLIENT_SECRET": config.GOOGLE_CLIENT_SECRET,
                                  "PUBLIC_URL": config.PUBLIC_URL}.items() if not v]
        if missing:
            raise SystemExit(f"Google sign-in is on, but {', '.join(missing)} is not set. See .env.example.")
    elif args.host not in LOCAL_HOSTS:
        raise SystemExit(f"Refusing to listen on {args.host} without login. "
                         "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET (see .env.example).")

    db.init()
    if db.project_count() == 0:
        print("No CRM data yet - running first sync...")
        crm_sync.sync()
    if args.auto_sync_hours > 0:
        threading.Thread(target=auto_sync_loop, args=(args.auto_sync_hours,), daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    mode = (f"Google sign-in for @{config.ALLOWED_DOMAIN}, public URL {config.PUBLIC_URL}"
            if config.AUTH_ENABLED else "local mode, no login")
    print(f"Dashboard: http://{args.host}:{args.port}  ({mode})")
    print(f"Database:  {db.DB_PATH}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
