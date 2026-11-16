# Project Status, AMC & Domain Tracker

A local dashboard for project status (from the Webelio CRM), AMC pitches and payments, website AMC renewals, and domain expiry.

Needs only Python 3.10 or newer. No packages to install.

## Run on your own computer

```
python server.py                      # http://127.0.0.1:8765
python server.py --auto-sync-hours 6  # also re-sync from the CRM every 6 hours
```

On Windows you can also double-click `start.bat`. It opens the browser and auto-syncs every 6 hours.

Without Google sign-in configured, the dashboard runs in **local mode**: no login, and it only answers requests made on the same machine.

## Deploy on the company server

The app runs as a small Python service behind nginx (HTTPS). Colleagues sign in with their @webel.io Google account. The steps below assume a Linux server with Python 3.10+, nginx and systemd.

**1. Create the Google sign-in client** (once, in the Webelio Google Cloud project):
1. Go to Google Cloud Console → *APIs & Services* → *OAuth consent screen*. Choose **Internal**, which limits sign-in to your Workspace.
2. Go to *Credentials* → *Create credentials* → *OAuth client ID* → *Web application*.
3. Under *Authorized redirect URIs*, add `https://<your tracker domain>/auth/callback`.
4. Copy the client ID and client secret.

**2. Copy the app to the server**, for example to `/opt/project-tracker`, owned by a `tracker` user. Copy `data/tracker.db` along with it so the existing data comes too. If you leave it out, the first start imports from `data/crm_store.json` and `data/tracking.json` if they're present, or else syncs from the CRM.

**3. Configure it:** `cp .env.example .env`, then fill in `PUBLIC_URL`, `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. Make the file readable only by the service user: `chmod 600 .env`.

**4. Run it as a service:** see `deploy/project-tracker.service`.

**5. Put nginx with HTTPS in front:** see `deploy/nginx.conf`.

**6. Back up the database daily**, for example with this cron job for the `tracker` user (keeps 30 days):
```
0 2 * * * cd /opt/project-tracker && python3 db.py backup backups/tracker-$(date +\%F).db && find backups -name 'tracker-*.db' -mtime +30 -delete
```
Create the `backups` folder first. `db.py backup` is safe to run while the server is running. To restore, stop the service, copy a backup over `data/tracker.db`, and start it again.

**Access:** anyone with an @webel.io account can sign in and edit. To limit this, list people in `ALLOWED_EMAILS`. To give someone view-only access, list them in `READONLY_EMAILS`. Restart the service after changing `.env`.

## Where the data is stored

Everything lives in one SQLite database, `data/tracker.db`.

| Data | Source | Who writes it |
|---|---|---|
| Projects, status, PM, progress, deadline, invoiced/paid/due | CRM public monthly report: `https://crm.webel.io/tracker/public/monthly-report?month=YYYY-MM` | The CRM sync only. Read-only in the dashboard. |
| AMC, website renewal, domain | Entered in the dashboard (click any project) | Signed-in editors |
| Change log | Every save in the dashboard | Recorded automatically: who, when, field, old → new value |

- **Change history.** The *Activity* tab lists every change across all projects. Each project's panel shows its own history and who last updated it. CRM syncs are listed under *Sync & settings*, with who started them.
- **Progress and status come from the CRM.** Update them in the CRM, then sync. The dashboard never writes back to the CRM.

- **Sync.** "Sync from CRM" reads the month list from the CRM page and fetches every month. To sync only certain months, paste their links under *Sync & settings*, or run `python crm_sync.py 2025-09 2025-10`.
- **No duplicates.** Records are keyed by the CRM project ID taken from the project link. Syncing a month again updates that month's snapshot and doesn't add new rows.
- **History.** Every project keeps a snapshot for each month it appears in. Status changes between syncs are listed in the sync history.
- **Original values.** The CRM text is stored exactly as received (`raw`) next to the parsed values.
- **Missing data.** Blank CRM fields show as **Missing** and are listed under *Data quality*. Nothing is filled in with guesses.
- **CRM access.** Only the public monthly report is readable. The CSV export and the other tracker pages need a CRM login. If the monthly report ever needs a login too, the sync stops and says so.

## How projects are grouped

The CRM statuses are Not Started, In Progress, On Hold, Finished, Cancelled, Disputed and Not Responding. There is no "Delayed" status, so the dashboard works it out:

- **Finished**: CRM status is Finished.
- **Cancelled**: counted in the total and shown separately.
- **Delayed**: any open project where one of these is true:
  - its deadline has passed;
  - its status is On Hold, Disputed or Not Responding (you can change this list);
  - it has no deadline and its report month ended more than *N* days ago (default 30).
- **In Progress**: every other open project.

Each Delayed project shows the reason. Change the rules under *Sync & settings*.

## AMC schedule (automatic)

- **Which projects:** website projects only, meaning projects with a website assignee in the CRM. Cancelled projects are left out. To change this for one project, set "AMC applies" to Yes or No in the project panel.
- **Amount:** ₹11,800 including GST, pre-filled. You can change it for one project, or change the default under *Sync & settings*.
- **Timing:** counted from the first month the project appears in the CRM. Example: a project starting May 2025 is **AMC due** from 1 May 2026 (12 months) and **Under AMC** from 1 Jul 2026 (2 more months, grace). The AMC period then runs one year, to 1 Jul 2027, which is pre-filled as the renewal due date.
- **Stages:** Not due yet → AMC due → Under AMC · unpaid. Setting the pitch/payment status to Received, or entering an amount received, marks the project as AMC received. Declined marks it Declined.
- Values you save for a project always take priority over the pre-filled ones.

## Domain ownership

Each domain is either **With Webelio** (the default) or **With client**. Only domains with Webelio count towards the "about to expire" and "expired" cards and alerts. Client-owned domains are still listed, under the "With client" filter.

## Summary cards

| Card | Formula |
|---|---|
| Under AMC, unpaid | AMC projects older than 12 + 2 months, with no AMC payment recorded |
| AMC due | AMC projects 12–14 months old, with no AMC payment recorded |
| AMC pitched | AMC status is Pitched, Received or Declined |
| AMC received | AMC status is Received |
| AMC amount received | Sum of "Amount received" |
| Website AMC renewals | Number with renewal status Renewed, plus the number still Pending |
| Domains about to expire | Held by Webelio; expiry date is within the warning window (default 30 days) |
| Expired domains | Held by Webelio; expiry date is before today |

All cards follow the Month, PM and Search filters. Click a card to open the matching list.
