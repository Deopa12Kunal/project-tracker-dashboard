"use strict";
/* Project Status, AMC & Domain Tracker
 * CRM data (read-only)  -> state.crm      (database, written only by the CRM sync)
 * Manual tracking data  -> state.tracking (database, edited in this portal)
 * Change log            -> state.changes  (who changed what, when)
 * Every number on screen is computed here from those sources.
 */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const CATS = {
  finished:    { label: "Finished",    color: "var(--c-finished)" },
  in_progress: { label: "In Progress", color: "var(--c-progress)" },
  delayed:     { label: "Delayed",     color: "var(--c-delayed)" },
  cancelled:   { label: "Cancelled",   color: "var(--c-cancelled)" },
  unknown:     { label: "No status",   color: "var(--muted)" },
};
const AMC_LABEL = { not_pitched: "Not pitched", pitched: "Pitched", received: "Received", declined: "Declined" };
const REN_LABEL = { not_due: "Not due", pending: "Pending", renewed: "Renewed", lost: "Not renewing" };
const FIELD_LABEL = { name: "Project name", client: "Client", pm: "Project manager", website_assignee: "Website assignee",
  status: "Status", progress: "Progress", deadline: "Deadline", invoiced: "Invoiced", paid: "Paid", due: "Due",
  deadline_unparseable: "Deadline (unreadable)" };
const CHANGE_FIELD = {
  "amc.applies": "AMC applies", "amc.status": "AMC pitch / payment status", "amc.pitched_date": "AMC pitched on",
  "amc.quoted_amount": "AMC amount", "amc.received_amount": "AMC amount received", "amc.received_date": "AMC received on",
  "amc.period_end": "AMC period ends", "amc.notes": "AMC notes",
  "renewal.status": "Renewal status", "renewal.due_date": "Renewal due", "renewal.renewed_date": "Renewed on",
  "renewal.amount": "Renewal amount", "renewal.notes": "Renewal notes",
  "domain.name": "Domain name", "domain.ownership": "Domain ownership", "domain.registrar": "Domain registrar",
  "domain.expiry_date": "Domain expiry date", "domain.auto_renew": "Domain auto-renew", "domain.notes": "Domain notes",
  notes: "Notes",
};

const state = {
  crm: null, tracking: null, user: null, changes: [],
  f: { month: "", pm: "", search: "" },
  tab: "projects",
  projCat: "", amcChip: "", domChip: "", qualField: "",
  sort: {},
  drawerId: null,
};

/* ------------------------------------------------------------------ utils */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const todayISO = () => new Date().toISOString().slice(0, 10);
const daysBetween = (a, b) => Math.round((Date.parse(b) - Date.parse(a)) / 86400000);
const monthLabel = (m) => { if (!m) return ""; const [y, mo] = m.split("-"); return new Date(+y, +mo - 1, 1).toLocaleString("en", { month: "short", year: "numeric" }); };
const monthShort = (m) => { const [y, mo] = m.split("-"); return new Date(+y, +mo - 1, 1).toLocaleString("en", { month: "short" }) + " ’" + y.slice(2); };
const addMonths = (ym, n) => { const [y, mo] = ym.split("-").map(Number); return new Date(Date.UTC(y, mo - 1 + n, 1)).toISOString().slice(0, 10); };
const monthEnd = (m) => { const [y, mo] = m.split("-").map(Number); return new Date(Date.UTC(y, mo, 0)).toISOString().slice(0, 10); };
const fmtDate = (d) => d ? new Date(d + "T00:00:00").toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" }) : "";
const cur = () => state.tracking?.settings?.currency ?? "₹";
const fmtMoney = (n) => n == null ? "" : cur() + Number(n).toLocaleString("en-IN", { maximumFractionDigits: 2, minimumFractionDigits: 0 });
const fmtNum = (n) => Number(n).toLocaleString("en-IN");
const MISSING = '<span class="missing" title="Not provided in the CRM">Missing</span>';
const orMissing = (v, f = (x) => esc(x)) => (v == null || v === "" ? MISSING : f(v));
const statusPill = (kind, icon, label) => `<span class="status ${kind}"><i aria-hidden="true">${icon}</i>${esc(label)}</span>`;

