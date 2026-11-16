"""
Web server for the Project Status, AMC & Domain Tracking Dashboard.

    python server.py                       # http://127.0.0.1:8765
    python server.py --port 9000
    python server.py --auto-sync-hours 6   # also re-sync from the CRM every 6 hours

Everyone signs in with a login ID and password created by an admin.
Create the first admin with:  python manage.py add-user admin --name "Your Name" --role admin
Data is stored in SQLite (db.py). Settings come from .env (see .env.example).
"""
from __future__ import annotations

import argparse
import json
import re
import threading
import time
import urllib.parse
from datetime import date
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import auth
import config
import crm_sync
import db

STATIC = f"{config.ROOT}/static"
SESSION_COOKIE = "ptd_session"
PUBLIC_FILES = {"/login.html", "/styles.css"}
LOOPBACK = {"127.0.0.1", "::1"}

AMC_STATUSES = {"", "not_pitched", "pitched", "received", "declined"}
AMC_APPLIES = {"", "yes", "no"}          # "" = automatic (website projects only)
RENEWAL_STATUSES = {"", "not_due", "pending", "renewed", "lost"}
DOMAIN_OWNERS = {"", "webelio", "client"}

AMC_FIELDS = {"applies": "str", "status": "str", "pitched_date": "date", "quoted_amount": "num",
              "received_amount": "num", "received_date": "date", "period_end": "date",
              "informed_date": "date", "notes": "str"}
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


class HttpError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


