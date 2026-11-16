"""
CRM sync for the Webelio public Monthly Report.

Source: https://crm.webel.io/tracker/public/monthly-report?month=YYYY-MM
(public HTML page; the CSV export and other tracker pages require a CRM login,
so this module only reads the public monthly report).

- Discovers every available month from the page's month <select>.
- Parses each month's project table + summary cards.
- Merges into the database (db.py) keyed by CRM project id (no duplicates).
- Keeps a per-month snapshot of every project (history is never discarded).
- Records which fields were missing in the CRM instead of guessing values.

Usage:
    python crm_sync.py                 # sync all months the CRM lists
    python crm_sync.py 2025-05 2025-06 # sync only these months
    python crm_sync.py --url "https://crm.webel.io/tracker/public/monthly-report?month=2025-07&pm=&status=&website="
"""
from __future__ import annotations

import hashlib
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser

import db

BASE_URL = "https://crm.webel.io/tracker/public/monthly-report"
CRM_ORIGIN = "https://crm.webel.io"
USER_AGENT = "ProjectTrackerDashboard/1.0 (+internal sync)"

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
COLUMNS = ["name", "client", "pm", "website", "status", "progress",
           "deadline", "invoiced", "paid", "due"]
EMPTY_MARKERS = {"", "—", "-", "–", "�", "n/a", "na", "null", "none"}


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def month_url(month: str) -> str:
    q = urllib.parse.urlencode({"month": month, "pm": "", "status": "", "website": ""})
    return f"{BASE_URL}?{q}"


def month_from_url(url: str) -> str:
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    month = (qs.get("month") or [""])[0]
    if not MONTH_RE.match(month):
        raise ValueError(f"URL has no valid ?month=YYYY-MM parameter: {url}")
    return month