function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (isError ? " error" : ""); t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), isError ? 6000 : 3000);
}
async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (res.status === 401) { location.href = "/login.html"; throw new Error("Signed out. Please sign in again."); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

/* ------------------------------------------------------- classification */
function classify(snap, month) {
  const s = state.tracking.settings;
  const today = todayISO();
  const st = snap.status;
  if (!st) return { cat: "unknown", reason: "Status missing in CRM" };
  if (st === "Finished") return { cat: "finished", reason: "" };
  if (st === "Cancelled") return { cat: "cancelled", reason: "" };
  if ((s.delayed_statuses || []).includes(st)) return { cat: "delayed", reason: `Status: ${st}` };
  if (snap.deadline && snap.deadline < today)
    return { cat: "delayed", reason: `Deadline passed ${daysBetween(snap.deadline, today)}d ago` };
  if (!snap.deadline && month) {
    const end = monthEnd(month);
    const over = daysBetween(end, today) - (s.no_deadline_grace_days ?? 30);
    if (over > 0) return { cat: "delayed", reason: `No deadline · open since ${monthLabel(month)}` };
  }
  return { cat: "in_progress", reason: st !== "In Progress" ? st : "" };
}

/* AMC schedule. Start = first month the project appears in the CRM.
 * Due at start + amc_due_months; automatically "under AMC" after amc_grace_months more.
 * Only website projects (a website assignee in the CRM), not cancelled, unless overridden. */
const AMC_STAGE = {
  under:    ["critical", "!", "Under AMC · unpaid", 0],
  due:      ["warning", "!", "AMC due", 1],
  upcoming: ["neutral", "–", "Not due yet", 2],
  paid:     ["good", "✓", "AMC received", 3],
  declined: ["neutral", "✕", "Declined", 4],
  na:       null,
};
function amcInfo(id) {
  const p = state.crm.projects[id];
  const a = state.tracking.projects[id]?.amc || {};
  const s = state.tracking.settings;
  const months = Object.keys(p.history).sort();
  const start = months[0];
  const isWeb = months.some((m) => p.history[m].website_assignee);
  const cancelled = p.latest.status === "Cancelled";
  const applies = a.applies === "yes" ? true : a.applies === "no" ? false : isWeb && !cancelled;
  const dueDate = addMonths(start, s.amc_due_months);
  const underDate = addMonths(start, s.amc_due_months + s.amc_grace_months);
  const periodEnd = addMonths(underDate.slice(0, 7), 12);
  const amount = a.quoted_amount ?? s.amc_default_amount;
  const today = todayISO();
  let stage = "na";
  if (applies) {
    if (a.status === "received" || a.received_amount > 0) stage = "paid";
    else if (a.status === "declined") stage = "declined";
    else if (today < dueDate) stage = "upcoming";
    else if (today < underDate) stage = "due";
    else stage = "under";
  }
  const why = a.applies === "yes" ? "Set to Yes manually" : a.applies === "no" ? "Set to No manually"
    : cancelled ? "Cancelled in CRM" : isWeb ? "Website project" : "Not a website project (no website assignee in CRM)";
  return { applies, isWeb, start, dueDate, underDate, periodEnd, amount, stage, why };
}
function amcStagePill(info) {
  const m = AMC_STAGE[info.stage];
  return m ? statusPill(m[0], m[1], m[2]) : '<span class="meta">No AMC</span>';
}
const amcStageLabel = (info) => AMC_STAGE[info.stage]?.[2] || "No AMC";
const amcStageSort = (info) => AMC_STAGE[info.stage]?.[3] ?? 9;

function domainState(dom) {
  if (dom?.ownership === "client") return { key: "client", days: dom.expiry_date ? daysBetween(todayISO(), dom.expiry_date) : null };
  if (!dom || !dom.expiry_date) return { key: "missing", days: null };
  const days = daysBetween(todayISO(), dom.expiry_date);
  if (days < 0) return { key: "expired", days };
  if (days <= state.tracking.settings.domain_warning_days) return { key: "expiring", days };
  return { key: "ok", days };
}
function domainPill(ds) {
  if (ds.key === "expired") return statusPill("critical", "!", `Expired ${-ds.days}d ago`);
  if (ds.key === "expiring") return statusPill("warning", "!", ds.days === 0 ? "Expires today" : `Expires in ${ds.days}d`);
  if (ds.key === "ok") return statusPill("good", "✓", `${ds.days}d left`);
  if (ds.key === "client") return statusPill("neutral", "–", "With client · not tracked");
  return '<span class="missing">Not recorded</span>';
}
function amcPill(s) {
  if (!s) return '<span class="meta">—</span>';
  const map = { received: ["good", "✓"], pitched: ["warning", "…"], declined: ["critical", "✕"], not_pitched: ["neutral", "–"] };
  return statusPill(map[s][0], map[s][1], AMC_LABEL[s]);
}
function renPill(s) {
  if (!s) return '<span class="meta">—</span>';
  const map = { renewed: ["good", "✓"], pending: ["warning", "…"], lost: ["critical", "✕"], not_due: ["neutral", "–"] };
  return statusPill(map[s][0], map[s][1], REN_LABEL[s]);
}
function catBadge(v) {
  const c = CATS[v.cat];
  return `<span class="badge"><span class="swatch" style="background:${c.color}"></span>${c.label}</span>` +
    (v.reason ? `<div class="reason">${esc(v.reason)}</div>` : "");
}

/* --------------------------------------------------------------- dataset */
function buildRows({ ignoreMonth = false } = {}) {
  const { crm, tracking, f } = state;
  const out = [];
  const add = (p, month, snap) => {
    const t = tracking.projects[p.id] || {};
    const cls = classify(snap, month);
    const ds = domainState(t.domain);
    out.push({ id: p.id, month, crmUrl: p.crm_project_url, ...snap, ...cls, t, ds, amc: amcInfo(p.id) });
  };
  if (f.month && !ignoreMonth) {
    for (const id of crm.months[f.month]?.project_ids || []) {
      const p = crm.projects[id]; if (p?.history[f.month]) add(p, f.month, p.history[f.month]);
    }
  } else {
    for (const p of Object.values(crm.projects)) add(p, p.latest_month, p.latest);
  }
  const q = f.search.trim().toLowerCase();
  return out.filter((r) =>
    (!f.pm || (f.pm === "__none" ? !r.pm : r.pm === f.pm)) &&
    (!q || [r.name, r.client, r.pm, r.website_assignee, r.status, r.t.domain?.name, r.t.domain?.registrar]
      .some((x) => x && String(x).toLowerCase().includes(q))));
}

function metrics(rows) {
  const m = { total: rows.length, finished: 0, in_progress: 0, delayed: 0, cancelled: 0, unknown: 0,
    amcPitched: 0, amcReceived: 0, amcDeclined: 0, amcAwaiting: 0, amcAmount: 0, amcQuotedOpen: 0,
    renRenewed: 0, renPending: 0, renLost: 0, renAmount: 0,
    amcApplies: 0, amcDue: 0, amcDueAmt: 0, amcUnder: 0, amcUnderAmt: 0, amcUpcoming: 0,
    domExpiring: 0, domExpired: 0, domOk: 0, domMissing: 0, domClient: 0, invoiced: 0, paid: 0, due: 0 };
  for (const r of rows) {
    m[r.cat]++;
    if (r.amc.applies) m.amcApplies++;
    if (r.amc.stage === "due") { m.amcDue++; m.amcDueAmt += r.amc.amount || 0; }
    if (r.amc.stage === "under") { m.amcUnder++; m.amcUnderAmt += r.amc.amount || 0; }
    if (r.amc.stage === "upcoming") m.amcUpcoming++;
    if (r.ds.key === "client") m.domClient++;
    const a = r.t.amc || {}, rn = r.t.renewal || {};
    if (["pitched", "received", "declined"].includes(a.status)) m.amcPitched++;
    if (a.status === "received") m.amcReceived++;
    if (a.status === "declined") m.amcDeclined++;
    if (a.status === "pitched") { m.amcAwaiting++; m.amcQuotedOpen += a.quoted_amount || 0; }
    m.amcAmount += a.received_amount || 0;
    if (rn.status === "renewed") { m.renRenewed++; m.renAmount += rn.amount || 0; }
    if (rn.status === "pending") m.renPending++;
    if (rn.status === "lost") m.renLost++;
    if (r.ds.key === "expiring") m.domExpiring++;
    if (r.ds.key === "expired") m.domExpired++;
    if (r.ds.key === "ok") m.domOk++;
    if (r.ds.key === "missing") m.domMissing++;
    m.invoiced += r.invoiced || 0; m.paid += r.paid || 0; m.due += r.due || 0;
  }
  return m;
}

/* ----------------------------------------------------------------- cards */
function renderCards(rows) {
  const m = metrics(rows);
  const pct = (a, b) => (b ? Math.round((a / b) * 100) + "%" : "–");
  const sw = (c) => `<span class="swatch" style="background:${CATS[c].color}"></span>`;
  const warnDays = state.tracking.settings.domain_warning_days;
  const renTotal = m.renRenewed + m.renPending + m.renLost;
  const cards = [
    { group: "Projects (CRM)" },
    { id: "total", label: "Total projects", value: fmtNum(m.total),
      detail: `${m.cancelled} cancelled${m.unknown ? ` · ${m.unknown} without status` : ""}`,
      bar: [["finished", m.finished], ["in_progress", m.in_progress], ["delayed", m.delayed], ["cancelled", m.cancelled]], go: ["projects", ""] },
    { id: "finished", label: sw("finished") + "Finished", value: fmtNum(m.finished), detail: `${pct(m.finished, m.total)} of total`, go: ["projects", "finished"] },
    { id: "in_progress", label: sw("in_progress") + "In progress", value: fmtNum(m.in_progress), detail: `${pct(m.in_progress, m.total)} of total`, go: ["projects", "in_progress"] },
    { id: "delayed", label: sw("delayed") + "Delayed", value: fmtNum(m.delayed), detail: "Past deadline, stalled, or long open", go: ["projects", "delayed"] },
    { group: "AMC & renewals" },
    { id: "amcUnder", label: statusPill("critical", "!", "") + "Under AMC, unpaid", value: fmtNum(m.amcUnder),
      detail: `${fmtMoney(m.amcUnderAmt)} outstanding · over ${state.tracking.settings.amc_due_months + state.tracking.settings.amc_grace_months} months old`, go: ["amc", "st_under"] },
    { id: "amcDue", label: statusPill("warning", "!", "") + "AMC due", value: fmtNum(m.amcDue),
      detail: `${fmtMoney(m.amcDueAmt)} · ${m.amcUpcoming} more not due yet`, go: ["amc", "st_due"] },
    { id: "amcPitched", label: "AMC pitched", value: fmtNum(m.amcPitched), detail: `${m.amcAwaiting} awaiting · ${m.amcDeclined} declined`, go: ["amc", "pitched_any"] },
    { id: "amcReceived", label: "AMC received", value: fmtNum(m.amcReceived), detail: `${pct(m.amcReceived, m.amcPitched)} of pitched converted`, go: ["amc", "st_paid"] },
    { id: "amcAmount", label: "AMC amount received", value: fmtMoney(m.amcAmount), detail: m.amcQuotedOpen ? `${fmtMoney(m.amcQuotedOpen)} quoted, awaiting` : "From recorded payments", go: ["amc", "st_paid"] },
    { id: "renewals", label: "Website AMC renewals", value: `${fmtNum(m.renRenewed)} <span class="detail">renewed</span>`,
      detail: `${m.renPending} pending${m.renLost ? ` · ${m.renLost} not renewing` : ""}${m.renAmount ? ` · ${fmtMoney(m.renAmount)}` : ""}`,
      barRaw: renTotal ? [["var(--good)", m.renRenewed], ["var(--warning)", m.renPending], ["var(--critical)", m.renLost]] : null, go: ["amc", "ren_pending"] },
    { group: "Domains (with Webelio)" },
    { id: "domExpiring", label: statusPill("warning", "!", "") + "Domains about to expire", value: fmtNum(m.domExpiring), detail: `Within ${warnDays} days`, go: ["domains", "expiring"] },
    { id: "domExpired", label: statusPill("critical", "!", "") + "Expired domains", value: fmtNum(m.domExpired), detail: `${m.domMissing} with no expiry recorded${m.domClient ? ` · ${m.domClient} with client` : ""}`, go: ["domains", "expired"] },
  ];
  const activeKey = state.tab + ":" + ({ projects: state.projCat, amc: state.amcChip, domains: state.domChip }[state.tab] ?? "");
  $("#cards").innerHTML = cards.map((c) => {
    if (c.group) return `<div class="group-label">${c.group}</div>`;
    let bar = "";
    if (c.bar) { const t = c.bar.reduce((s, [, n]) => s + n, 0) || 1; bar = `<div class="bar">${c.bar.filter(([, n]) => n).map(([k, n]) => `<span style="width:${(n / t) * 100}%;background:${CATS[k].color}" title="${CATS[k].label}: ${n}"></span>`).join("")}</div>`; }
    if (c.barRaw) { const t = c.barRaw.reduce((s, [, n]) => s + n, 0) || 1; bar = `<div class="bar">${c.barRaw.filter(([, n]) => n).map(([col, n]) => `<span style="width:${(n / t) * 100}%;background:${col}"></span>`).join("")}</div>`; }
    const active = c.go && activeKey === c.go.join(":") && c.go[1] ? " active" : "";
    return `<button class="card${active}" data-go="${c.go.join(":")}"><span class="label">${c.label}</span><span class="value">${c.value}</span><span class="detail">${esc(c.detail)}</span>${bar}</button>`;
  }).join("");
}

/* ----------------------------------------------------------------- trend */
function renderTrend() {
  const all = buildRows({ ignoreMonth: true });
  // trend uses every month's own snapshot, so recompute per month
  const { crm, f } = state;
  const months = Object.keys(crm.months).sort();
  const ids = new Set(all.map((r) => r.id)); // respects PM + search filters
  const series = ["finished", "in_progress", "delayed"];
  const data = months.map((m) => {
    const c = { finished: 0, in_progress: 0, delayed: 0, cancelled: 0, unknown: 0 };
    for (const id of crm.months[m].project_ids) {
      if (!ids.has(id)) continue;
      const snap = crm.projects[id].history[m];
      c[classify(snap, m).cat]++;
    }
    return { m, c, total: series.reduce((s, k) => s + c[k], 0) };
  });
  const max = Math.max(1, ...data.map((d) => d.total));
  $("#trendLegend").innerHTML = series.map((k) => `<span><span class="swatch" style="background:${CATS[k].color}"></span>${CATS[k].label}</span>`).join("") + `<span class="meta">Cancelled not shown · click a month to filter</span>`;
  const H = 170;
  $("#trend").innerHTML = data.map((d) => `
    <div class="col${f.month === d.m ? " sel" : ""}" data-month="${d.m}" tabindex="0" role="button" aria-label="${monthLabel(d.m)}: ${d.total} projects">
      <div class="total">${d.total || ""}</div>
      <div class="stack">${series.map((k) => d.c[k] ? `<div class="seg" style="height:${(d.c[k] / max) * H}px;background:${CATS[k].color}"></div>` : "").join("")}</div>
    </div>`).join("");
  let axis = $("#trendAxis");
  if (!axis) { axis = document.createElement("div"); axis.id = "trendAxis"; axis.className = "trend-axis"; $("#trend").after(axis); }
  axis.innerHTML = data.map((d) => `<span>${monthShort(d.m)}</span>`).join("");
  $("#trend").onscroll = () => (axis.scrollLeft = $("#trend").scrollLeft);
  const tip = $("#tooltip");
  $$("#trend .col").forEach((col, i) => {
    const d = data[i];
    const show = (e) => {
      tip.innerHTML = `<div><b>${monthLabel(d.m)}</b></div>` + series.map((k) => `<div class="row"><span><span class="swatch" style="background:${CATS[k].color}"></span> ${CATS[k].label}</span><b>${d.c[k]}</b></div>`).join("") +
        (d.c.cancelled ? `<div class="row meta"><span>Cancelled</span><b>${d.c.cancelled}</b></div>` : "");
      tip.hidden = false;
      const r = col.getBoundingClientRect();
      const x = Math.min(window.innerWidth - tip.offsetWidth - 8, Math.max(8, r.left + r.width / 2 - tip.offsetWidth / 2));
      tip.style.left = x + "px"; tip.style.top = Math.max(8, r.top - tip.offsetHeight - 8) + "px";
    };
    col.onmouseenter = show; col.onfocus = show;
    col.onmouseleave = col.onblur = () => (tip.hidden = true);
    col.onclick = () => { setMonth(state.f.month === d.m ? "" : d.m); };
    col.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); col.click(); } };
  });
}

