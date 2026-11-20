# Project Status, AMC & Domain Tracker

An internal dashboard for project status (from the Webelio CRM), AMC pitches, payments and reminders, website AMC renewals, and domain expiry.

Needs only Python 3.10 or newer. No packages to install.

## Run on your own computer

```
copy .env.example .env      # once: then set LOGIN_ID and LOGIN_PASSWORD in .env
python server.py                                                   # http://127.0.0.1:8765
python server.py --auto-sync-hours 6   # also re-sync from the CRM every 6 hours
```

On Windows you can also double-click `start.bat`. It opens the browser and auto-syncs every 6 hours.

When the server starts it prints the address for other PCs on the same network, e.g. `http://192.168.29.116:8765`. The PC running the server must stay on. Set `HOST=127.0.0.1` in `.env` to allow this PC only.

## Login

There is **one shared login** for everyone, set in `.env`:

```
LOGIN_ID=admin
LOGIN_PASSWORD=your-password      # at least 6 characters
LOGIN_NAME=Admin                  # name shown in the Activity log
```

- **Change the password:** edit `LOGIN_PASSWORD` in `.env` and restart the server. Everyone is signed out and signs in with the new password.
- The shared login has full access (edit records, sync, settings). Any other accounts from earlier versions are switched off when the server starts.
- Because everyone uses the same login, the Activity log shows changes as made by `LOGIN_NAME`, not by a named person.
- **Security:** the password is kept in the database only as a salted PBKDF2 hash. After 5 wrong passwords from the same PC, that PC is locked out for 15 minutes.

## WhatsApp AMC reminders

In the *AMC & Renewals* tab, or in a project's panel, click **WhatsApp**:

1. WhatsApp (the app, or WhatsApp Web) opens with the reminder already written: the project, amount (incl. GST) and due date.
2. The project name is copied, so you can paste it into WhatsApp's search to find the client's group. Choose the group and send.
3. Back in the dashboard, answer **"Did you send the reminder?"**. **Yes** marks the client as **Informed** with today's date and your name, and your colleagues see it.

WhatsApp doesn't tell other apps whether a message was sent, which is why the dashboard asks. To correct the date or clear it, edit *Client informed on* in the project's panel. Change the message wording under *Sync & settings* → *WhatsApp AMC reminder message*.

The *Still to inform* filter and the *AMC reminders sent* card show which due or overdue AMC clients haven't been informed yet.

## Working together

- **Who changed what:** the *Activity* tab lists every change, with who made it, when, and the old and new value. Each project's panel shows its own history, and the AMC list has a *Last update* column.
- **Live updates:** an open dashboard checks for colleagues' changes every 30 seconds and refreshes itself. It waits if you have a project open, so it never interrupts you.
- **No silent overwrites:** if two people edit the same project at the same time, the second save is stopped with a message saying who changed it. That person then reopens the project and redoes their change.

## Deploy on the company server

The app runs as a small Python service behind nginx (HTTPS). The steps below assume a Linux server with Python 3.10+, nginx and systemd.

**1. Copy the app to the server**, for example to `/opt/project-tracker`, owned by a `tracker` user. Copy `data/tracker.db` along with it, so the existing data and user accounts come too. If you leave it out, the first start imports from `data/crm_store.json` and `data/tracking.json` if they're present, or else syncs from the CRM.

**2. Configure it:** `cp .env.example .env`, then set `LOGIN_ID`, `LOGIN_PASSWORD`, `HOST=127.0.0.1` (nginx sits in front) and `PUBLIC_URL` to the HTTPS address.

**3.** (No account setup needed: the shared login comes from `.env`.)

**4. Run it as a service:** see `deploy/project-tracker.service`.

**5. Put nginx with HTTPS in front:** see `deploy/nginx.conf`. HTTPS matters here, because otherwise passwords would travel over the network unencrypted.

**6. Back up the database daily**, for example with this cron job for the `tracker` user (keeps 30 days):
```
0 2 * * * cd /opt/project-tracker && python3 manage.py backup backups/tracker-$(date +\%F).db && find backups -name 'tracker-*.db' -mtime +30 -delete
```
Create the `backups` folder first. The backup command is safe to run while the server is running. To restore, stop the service, copy a backup over `data/tracker.db`, and start it again.

## Where the data is stored

Everything lives in one SQLite database, `data/tracker.db`.

| Data | Source | Who writes it |
|---|---|---|
| Projects, status, PM, progress, deadline, invoiced/paid/due | CRM public monthly report: `https://crm.webel.io/tracker/public/monthly-report?month=YYYY-MM` | The CRM sync only. Read-only in the dashboard. |
| AMC, website renewal, domain, client informed | Entered in the dashboard (click any project) | Editors and admins |
| Change log | Every save in the dashboard, plus user-account changes | Recorded automatically: who, when, field, old → new value |
| User accounts | *Users* tab or `manage.py` | Admins |

- **AMC, renewal and domain fields each have their own column** in the `tracking` table (`amc_status`, `renewal_due_date`, `domain_expiry_date`, …), so they can be queried directly with SQL. The database itself refuses wrong values: unknown statuses, impossible dates, negative or non-numeric amounts. The fields are defined once, in `FIELDS` in `db.py`. To add one, add it there and add a matching `<section>.<field>` input in `static/index.html`. The column is created on the next start.
- **Upgrading from an older version** (where these fields were stored as JSON) happens automatically on the first start. A backup, `tracker-before-columns-<date>.db`, is written next to the database first. If any stored value doesn't fit, nothing is changed and the server tells you which project and field to fix.

- **Progress and status come from the CRM.** Update them in the CRM, then sync. The dashboard never writes back to the CRM. CRM syncs are listed under *Sync & settings*, with who started them.

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