def fetch(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    if "authentication-form-wrapper" in body or 'name="password"' in body:
        raise PermissionError(
            "The CRM returned its login page. The public monthly report may have "
            "been made private; a CRM login/API key is now required.")
    return body


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
class _ReportParser(HTMLParser):
    """Collects month options, summary cards and table rows."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.months: list[str] = []
        self.selected_month: str | None = None
        self._in_month_select = False
        self._select_name = None

        self.rows: list[list[dict]] = []
        self._in_tbody = False
        self._row: list[dict] | None = None
        self._cell: dict | None = None
        self._div_stack: list[str] = []

        self.cards: list[tuple[str, str, str]] = []  # (label, value, delta)
        self._card_depth = None
        self._card_buf: list[list[str]] = []
        self._card_cur: list[str] | None = None

    # -- tags --
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class", "") or ""
        if tag == "select":
            self._select_name = a.get("name")
            self._in_month_select = self._select_name == "month"
        elif tag == "option" and self._in_month_select:
            v = a.get("value", "")
            if MONTH_RE.match(v):
                self.months.append(v)
                if "selected" in a:
                    self.selected_month = v
        elif tag == "tbody":
            self._in_tbody = True
        elif tag == "tr" and self._in_tbody:
            self._row = []
        elif tag == "td" and self._row is not None:
            self._cell = {"text": [], "hrefs": [], "class": cls, "title": a.get("title")}
        elif tag == "a" and self._cell is not None:
            if a.get("href"):
                self._cell["hrefs"].append(a["href"])
        elif tag == "span" and self._cell is not None and "rounded-full" in cls:
            self._cell["badge_class"] = cls
        elif tag == "div":
            self._div_stack.append(cls)
            if "rounded-lg shadow p-4" in cls and not self._in_tbody:
                self._card_depth = len(self._div_stack)
                self._card_buf = []
            elif self._card_depth is not None:
                self._card_cur = []
                self._card_buf.append(self._card_cur)

    def handle_endtag(self, tag):
        if tag == "select":
            self._in_month_select = False
        elif tag == "tbody":
            self._in_tbody = False
        elif tag == "td" and self._cell is not None and self._row is not None:
            self._cell["text"] = " ".join(" ".join(self._cell["text"]).split())
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
        elif tag == "div":
            if self._card_depth is not None and len(self._div_stack) == self._card_depth:
                parts = [" ".join(" ".join(p).split()) for p in self._card_buf]
                parts += ["", "", ""]
                self.cards.append((parts[0], parts[1], parts[2]))
                self._card_depth = None
            self._card_cur = None
            if self._div_stack:
                self._div_stack.pop()

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"].append(data)
        elif self._card_cur is not None:
            self._card_cur.append(data)


def _clean(s: str | None) -> str | None:
    if s is None:
        return None
    s = unescape(s).strip()
    return None if s.lower() in EMPTY_MARKERS else s


def _money(s: str | None) -> float | None:
    s = _clean(s)
    if s is None:
        return None
    try:
        return float(s.replace(",", "").replace("₹", "").strip())
    except ValueError:
        return None


def _date(s: str | None) -> str | None:
    s = _clean(s)
    if s is None:
        return None
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d", "%d %b %Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _id_from_href(hrefs: list[str], kind: str) -> str | None:
    for h in hrefs:
        m = re.search(rf"/admin/{kind}/(?:view|client)/(\d+)", h)
        if m:
            return m.group(1)
    return None


def _flag_from_class(cls: str) -> str | None:
    m = re.search(r"text-(green|red|yellow|amber|gray)-\d+", cls or "")
    return m.group(1) if m else None


def parse_report(html: str) -> dict:
    p = _ReportParser()
    p.feed(html)

    summary = {}
    for label, value, delta in p.cards:
        key = re.sub(r"[^a-z]+", "_", label.lower()).strip("_")
        if not key:
            continue
        num = _money(value)
        summary[key] = {"label": label, "value": num if num is not None else value,
                        "delta": delta or None}

    projects = []
    for cells in p.rows:
        if len(cells) < len(COLUMNS):
            continue
        c = dict(zip(COLUMNS, cells))
        name = _clean(c["name"].get("title")) or _clean(c["name"]["text"])
        client = _clean(c["client"]["text"])
        pid = _id_from_href(c["name"]["hrefs"], "projects")
        key = pid or "nohref-" + hashlib.sha1(f"{name}|{client}".encode()).hexdigest()[:12]
        progress_txt = _clean(c["progress"]["text"])
        progress = None
        if progress_txt:
            m = re.search(r"(\d+(?:\.\d+)?)\s*%", progress_txt)
            progress = float(m.group(1)) if m else None
        raw = {k: c[k]["text"] for k in COLUMNS}
        raw["name"] = c["name"].get("title") or c["name"]["text"]
        rec = {
            "id": key,
            "crm_project_id": pid,
            "crm_project_url": (CRM_ORIGIN + c["name"]["hrefs"][0]) if c["name"]["hrefs"] else None,
            "name": name,
            "client": client,
            "crm_client_id": _id_from_href(c["client"]["hrefs"], "clients"),
            "pm": _clean(c["pm"]["text"]),
            "website_assignee": _clean(c["website"]["text"]),
            "website_flag": _flag_from_class(c["website"]["class"]),
            "status": _clean(c["status"]["text"]),
            "progress": progress,
            "deadline": _date(c["deadline"]["text"]),
            "invoiced": _money(c["invoiced"]["text"]),
            "paid": _money(c["paid"]["text"]),
            "due": _money(c["due"]["text"]),
            "raw": raw,  # original CRM text, preserved verbatim
        }
        missing = [f for f in ("name", "client", "pm", "website_assignee", "status",
                               "progress", "deadline", "invoiced", "paid", "due")
                   if rec[f] is None]
        if rec["deadline"] is None and _clean(c["deadline"]["text"]):
            missing.append("deadline_unparseable")
        rec["missing_fields"] = missing
        projects.append(rec)

    return {"months_available": sorted(set(p.months)),
            "selected_month": p.selected_month,
            "summary": summary,
            "projects": projects}


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SNAPSHOT_FIELDS = ["name", "client", "pm", "website_assignee", "website_flag", "status",
                   "progress", "deadline", "invoiced", "paid", "due", "missing_fields", "raw"]


def merge_month(store: dict, month: str, parsed: dict, url: str) -> dict:
    """Merge one parsed month into the store. Returns a change summary."""
    changes = {"month": month, "new_projects": [], "status_changes": [], "rows": 0}
    seen_ids = []
    for rec in parsed["projects"]:
        pid = rec["id"]
        if pid in seen_ids:          # duplicate row within the same month page
            continue
        seen_ids.append(pid)
        snap = {k: rec[k] for k in SNAPSHOT_FIELDS}
        proj = store["projects"].get(pid)
        if proj is None:
            proj = {"id": pid, "crm_project_id": rec["crm_project_id"],
                    "crm_project_url": rec["crm_project_url"],
                    "crm_client_id": rec["crm_client_id"],
                    "first_seen": _now(), "history": {}}
            store["projects"][pid] = proj
            changes["new_projects"].append(pid)
        prev = proj["history"].get(month)
        if prev and prev.get("status") != snap["status"]:
            changes["status_changes"].append(
                {"id": pid, "name": snap["name"], "from": prev.get("status"), "to": snap["status"]})
        proj["history"][month] = snap
        # "latest" = snapshot from the most recent month the project appears in
        latest_month = max(proj["history"])
        proj["latest_month"] = latest_month
        proj["first_month"] = min(proj["history"])
        proj["latest"] = proj["history"][latest_month]
        proj["crm_client_id"] = proj.get("crm_client_id") or rec["crm_client_id"]
    store["months"][month] = {"url": url, "fetched_at": _now(),
                              "summary": parsed["summary"], "project_ids": seen_ids}
    changes["rows"] = len(seen_ids)
    return changes


def sync(months: list[str] | None = None, urls: list[str] | None = None,
         log=print, user: str | None = None) -> dict:
    """Fetch months from the CRM and merge them into the database.
    `user` is the email of whoever started the sync (None = automatic)."""
    store = db.load_store(include_log=False)
    store["source"] = BASE_URL
    targets: dict[str, str] = {}
    for u in urls or []:
        targets[month_from_url(u)] = u
    for m in months or []:
        if not MONTH_RE.match(m):
            raise ValueError(f"Invalid month '{m}', expected YYYY-MM")
        targets.setdefault(m, month_url(m))

    errors, results = [], []
    discover = not targets
    if discover:
        # Fetch the default page to discover every month the CRM offers.
        first = parse_report(fetch(BASE_URL))
        available = first["months_available"]
        store["months_available"] = available
        targets = {m: month_url(m) for m in available}

    for month in sorted(targets):
        url = targets[month]
        try:
            parsed = parse_report(fetch(url))
            if parsed["months_available"]:
                store["months_available"] = sorted(
                    set(store.get("months_available", [])) | set(parsed["months_available"]))
            ch = merge_month(store, month, parsed, url)
            results.append(ch)
            log(f"  {month}: {ch['rows']} projects "
                f"({len(ch['new_projects'])} new, {len(ch['status_changes'])} status changes)")
        except Exception as e:  # keep going; report per-month failures
            errors.append({"month": month, "error": str(e)})
            log(f"  {month}: ERROR {e}")

    store["last_synced"] = _now()
    entry = {"at": store["last_synced"], "months": [r["month"] for r in results],
             "new_projects": sum(len(r["new_projects"]) for r in results),
             "status_changes": [c | {"month": r["month"]} for r in results for c in r["status_changes"]],
             "errors": errors}
    db.save_store(store, sync_entry=entry, triggered_by=user or "auto")
    return entry


if __name__ == "__main__":
    args = sys.argv[1:]
    urls, months = [], []
    i = 0
    while i < len(args):
        if args[i] == "--url" and i + 1 < len(args):
            urls.append(args[i + 1]); i += 2
        elif args[i].startswith("http"):
            urls.append(args[i]); i += 1
        else:
            months.append(args[i]); i += 1
    db.init()
    print("Syncing from", BASE_URL)
    result = sync(months, urls, user="command line")
    print(f"Done. {result['new_projects']} new projects, "
          f"{len(result['status_changes'])} status changes, {len(result['errors'])} errors.")
    print("Database:", db.DB_PATH)