/* ---------------------------------------------------------------- tables */
function renderTable(el, key, columns, rows, emptyMsg) {
  const s = state.sort[key] || {};
  if (s.col != null) {
    const col = columns[s.col];
    const val = col.sort || ((r) => r[col.key]);
    rows = [...rows].sort((a, b) => {
      const x = val(a), y = val(b);
      if (x == null && y == null) return 0; if (x == null) return 1; if (y == null) return -1;
      return (x > y ? 1 : x < y ? -1 : 0) * (s.dir === "desc" ? -1 : 1);
    });
  }
  el._rows = rows; el._columns = columns;
  el.innerHTML = `<thead><tr>${columns.map((c, i) => `<th class="${c.num ? "num" : ""}" data-i="${i}" ${s.col === i ? `data-sort-dir="${s.dir}"` : ""}>${c.label}</th>`).join("")}</tr></thead>
    <tbody>${rows.length ? rows.map((r) => `<tr data-id="${esc(r.id)}">${columns.map((c) => `<td class="${c.num ? "num" : ""}">${c.render ? c.render(r) : esc(r[c.key])}</td>`).join("")}</tr>`).join("")
      : `<tr class="empty-row"><td colspan="${columns.length}">${emptyMsg}</td></tr>`}</tbody>`;
  $$("th", el).forEach((th) => th.onclick = () => {
    const i = +th.dataset.i; const cur = state.sort[key] || {};
    state.sort[key] = { col: i, dir: cur.col === i && cur.dir === "asc" ? "desc" : "asc" };
    render();
  });
  $$("tbody tr[data-id]", el).forEach((tr) => tr.onclick = (e) => { if (!e.target.closest("a")) openDrawer(tr.dataset.id); });
}