class Handler(SimpleHTTPRequestHandler):
    server_version = "ProjectTracker"
    sys_version = ""

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC, **kw)

    def log_message(self, fmt, *args):
        first = args[0] if args else ""
        if isinstance(first, str) and ("/api/" in first or "/auth/" in first) and "/api/version" not in first:
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

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _same_origin(self):
        """Requests that change data must come from this dashboard's own pages."""
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
            raise HttpError(403, "Request came from another site")

    def _body(self):
        self._same_origin()
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            raise HttpError(403, "Expected a JSON request")
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
    def _session_cookie(value, max_age):
        parts = [f"{SESSION_COOKIE}={value}", "Path=/", f"Max-Age={int(max_age)}", "HttpOnly", "SameSite=Lax"]
        if config.SECURE_COOKIES:
            parts.append("Secure")
        return "; ".join(parts)

    def _client_ip(self) -> str:
        ip = self.client_address[0]
        if ip in LOOPBACK and self.headers.get("X-Real-IP"):   # behind nginx on the same server
            return self.headers["X-Real-IP"].strip()
        return ip

    def _user(self) -> dict | None:
        return db.session_user(self._cookie(SESSION_COOKIE))

    def _require(self, role: str = "viewer") -> dict:
        user = self._user()
        if not user:
            raise HttpError(401, "Not signed in")
        if auth.ROLE_LEVEL[user["role"]] < auth.ROLE_LEVEL[role]:
            raise HttpError(403, "Only admins can do this" if role == "admin"
                            else "Your account can view but not edit")
        return user

    def _gate_static(self) -> bool:
        if self.route in PUBLIC_FILES or self._user():
            return True
        self._redirect("/login.html")
        return False

    def _handle(self, fn):
        try:
            fn()
        except HttpError as e:
            self._err(str(e), e.status)
        except db.Conflict as e:
            self._err(str(e), 409)
        except KeyError:
            self._err("Not found", 404)
        except PermissionError as e:          # CRM sync: the CRM asked for a login
            self._err(str(e), 502)
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            self._err(str(e))
        except Exception as e:
            print("Request failed:", self.command, self.route, repr(e))
            self._err("Something went wrong on the server. Nothing was changed.", 500)

    # ---- HTTP methods ----
    def do_HEAD(self):
        if self._gate_static():
            super().do_HEAD()

    def do_GET(self):
        r = self.route
        if not r.startswith("/api/"):
            if self._gate_static():
                super().do_GET()
            return
        self._handle(lambda: self._get_api(r))

    def do_POST(self):
        self._handle(lambda: self._post(self.route))

    def do_PUT(self):
        self._handle(lambda: self._put(self.route))

    # ---- GET /api/... ----
    def _get_api(self, r):
        user = self._require()
        if r == "/api/me":
            return self._json(user)
        if r == "/api/version":
            return self._json(db.version())
        if r == "/api/data":
            return self._json({"crm": db.load_store(), "tracking": db.load_tracking(),
                               "server_time": db.now(), "user": user})
        if r == "/api/changes":
            q = self._query()
            limit = int(q.get("limit", 100)) if q.get("limit", "100").isdigit() else 100
            return self._json({"changes": db.changes(q.get("project") or None, limit)})
        if r == "/api/users":
            self._require("admin")
            return self._json({"users": db.list_users()})
        raise HttpError(404, "Not found")

    # ---- POST ----
    def _post(self, r):
        if r == "/auth/login":
            return self._login()
        if r == "/auth/logout":
            self._same_origin()
            db.delete_session(self._cookie(SESSION_COOKIE))
            return self._json({"ok": True}, cookies=[self._session_cookie("", 0)])
        if r == "/api/me/password":
            user = self._require()
            body = self._body()
            full = db.get_user(user["username"], with_hash=True)
            if not auth.verify_password(str(body.get("current") or ""), full["password_hash"]):
                raise HttpError(400, "Your current password is wrong")
            new = auth.check_password(body.get("new"))
            db.update_user(user["username"], user["username"], password_hash=auth.hash_password(new))
            # Changing the password signs out every session, so start a fresh one here.
            token = db.create_session(user["username"], config.SESSION_DAYS)
            return self._json({"ok": True}, cookies=[self._session_cookie(token, config.SESSION_DAYS * 86400)])
        if r == "/api/users":
            admin = self._require("admin")
            body = self._body()
            name = str(body.get("name") or "").strip()[:100]
            if not name:
                raise ValueError("Enter the person's name")
            u = db.create_user(auth.check_username(body.get("username")), name,
                               auth.check_role(body.get("role")),
                               auth.hash_password(auth.check_password(body.get("password"))), admin["username"])
            return self._json({"ok": True, "user": u})
        m = re.fullmatch(r"/api/tracking/project/([A-Za-z0-9_-]{1,64})/informed", r)
        if m:
            user = self._require("editor")
            body = self._body()
            when = str(body.get("date") or date.today().isoformat())
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", when):
                raise ValueError("'date' must be a date (YYYY-MM-DD)")
            return self._json({"ok": True, "entry": db.mark_informed(m.group(1), when, user["username"])})
        if r == "/api/sync":
            user = self._require("editor")
            body = self._body()
            months = [m for m in body.get("months", []) if m]
            urls = [u for u in body.get("urls", []) if u]
            for u in urls:
                if not u.startswith(crm_sync.BASE_URL):
                    raise ValueError(f"Only {crm_sync.BASE_URL} links can be synced")
            if not _sync_lock.acquire(blocking=False):
                raise HttpError(409, "A sync is already running")
            try:
                lines = []
                result = crm_sync.sync(months, urls, log=lines.append, user=user["username"])
            finally:
                _sync_lock.release()
            return self._json({"result": result, "log": lines})
        raise HttpError(404, "Not found")

    def _login(self):
        body = self._body()
        username = str(body.get("username") or "").strip().lower()
        password = str(body.get("password") or "")
        ip = self._client_ip()
        if auth.throttle.locked(username, ip):
            raise HttpError(429, "Too many wrong attempts. Wait 15 minutes and try again, or ask an admin "
                                 "to reset your password.")
        account = db.get_user(username, with_hash=True) if username else None
        if not auth.authenticate(account, password):
            auth.throttle.fail(username, ip)
            print(f"Failed sign-in for '{username}' from {ip}")
            raise HttpError(401, "Wrong login ID or password")
        auth.throttle.clear(username)
        db.touch_login(username)
        token = db.create_session(username, config.SESSION_DAYS)
        print(f"Signed in: {username} from {ip}")
        return self._json({"ok": True}, cookies=[self._session_cookie(token, config.SESSION_DAYS * 86400)])

    # ---- PUT ----
    def _put(self, r):
        m = re.fullmatch(r"/api/tracking/project/([A-Za-z0-9_-]{1,64})", r)
        if m:
            user = self._require("editor")
            body = self._body()
            amc = _clean_section(body.get("amc"), AMC_FIELDS)
            ren = _clean_section(body.get("renewal"), RENEWAL_FIELDS)
            dom = _clean_section(body.get("domain"), DOMAIN_FIELDS)
            if amc.get("status", "") not in AMC_STATUSES:
                raise ValueError("Unknown AMC status")
            if amc.get("applies", "") not in AMC_APPLIES:
                raise ValueError("Unknown AMC 'applies' value")
            if dom.get("ownership", "") not in DOMAIN_OWNERS:
                raise ValueError("Unknown domain ownership")
            if ren.get("status", "") not in RENEWAL_STATUSES:
                raise ValueError("Unknown renewal status")
            entry = {"amc": amc, "renewal": ren, "domain": dom,
                     "notes": str(body.get("notes") or "").strip()[:4000]}
            base = body["base_updated_at"] if "base_updated_at" in body else "any"
            saved = db.save_tracking(m.group(1), entry, user["username"], base)
            return self._json({"ok": True, "entry": saved})
        if r == "/api/settings":
            user = self._require("editor")
            body = self._body()
            s = {}
            s["domain_warning_days"] = max(1, min(365, int(body.get("domain_warning_days", 30))))
            s["no_deadline_grace_days"] = max(0, min(3650, int(body.get("no_deadline_grace_days", 30))))
            s["delayed_statuses"] = [str(x) for x in body.get("delayed_statuses", [])][:20]
            s["currency"] = str(body.get("currency", "₹"))[:5]
            s["amc_default_amount"] = max(0.0, float(body.get("amc_default_amount", 11800)))
            s["amc_due_months"] = max(1, min(120, int(body.get("amc_due_months", 12))))
            s["amc_grace_months"] = max(0, min(60, int(body.get("amc_grace_months", 2))))
            s["whatsapp_template"] = (str(body.get("whatsapp_template") or "").strip()[:2000]
                                      or db.DEFAULT_WHATSAPP_TEMPLATE)
            return self._json({"ok": True, "settings": db.save_settings(s, user["username"])})
        m = re.fullmatch(r"/api/users/([a-z0-9._-]{1,32})", r)
        if m:
            admin = self._require("admin")
            body = self._body()
            kw = {}
            if "name" in body:
                kw["name"] = str(body["name"] or "").strip()[:100] or None
            if "role" in body:
                kw["role"] = auth.check_role(body["role"])
            if "active" in body:
                kw["active"] = bool(body["active"])
            if body.get("password"):
                kw["password_hash"] = auth.hash_password(auth.check_password(body["password"]))
            if m.group(1) == admin["username"] and (kw.get("active") is False):
                raise ValueError("You can't deactivate your own account")
            return self._json({"ok": True, "user": db.update_user(m.group(1), admin["username"], **kw)})
        raise HttpError(404, "Not found")


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

    db.init()
    if db.project_count() == 0:
        print("No CRM data yet - running first sync...")
        crm_sync.sync()
    if args.auto_sync_hours > 0:
        threading.Thread(target=auto_sync_loop, args=(args.auto_sync_hours,), daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Dashboard: http://{args.host}:{args.port}")
    print(f"Database:  {db.DB_PATH}")
    if db.user_count(active_admins_only=True) == 0:
        print("\n  No admin account yet. Create one (in another terminal):\n"
              '    python manage.py add-user admin --name "Your Name" --role admin\n')
    srv.serve_forever()


if __name__ == "__main__":
    main()