const projCell = (r) => `<span class="name" title="${esc(r.name)}">${orMissing(r.name)}</span><span class="meta">${r.client ? esc(r.client) : MISSING}</span>`;
const moneyCol = (key, label) => ({ key, label, num: true, render: (r) => orMissing(r[key], fmtMoney), csv: (r) => r[key] ?? "" });

function chipBar(el, chips, active, onPick) {
  el.innerHTML = chips.map(([k, label, n]) => `<button class="chip${active === k ? " active" : ""}" data-k="${k}">${label}<span class="n">${n}</span></button>`).join("");
  $$(".chip", el).forEach((b) => b.onclick = () => onPick(b.dataset.k));
}

function renderProjects(rows) {
  const counts = metrics(rows);
  chipBar($("#categoryChips"), [["", "All", rows.length], ...Object.entries(CATS).filter(([k]) => k !== "unknown" || counts.unknown).map(([k, c]) => [k, c.label, counts[k]])],
    state.projCat, (k) => { state.projCat = k; render(); });
  const list = state.projCat ? rows.filter((r) => r.cat === state.projCat) : rows;
  renderTable($("#projectsTable"), "projects", [
    { key: "name", label: "Project / client", render: projCell, csv: (r) => r.name },
    { key: "client", label: "Client", render: (r) => orMissing(r.client), hidden: true },
    { key: "pm", label: "PM", render: (r) => orMissing(r.pm) },
    { key: "month", label: "Month", render: (r) => esc(monthLabel(r.month)) },
    { key: "status", label: "CRM status", render: (r) => orMissing(r.status) },
    { key: "cat", label: "Category", render: catBadge, sort: (r) => CATS[r.cat].label, csv: (r) => CATS[r.cat].label + (r.reason ? ` (${r.reason})` : "") },
    { key: "progress", label: "Progress", num: true, render: (r) => r.progress == null ? MISSING : `<span class="progress"><span class="track"><span class="fill" style="width:${r.progress}%"></span></span>${r.progress}%</span>` },
    { key: "deadline", label: "Deadline", render: (r) => orMissing(r.deadline, fmtDate) },
    moneyCol("invoiced", "Invoiced"), moneyCol("paid", "Paid"), moneyCol("due", "Due"),
    { key: "amc", label: "AMC", render: (r) => amcStagePill(r.amc), sort: (r) => amcStageSort(r.amc), csv: (r) => amcStageLabel(r.amc) },
    { key: "domain", label: "Domain", render: (r) => r.t.domain?.name ? `${esc(r.t.domain.name)}<div>${domainPill(r.ds)}</div>` : '<span class="meta">—</span>', sort: (r) => r.ds.days, csv: (r) => r.t.domain?.name || "" },
  ].filter((c) => !c.hidden), list, "No projects match these filters.");
}

function renderAmc(rows) {
  const c = (fn) => rows.filter(fn).length;
  const st = (s) => (r) => r.amc.stage === s;
  const chips = [
    ["", "AMC projects", c((r) => r.amc.applies)],
    ["st_under", "Under AMC · unpaid", c(st("under"))],
    ["st_due", "AMC due", c(st("due"))],
    ["st_upcoming", "Not due yet", c(st("upcoming"))],
    ["st_paid", "AMC received", c(st("paid"))],
    ["pitched_any", "Pitched", c((r) => ["pitched", "received", "declined"].includes(r.t.amc?.status))],
    ["pitched", "Awaiting reply", c((r) => r.t.amc?.status === "pitched")],
    ["declined", "Declined", c((r) => r.t.amc?.status === "declined")],
    ["ren_renewed", "Renewed", c((r) => r.t.renewal?.status === "renewed")],
    ["ren_pending", "Renewal pending", c((r) => r.t.renewal?.status === "pending")],
    ["st_na", "No AMC", c(st("na"))],
  ];
  chipBar($("#amcChips"), chips, state.amcChip, (k) => { state.amcChip = k; render(); });
  const s = state.tracking.settings;
  $("#amcRule").textContent = `Website projects only. AMC of ${fmtMoney(s.amc_default_amount)} (incl. GST) is due ${s.amc_due_months} months after the project's first month in the CRM, and the project is automatically under AMC ${s.amc_grace_months} months after that. Click a project to change its amount or mark it as paid.`;
  const k = state.amcChip;
  const list = rows.filter((r) => {
    const a = r.t.amc?.status, rn = r.t.renewal?.status;
    if (!k) return r.amc.applies;
    if (k.startsWith("st_")) return r.amc.stage === k.slice(3);
    if (k === "pitched_any") return ["pitched", "received", "declined"].includes(a);
    if (k === "amc_none") return !a;
    if (k === "ren_renewed") return rn === "renewed";
    if (k === "ren_pending") return rn === "pending";
    return a === k;
  });
  renderTable($("#amcTable"), "amc", [
    { key: "name", label: "Project / client", render: projCell, csv: (r) => r.name },
    { key: "start", label: "Started", render: (r) => esc(monthLabel(r.amc.start)), sort: (r) => r.amc.start, csv: (r) => r.amc.start },
    { key: "adue", label: "AMC due", render: (r) => r.amc.applies ? esc(fmtDate(r.amc.dueDate)) : "", sort: (r) => r.amc.dueDate, csv: (r) => r.amc.applies ? r.amc.dueDate : "" },
    { key: "aund", label: "Under AMC from", render: (r) => r.amc.applies ? esc(fmtDate(r.amc.underDate)) : "", sort: (r) => r.amc.underDate, csv: (r) => r.amc.applies ? r.amc.underDate : "" },
    { key: "stage", label: "AMC", render: (r) => amcStagePill(r.amc), sort: (r) => amcStageSort(r.amc), csv: (r) => amcStageLabel(r.amc) },
    { key: "q", label: "Amount", num: true, render: (r) => r.amc.applies ? esc(fmtMoney(r.amc.amount)) : "", sort: (r) => r.amc.amount, csv: (r) => r.amc.applies ? r.amc.amount : "" },
    { key: "amcs", label: "Pitch status", render: (r) => amcPill(r.t.amc?.status), sort: (r) => r.t.amc?.status, csv: (r) => AMC_LABEL[r.t.amc?.status] || "" },
    { key: "ra", label: "Received", num: true, render: (r) => esc(fmtMoney(r.t.amc?.received_amount)), sort: (r) => r.t.amc?.received_amount, csv: (r) => r.t.amc?.received_amount ?? "" },
    { key: "rd", label: "Received on", render: (r) => esc(fmtDate(r.t.amc?.received_date)), sort: (r) => r.t.amc?.received_date, csv: (r) => r.t.amc?.received_date || "" },
    { key: "rs", label: "Renewal", render: (r) => renPill(r.t.renewal?.status), sort: (r) => r.t.renewal?.status, csv: (r) => REN_LABEL[r.t.renewal?.status] || "" },
    { key: "rdue", label: "Renewal due", render: (r) => esc(fmtDate(r.t.renewal?.due_date || (r.amc.applies ? r.amc.periodEnd : ""))), sort: (r) => r.t.renewal?.due_date || r.amc.periodEnd, csv: (r) => r.t.renewal?.due_date || (r.amc.applies ? r.amc.periodEnd : "") },
  ], list, k ? "Nothing in this group yet." : "No projects match these filters.");
}

function renderDomains(rows) {
  const n = (key) => rows.filter((r) => r.ds.key === key).length;
  chipBar($("#domainChips"), [["", "All", rows.length], ["expired", "Expired", n("expired")], ["expiring", `Expiring ≤ ${state.tracking.settings.domain_warning_days}d`, n("expiring")], ["ok", "OK", n("ok")], ["missing", "Not recorded", n("missing")], ["client", "With client", n("client")]],
    state.domChip, (k) => { state.domChip = k; render(); });
  const list = state.domChip ? rows.filter((r) => r.ds.key === state.domChip) : rows;
  if (state.sort.domains == null) state.sort.domains = { col: 5, dir: "asc" };
  renderTable($("#domainTable"), "domains", [
    { key: "dn", label: "Domain", render: (r) => r.t.domain?.name ? `<b>${esc(r.t.domain.name)}</b>` : '<span class="missing">No domain</span>', sort: (r) => r.t.domain?.name, csv: (r) => r.t.domain?.name || "" },
    { key: "name", label: "Project / client", render: projCell, csv: (r) => r.name },
    { key: "own", label: "Ownership", render: (r) => r.t.domain?.name || r.t.domain?.expiry_date ? (r.t.domain?.ownership === "client" ? "With client" : "With Webelio") : "", sort: (r) => r.t.domain?.ownership, csv: (r) => r.t.domain?.ownership === "client" ? "With client" : r.t.domain?.name ? "With Webelio" : "" },
    { key: "reg", label: "Registrar", render: (r) => esc(r.t.domain?.registrar || ""), sort: (r) => r.t.domain?.registrar, csv: (r) => r.t.domain?.registrar || "" },
    { key: "exp", label: "Expiry date", render: (r) => esc(fmtDate(r.t.domain?.expiry_date)), sort: (r) => r.t.domain?.expiry_date, csv: (r) => r.t.domain?.expiry_date || "" },
    { key: "ds", label: "Status", render: (r) => domainPill(r.ds), sort: (r) => r.ds.days ?? 1e9, csv: (r) => r.ds.key },
    { key: "ar", label: "Auto-renew", render: (r) => r.t.domain?.expiry_date ? (r.t.domain?.auto_renew ? "Yes" : "No") : "", sort: (r) => r.t.domain?.auto_renew ? 1 : 0, csv: (r) => r.t.domain?.auto_renew ? "yes" : "no" },
  ], list, "No projects in this group.");
}

function renderQuality(rows) {
  const counts = {};
  for (const r of rows) for (const fld of r.missing_fields || []) counts[fld] = (counts[fld] || 0) + 1;
  const withAny = rows.filter((r) => (r.missing_fields || []).length).length;
  $("#qualityGrid").innerHTML =
    `<button class="qcard${!state.qualField ? " active" : ""}" data-f=""><div class="value">${withAny}</div><div class="label">Projects with missing CRM data (of ${rows.length})</div></button>` +
    Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([k, n]) =>
      `<button class="qcard${state.qualField === k ? " active" : ""}" data-f="${k}"><div class="value">${n}</div><div class="label">Missing ${esc(FIELD_LABEL[k] || k)}</div></button>`).join("");
  $$("#qualityGrid .qcard").forEach((b) => b.onclick = () => { state.qualField = b.dataset.f; render(); });
  const list = rows.filter((r) => state.qualField ? (r.missing_fields || []).includes(state.qualField) : (r.missing_fields || []).length);
  renderTable($("#qualityTable"), "quality", [
    { key: "name", label: "Project / client", render: projCell },
    { key: "month", label: "Month", render: (r) => esc(monthLabel(r.month)) },
    { key: "pm", label: "PM", render: (r) => orMissing(r.pm) },
    { key: "missing_fields", label: "Missing fields", render: (r) => r.missing_fields.map((f) => `<span class="missing">${esc(FIELD_LABEL[f] || f)}</span>`).join(" "), sort: (r) => r.missing_fields.length },
    { key: "crm", label: "Fix in CRM", render: (r) => r.crmUrl ? `<a href="${esc(r.crmUrl)}" target="_blank" rel="noopener">Open ↗</a>` : "" },
  ], list, "No missing CRM data for these filters.");
}

/* -------------------------------------------------------------- activity */
const canEdit = () => state.user?.role === "editor";
const whoLabel = (email, name) => email === "import" ? "Imported from old files" : email === "auto" ? "Automatic sync"
  : email === "local" ? "Local mode" : (name || email || "—");
const changeFieldLabel = (f) => CHANGE_FIELD[f] || (f.startsWith("settings.") ? "Rule: " + f.slice(9).replace(/_/g, " ") : f);
function changeValue(field, v) {
  if (v == null) return field === "amc.applies" ? "Automatic" : '<span class="meta">(empty)</span>';
  const key = field.split(".").pop();
  if (field === "amc.status") return esc(AMC_LABEL[v] || v);
  if (field === "renewal.status") return esc(REN_LABEL[v] || v);
  if (field === "amc.applies") return esc({ yes: "Yes", no: "No" }[v] || v);
  if (field === "domain.ownership") return esc({ webelio: "With Webelio", client: "With client" }[v] || v);
  if (/_date$|period_end$/.test(key)) return esc(fmtDate(v));
  if (/amount$/.test(key)) return esc(fmtMoney(Number(v)));
  return esc(v);
}
const changeProject = (c) => c.project_id ? state.crm.projects[c.project_id]?.latest?.name || c.project_id : "Dashboard rules";
const changeColumns = (withProject) => [
  { key: "at", label: "When", render: (c) => esc(new Date(c.at).toLocaleString()), csv: (c) => c.at },
  { key: "who", label: "Who", render: (c) => esc(whoLabel(c.user_email, c.user_name)), sort: (c) => whoLabel(c.user_email, c.user_name), csv: (c) => c.user_email },
  ...(withProject ? [{ key: "proj", label: "Project", render: (c) => `<span class="name">${esc(changeProject(c))}</span>`, sort: changeProject, csv: changeProject }] : []),
  { key: "field", label: "What changed", render: (c) => esc(changeFieldLabel(c.field)), sort: (c) => changeFieldLabel(c.field), csv: (c) => changeFieldLabel(c.field) },
  { key: "old_value", label: "From", render: (c) => changeValue(c.field, c.old_value), csv: (c) => c.old_value ?? "" },
  { key: "new_value", label: "To", render: (c) => changeValue(c.field, c.new_value), csv: (c) => c.new_value ?? "" },
];
function renderActivity() {
  const q = state.f.search.trim().toLowerCase();
  const list = state.changes.map((c) => ({ ...c, id: c.project_id || "" })).filter((c) => !q ||
    [whoLabel(c.user_email, c.user_name), c.user_email, changeProject(c), changeFieldLabel(c.field), c.old_value, c.new_value]
      .some((x) => x && String(x).toLowerCase().includes(q)));
  renderTable($("#activityTable"), "activity", changeColumns(true), list,
    q ? "No changes match this search." : "No changes yet. Open a project and save its AMC, renewal or domain details to see them here.");
}
async function loadChanges() {
  try { state.changes = (await api("/api/changes?limit=500")).changes; } catch (err) { toast(err.message, true); }
}
async function loadProjectChanges(id) {
  const el = $("#projectChanges"); el.innerHTML = "";
  try {
    const { changes } = await api(`/api/changes?project=${encodeURIComponent(id)}&limit=200`);
    if (state.drawerId !== id) return;
    if (!changes.length) { el.innerHTML = `<h3>Change history</h3><p class="note">No changes recorded yet.</p>`; return; }
    el.innerHTML = `<h3>Change history</h3><div class="table-wrap"><table id="projectChangesTable"></table></div>`;
    renderTable($("#projectChangesTable"), "projectChanges", changeColumns(false), changes.map((c) => ({ ...c, id: "" })), "");
    $$("#projectChangesTable tbody tr, #projectChangesTable th").forEach((el) => { el.onclick = null; el.style.cursor = "default"; });
  } catch (err) { el.innerHTML = `<p class="note">Couldn't load change history: ${esc(err.message)}</p>`; }
}

function renderUser() {
  const u = state.user; if (!u) return;
  const chip = $("#userChip");
  chip.hidden = false;
  chip.textContent = u.name + (u.role === "viewer" ? " · view only" : "");
  chip.title = u.email === "local" ? "Login is off because Google sign-in isn't configured" : u.email;
  $("#logoutBtn").hidden = !state.authEnabled;
  const ro = !canEdit();
  $("#syncBtn").hidden = ro;
  $("#readonlyNote").hidden = !ro;
  $$("#trackForm fieldset, #trackForm button, #settingsForm button, #syncForm button, #syncForm textarea").forEach((el) => (el.disabled = ro));
}

function renderSettings() {
  const s = state.tracking.settings;
  const form = $("#settingsForm");
  if (!form._filled) {
    form.domain_warning_days.value = s.domain_warning_days;
    form.no_deadline_grace_days.value = s.no_deadline_grace_days;
    form.currency.value = s.currency;
    form.amc_default_amount.value = s.amc_default_amount;
    form.amc_due_months.value = s.amc_due_months;
    form.amc_grace_months.value = s.amc_grace_months;
    const statuses = [...new Set(Object.values(state.crm.projects).flatMap((p) => Object.values(p.history).map((h) => h.status)).filter(Boolean))]
      .filter((x) => !["Finished", "Cancelled"].includes(x)).sort();
    $("#delayedStatuses").innerHTML = statuses.map((x) => `<label><input type="checkbox" value="${esc(x)}" ${s.delayed_statuses.includes(x) ? "checked" : ""}> ${esc(x)}</label>`).join("");
    form._filled = true;
  }
  const log = state.crm.sync_log || [];
  renderTable($("#syncLogTable"), "synclog", [
    { key: "at", label: "When", render: (r) => esc(new Date(r.at).toLocaleString()) },
    { key: "by", label: "By", render: (r) => esc(whoLabel(r.triggered_by, r.triggered_by)) },
    { key: "months", label: "Months", render: (r) => esc(r.months.length > 4 ? `${r.months.length} months (${monthShort(r.months[0])} – ${monthShort(r.months.at(-1))})` : r.months.map(monthShort).join(", ")) },
    { key: "new_projects", label: "New projects", num: true },
    { key: "sc", label: "Status changes", render: (r) => r.status_changes.length ? r.status_changes.slice(0, 5).map((c) => `${esc(c.name)}: ${esc(c.from)} → ${esc(c.to)}`).join("<br>") + (r.status_changes.length > 5 ? `<br>+${r.status_changes.length - 5} more` : "") : "0" },
    { key: "errors", label: "Errors", render: (r) => r.errors.length ? r.errors.map((e) => `<span class="missing">${esc(e.month)}: ${esc(e.error)}</span>`).join("<br>") : "None" },
  ], log.map((l, i) => ({ ...l, id: "log" + i })), "No syncs yet.");
  $$("#syncLogTable tbody tr").forEach((tr) => tr.onclick = null);
}

/* ---------------------------------------------------------------- render */
function render() {
  const rows = buildRows();
  const { crm, f } = state;
  const last = crm.last_synced ? new Date(crm.last_synced).toLocaleString() : "never";
  $("#syncInfo").textContent = `CRM: ${Object.keys(crm.projects).length} projects across ${Object.keys(crm.months).length} months · last synced ${last}` +
    (f.month ? ` · showing ${monthLabel(f.month)}` : "");
  renderCards(rows);
  renderTrend();
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === state.tab));
  $$(".tabpanel").forEach((p) => (p.hidden = p.id !== "tab-" + state.tab));
  if (state.tab === "projects") renderProjects(rows);
  if (state.tab === "amc") renderAmc(rows);
  if (state.tab === "domains") renderDomains(rows);
  if (state.tab === "activity") renderActivity();
  if (state.tab === "quality") renderQuality(rows);
  if (state.tab === "settings") renderSettings();
  try { localStorage.setItem("ptd.view", JSON.stringify({ f: state.f, tab: state.tab })); } catch {}
}

function fillFilters() {
  const months = Object.keys(state.crm.months).sort().reverse();
  $("#fMonth").innerHTML = `<option value="">All months (${Object.keys(state.crm.projects).length})</option>` +
    months.map((m) => `<option value="${m}">${monthLabel(m)} (${state.crm.months[m].project_ids.length})</option>`).join("");
  const pms = [...new Set(Object.values(state.crm.projects).map((p) => p.latest.pm).filter(Boolean))].sort();
  $("#fPm").innerHTML = `<option value="">All PMs</option>` + pms.map((p) => `<option>${esc(p)}</option>`).join("") + `<option value="__none">PM missing</option>`;
  $("#fMonth").value = state.f.month; $("#fPm").value = state.f.pm; $("#fSearch").value = state.f.search;
}
function setMonth(m) { state.f.month = m; $("#fMonth").value = m; render(); }

/* ---------------------------------------------------------------- drawer */
function openDrawer(id) {
  const p = state.crm.projects[id]; if (!p) return;
  state.drawerId = id;
  const month = state.f.month && p.history[state.f.month] ? state.f.month : p.latest_month;
  const snap = p.history[month];
  const cls = classify(snap, month);
  $("#drawerTitle").textContent = snap.name || "(no project name in CRM)";
  $("#drawerSub").innerHTML = `${esc(snap.client || "Client missing")} · ${esc(monthLabel(month))}` + (p.crm_project_url ? ` · <a href="${esc(p.crm_project_url)}" target="_blank" rel="noopener">Open in CRM ↗</a>` : "");
  const rows = [
    ["Status", orMissing(snap.status)], ["Category", catBadge({ ...cls })], ["Project manager", orMissing(snap.pm)],
    ["Website assignee", orMissing(snap.website_assignee)], ["Progress", orMissing(snap.progress, (v) => v + "%")],
    ["Deadline", orMissing(snap.deadline, fmtDate)], ["Invoiced", orMissing(snap.invoiced, fmtMoney)],
    ["Paid", orMissing(snap.paid, fmtMoney)], ["Due", orMissing(snap.due, fmtMoney)], ["CRM project ID", esc(p.crm_project_id || "—")],
  ];
  $("#crmFields").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
  const hist = Object.entries(p.history).sort((a, b) => a[0].localeCompare(b[0]));
  $("#crmHistory").innerHTML = hist.length > 1 ? `<h3>Monthly history</h3><div class="table-wrap"><table><thead><tr><th>Month</th><th>Status</th><th class="num">Progress</th><th class="num">Paid</th><th class="num">Due</th></tr></thead><tbody>${hist.map(([m, h]) => `<tr><td>${monthLabel(m)}</td><td>${esc(h.status || "")}</td><td class="num">${h.progress ?? ""}%</td><td class="num">${fmtMoney(h.paid)}</td><td class="num">${fmtMoney(h.due)}</td></tr>`).join("")}</tbody></table></div>` : "";
  const t = state.tracking.projects[id] || {};
  const ai = amcInfo(id);
  $("#amcSchedule").innerHTML = ai.applies
    ? `${esc(ai.why)} · started ${esc(monthLabel(ai.start))} → AMC due <b>${esc(fmtDate(ai.dueDate))}</b> → under AMC from <b>${esc(fmtDate(ai.underDate))}</b>. Now: ${amcStagePill(ai)}`
    : `No AMC: ${esc(ai.why)}. Set “AMC applies” to Yes to include it.`;
  // Pre-filled values; anything saved for this project takes priority.
  const defaults = { "domain.ownership": "webelio" };
  if (ai.applies) Object.assign(defaults, {
    "amc.quoted_amount": state.tracking.settings.amc_default_amount,
    "amc.period_end": ai.periodEnd, "renewal.due_date": ai.periodEnd });
  const form = $("#trackForm");
  for (const el of form.elements) {
    if (!el.name) continue;
    const [sec, key] = el.name.split(".");
    const v = t[sec]?.[key] ?? defaults[el.name];
    if (el.type === "checkbox") el.checked = !!v; else el.value = v ?? "";
  }
  $("#trackMeta").textContent = t.updated_at
    ? `Last updated ${new Date(t.updated_at).toLocaleString()} by ${whoLabel(t.updated_by, t.updated_by_name)}`
    : "No manual data yet";
  loadProjectChanges(id);
  $("#drawer").classList.add("open"); $("#drawer").setAttribute("aria-hidden", "false");
  $("#drawerBackdrop").hidden = false;
  setTimeout(() => $("#drawerClose").focus(), 50);
}
function closeDrawer() {
  $("#drawer").classList.remove("open"); $("#drawer").setAttribute("aria-hidden", "true");
  $("#drawerBackdrop").hidden = true; state.drawerId = null;
}

/* ----------------------------------------------------------------- CSV */
function exportCsv(tableId, name) {
  const el = $(tableId); const cols = el._columns; const rows = el._rows || [];
  const plain = (html) => { const d = document.createElement("div"); d.innerHTML = html; return d.textContent.trim(); };
  const cell = (c, r) => c.csv ? c.csv(r) : (c.render ? plain(c.render(r)) : r[c.key]);
  const q = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const lines = [cols.map((c) => q(plain(c.label))).join(","), ...rows.map((r) => cols.map((c) => q(cell(c, r))).join(","))];
  if (tableId === "#projectsTable" || tableId === "#amcTable" || tableId === "#domainTable") {
    lines[0] = q("CRM project ID") + "," + q("Client") + "," + q("Month") + "," + lines[0];
    rows.forEach((r, i) => { lines[i + 1] = [q(state.crm.projects[r.id]?.crm_project_id), q(r.client), q(r.month), lines[i + 1]].join(","); });
  }
  const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
  a.download = `${name}-${state.f.month || "all"}-${todayISO()}.csv`; a.click(); URL.revokeObjectURL(a.href);
}

/* ---------------------------------------------------------------- events */
async function load() {
  const [d, me] = await Promise.all([api("/api/data"), api("/api/me")]);
  state.crm = d.crm; state.tracking = d.tracking;
  state.user = me; state.authEnabled = me.auth_enabled;
  // Fallbacks so the page still works if the server is an older version.
  state.tracking.settings = { amc_default_amount: 11800, amc_due_months: 12, amc_grace_months: 2, ...state.tracking.settings };
}
async function doSync(body = {}, btn) {
  const b = btn || $("#syncBtn"); const old = b.textContent;
  b.disabled = true; b.textContent = "Syncing…";
  try {
    const r = await api("/api/sync", { method: "POST", body: JSON.stringify(body) });
    await load(); fillFilters(); render();
    const e = r.result.errors.length;
    toast(`Synced ${r.result.months.length} month(s): ${r.result.new_projects} new projects, ${r.result.status_changes.length} status changes${e ? `, ${e} errors` : ""}.`, !!e);
    return r;
  } catch (err) { toast(err.message, true); }
  finally { b.disabled = false; b.textContent = old; }
}

function wire() {
  $("#fMonth").onchange = (e) => setMonth(e.target.value);
  $("#fPm").onchange = (e) => { state.f.pm = e.target.value; render(); };
  let st; $("#fSearch").oninput = (e) => { clearTimeout(st); st = setTimeout(() => { state.f.search = e.target.value; render(); }, 150); };
  $("#clearFilters").onclick = () => { state.f = { month: "", pm: "", search: "" }; state.projCat = state.amcChip = state.domChip = state.qualField = ""; fillFilters(); render(); };
  $$(".tabs button").forEach((b) => b.onclick = () => { state.tab = b.dataset.tab; render(); });
  $("#cards").onclick = (e) => {
    const c = e.target.closest(".card"); if (!c) return;
    const [tab, chip] = c.dataset.go.split(":");
    state.tab = tab;
    if (tab === "projects") state.projCat = state.projCat === chip ? "" : chip;
    if (tab === "amc") state.amcChip = state.amcChip === chip ? "" : chip;
    if (tab === "domains") state.domChip = state.domChip === chip ? "" : chip;
    render();
    $(".tabs").scrollIntoView({ behavior: "smooth", block: "start" });
  };
  $$("[data-export]").forEach((b) => b.onclick = () => exportCsv({ projects: "#projectsTable", amc: "#amcTable", domains: "#domainTable", activity: "#activityTable" }[b.dataset.export], b.dataset.export));
  $("#syncBtn").onclick = () => doSync();
  $("#logoutBtn").onclick = async () => {
    try { await api("/auth/logout", { method: "POST", body: "{}" }); } catch {}
    location.href = "/login.html";
  };
  $("#syncForm").onsubmit = async (e) => {
    e.preventDefault();
    const lines = $("#syncInput").value.split(/\s+/).map((s) => s.trim()).filter(Boolean);
    const urls = lines.filter((l) => l.startsWith("http"));
    const months = lines.filter((l) => /^\d{4}-\d{2}$/.test(l));
    const bad = lines.filter((l) => !urls.includes(l) && !months.includes(l));
    if (bad.length) return toast(`Not a CRM link or YYYY-MM: ${bad.join(", ")}`, true);
    const r = await doSync({ urls, months }, e.submitter);
    if (r) { const log = $("#syncLog"); log.hidden = false; log.textContent = r.log.join("\n"); }
  };
  $("#settingsForm").onsubmit = async (e) => {
    e.preventDefault(); const f = e.target;
    const body = { domain_warning_days: +f.domain_warning_days.value, no_deadline_grace_days: +f.no_deadline_grace_days.value,
      currency: f.currency.value, amc_default_amount: +f.amc_default_amount.value,
      amc_due_months: +f.amc_due_months.value, amc_grace_months: +f.amc_grace_months.value, delayed_statuses: $$("#delayedStatuses input:checked").map((i) => i.value) };
    try { const r = await api("/api/settings", { method: "PUT", body: JSON.stringify(body) }); state.tracking.settings = r.settings; await loadChanges(); render(); toast("Rules saved."); }
    catch (err) { toast(err.message, true); }
  };
  $("#trackForm").onsubmit = async (e) => {
    e.preventDefault();
    const body = { amc: {}, renewal: {}, domain: {} };
    for (const el of e.target.elements) {
      if (!el.name) continue; const [sec, key] = el.name.split(".");
      body[sec][key] = el.type === "checkbox" ? el.checked : el.value;
    }
    if (!body.domain.name && !body.domain.expiry_date) { body.domain.auto_renew = false; delete body.domain.ownership; }
    if (!body.domain.auto_renew) delete body.domain.auto_renew;
    try {
      const r = await api(`/api/tracking/project/${encodeURIComponent(state.drawerId)}`, { method: "PUT", body: JSON.stringify(body) });
      if (r.entry) state.tracking.projects[state.drawerId] = r.entry; else delete state.tracking.projects[state.drawerId];
      await loadChanges();
      render(); closeDrawer(); toast("Saved.");
    } catch (err) { toast(err.message, true); }
  };
  $("#drawerClose").onclick = closeDrawer;
  $("#drawerBackdrop").onclick = closeDrawer;
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && state.drawerId) closeDrawer(); });
  $("#themeBtn").onclick = () => {
    const root = document.documentElement;
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("ptd.theme", root.dataset.theme); } catch {}
  };
}

(async function init() {
  try { const th = localStorage.getItem("ptd.theme"); if (th) document.documentElement.dataset.theme = th; } catch {}
  try { const v = JSON.parse(localStorage.getItem("ptd.view") || "null"); if (v) { state.f = { ...state.f, ...v.f }; state.tab = v.tab || state.tab; } } catch {}
  wire();
  try {
    await Promise.all([load(), loadChanges()]);
    if (state.f.month && !state.crm.months[state.f.month]) state.f.month = "";
    renderUser(); fillFilters(); render();
  } catch (err) {
    $("#syncInfo").textContent = "Could not load data: " + err.message + " (is server.py running?)";
  }
})();
