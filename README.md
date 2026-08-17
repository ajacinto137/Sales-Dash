# Planet Networks — Sales Performance Dashboard

An internal Flask + pandas dashboard that replaces the Excel/VBA sales
reporting workflow. It connects to the PlanetWeb SQL Server database and
the KPI PostgreSQL database, runs the same three queries currently used by
Excel/VBA, and displays the results in a web dashboard.

The entire application runs in Docker — the SQL Server ODBC driver is
installed **inside the container**, so you do not need to install it on
your Mac to use this project.

This application is **read-only against its two source databases** (PlanetWeb
SQL Server and KPI PostgreSQL) — it only ever executes `SELECT` statements
against them. It does have one app-owned database of its own, a separate
Postgres instance ("appdb") that backs the Needs Attention workflow
(rep-entered Attention Status + notes) — see "Needs Attention Workflow"
below. That data never comes from, or writes back to, either source
database.

## Stack

**Backend**
- [Flask](https://flask.palletsprojects.com/) (Python) — web app and routes (`app.py`)
- [pandas](https://pandas.pydata.org/) — data wrangling and metrics calculations (`sales_metrics.py`)
- [pyodbc](https://github.com/mkleehammer/pyodbc) + Microsoft ODBC Driver 18 — connects to the PlanetWeb **SQL Server** database (read-only)
- [psycopg2](https://www.psycopg.org/) — connects to the KPI **PostgreSQL** database (read-only) and the app-owned **appdb** PostgreSQL database (read/write, see "Needs Attention Workflow")
- [python-dotenv](https://github.com/theskumar/python-dotenv) — loads credentials from `.env`
- Raw SQL queries defined in `queries.py`, DB connection/health-check helpers in `db.py`
- `attention_store.py` — the Needs Attention workflow's persistence layer (Attention Status + notes), the only module in this app that writes to a database

**Frontend**
- Server-rendered [Jinja2](https://jinja.palletsprojects.com/) templates (`templates/`)
- Vanilla JavaScript (`static/js/dashboard.js`, `static/js/team_dashboard.js`, `static/js/attention.js`) — no frontend framework or build step
- [Chart.js](https://www.chartjs.org/) (v4, via CDN) — sales-over-time chart
- Plain CSS (`static/css/dashboard.css`, `static/css/team_dashboard.css`)

**Infrastructure**
- Docker / Docker Compose — the entire app, including the SQL Server ODBC driver, runs in a container (`Dockerfile`, `docker-compose.yml`)
- A second container, `appdb` (`postgres:16-alpine`, its own named volume) — the app-owned database backing the Needs Attention workflow, entirely separate from the two read-only source databases
- Base image: `python:3.12-slim-bookworm`
- App served on port `3005`

## Local Development

Uses `docker-compose.yml` — the Flask dev server on port 3005 directly,
no nginx, no TLS. This is the everyday workflow; it is unaffected by the
production setup described further down.

### 1. Prerequisites

- Docker Desktop (that's it — no Python or ODBC driver install needed on
  the Mac for the Docker workflow)

### 2. Configure credentials

Copy the template and fill in the credential values:

```bash
cp .env.example .env
```

```env
PLANETWEB_USERNAME=
PLANETWEB_PASSWORD=

KPI_USERNAME=
KPI_PASSWORD=

APPDB_PASSWORD=
```

The first four are the source-database credentials your team already
uses. Everything else about them (hosts, database names, ports, driver
name, SSL settings, and the queries themselves) is already filled in.

`APPDB_PASSWORD` is different in kind: it's not an existing credential to
look up anywhere — it sets the password for a brand-new, empty Postgres
database (`appdb` in `docker-compose.yml`) that this app creates for
itself on first `docker compose up`, used only by the Needs Attention
workflow (see "Needs Attention Workflow" below). Any value works; there's
nothing to "get right" beyond picking one.

`.env` is excluded from Git via `.gitignore` — never commit it once it
contains real credentials. `.env.example` is the tracked template that's
safe to commit; keep it in sync when you add new variables.

### 3. Start

```bash
docker compose up --build
```

This builds the image (installing ODBC Driver 18 for SQL Server and all
Python dependencies inside the container), starts the `appdb` Postgres
container alongside it (pulled from Docker Hub, not built), then starts
Flask on `0.0.0.0:3005` inside the container, mapped to your Mac at
`3005:3005`. `appdb` has no port published to the host — only the
`dashboard` container talks to it, over the Compose-internal network.

### 4. Open

```text
http://localhost:3005
```

You should see:

- **Planet Networks / Sales Performance** branding
- Connection status for PlanetWeb SQL Server and KPI PostgreSQL
- Query status for Main Sales, Service Cancellations, and Vision Packages
- Row/column counts, a sales-over-time chart, and the first 500 rows of
  each dataset (the full result stays in memory — only the display is
  capped)

### 5. Stop

```bash
docker compose down
```

### Other useful commands

```bash
# Run in the background
docker compose up -d --build

# Tail logs
docker compose logs -f

# Restart the container
docker compose restart
```

## Troubleshooting

Check the logs first:

```bash
docker compose logs -f
```

Common connection failures and what they usually mean:

| Message contains              | Likely cause                                             |
|--------------------------------|-----------------------------------------------------------|
| `Login failed`                 | Wrong username/password in `.env`                         |
| `SSL` / `certificate`          | TLS/SSL negotiation issue — check `Encrypt`/`sslmode` settings |
| `timeout` / `timed out`        | Network/VPN issue, or the host is unreachable from Docker  |
| `could not translate host name` / `Unable to resolve hostname` | DNS issue — check the host value in `.env` |
| `driver`                       | ODBC driver problem — rebuild with `docker compose up --build` |
| `permission denied`            | The database account doesn't have SELECT rights on a table |

The two databases are tested and queried **independently** — if PlanetWeb
SQL Server fails, KPI PostgreSQL (and vice versa) will still attempt to
load and display its data. Likewise, if the Service Cancellations query
fails, the Vision Packages query still runs.

## Health check

```text
GET /health
```

Returns application status as JSON (does not require both databases to be
up for Flask itself to report healthy):

```json
{
  "status": "ok",
  "planetweb_connected": true,
  "kpi_connected": true,
  "last_refreshed": "8/12/2026 8:04 PM"
}
```

## Production Deployment

Uses `docker-compose.prod.yml` — a separate stack from local dev, run
with `-f`, that puts nginx in front of the app over HTTPS. It does not
replace or modify `docker-compose.yml`; local dev keeps working exactly
as above.

What it adds on top of the dev stack:

- **nginx** (`nginx:1.27-alpine`) reverse-proxying to the app, listening
  on `80` (redirects to HTTPS) and `443`
- **gunicorn** instead of the Flask dev server for the app container
  (`FLASK_DEBUG` is forced off)
- **HTTPS using a certificate you supply** — nginx does not obtain or
  renew certificates itself; point it at a directory that already has
  one (from your CA, an internal PKI, Let's Encrypt via `certbot` run
  separately, etc.)

### 1. Provide the SSL certificate

Put `fullchain.pem` and `privkey.pem` in a directory on the host, then
set its path in `.env`:

```env
DOMAIN=dashboard.example.com
SSL_CERTS_DIR=/path/to/your/certs
```

`SSL_CERTS_DIR` is mounted read-only into the nginx container at
`/etc/nginx/certs`. It defaults to `./certs` in the project folder
(gitignored) if you don't have another location in mind. Keeping the
certificate renewed (e.g. via a `cron`'d `certbot renew` on the host,
or your CA's own tooling) is up to whatever issued it — this stack just
consumes whatever's in that directory.

If the certificate comes from `certbot` running directly on the host
(not in this compose stack), use **webroot** mode rather than
`--standalone` for renewals — nginx permanently holds port 80/443 once
this stack is up, so `--standalone` (which binds port 80 itself) will
fail every renewal. This project's `./webroot` directory is mounted
into nginx at `/var/www/certbot` and `nginx/conf.d/http.conf` already
serves `/.well-known/acme-challenge/` from it, so:

```bash
sudo certbot certonly --webroot -w /path/to/Sales-Dash/webroot \
  -d dashboard.example.com --email you@example.com --agree-tos --no-eff-email
```

For renewals to actually reach the running app's certificate location,
add a deploy hook (`/etc/letsencrypt/renewal-hooks/deploy/`) that
copies the renewed `fullchain.pem`/`privkey.pem` into `SSL_CERTS_DIR`
and restarts the `nginx` service.

### 2. Start

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

The site is served over HTTPS at `https://$DOMAIN`. If nginx fails to
start, check that `fullchain.pem`/`privkey.pem` actually exist in
`SSL_CERTS_DIR` — nginx will refuse to boot without them.

### Everyday commands

```bash
# Start
docker compose -f docker-compose.prod.yml up -d --build

# Logs
docker compose -f docker-compose.prod.yml logs -f

# Stop
docker compose -f docker-compose.prod.yml down
```

## Team Leaderboard Page — Section Names

The `/dashboard` route (`templates/dashboard.html`, styled by
`static/css/team_dashboard.css`) has nine standing sections, in this page
order. These are the canonical names to use when referring to a section of
this page — they're also marked with matching HTML comments in the
template itself.

Planet Networks Records, Current Sales Leaders, and Live Sales Activity
used to be a single "Planet Networks Records" section holding all 9 cards;
it was split into three on 2026-08-16 because only the Best Days/Weeks/
Months Ever cards are actual historical records — the rest are current
leaderboards or live sales activity, and the old single heading no longer
described them accurately.

| Name | Aka | What it is | Template markup |
|------|-----|------------|------------------|
| **Team Overview** | | Fixed set of 5 KPI cards, always in this order: Monthly Sales, Daily Sales, 7 Day Average, 30 Day Average, Monthly Projection (reconfigured 2026-08-14, by request — replaces the earlier Total Sales/Installed/Pending/Total Daily Sales/Team Install Rate/Monthly Forecast set). All five are always computed from the full all-time dataset and today's real date — they do NOT change with the page's period filter | `<section class="td-kpi-bar">` |
| **Sales Calendar & Monthly Sales Trend** | | One row (`.td-activity-split`, same 2-col layout the Individual Sales Profile's own Sales Activity section uses — merged 2026-08-16, by request), pairing two sub-sections that each keep their own canonical name: **Sales Calendar** (navigable month-by-month calendar of team total sales per day) and **Monthly Sales Trend** (team-wide total sales per calendar month, trailing 12 months, as a MonthlySalesTrendChart line chart, added 2026-08-16). Both always all-time, independent of the period filter — same convention as Records/Calendar. Monthly Sales Trend is distinct from the Individual Sales Profile's MonthlySalesChart (per-rep, day-granularity within one month) | `<section class="td-section">` containing `<h2>Sales Calendar &amp; Monthly Sales Trend</h2>` + `<div class="td-activity-split">` |
| **Channel & Time-of-Day Performance** | | One row, one card, a segmented-control toggle switches between the team-wide SalesByChannelChart and HourlySalesChart (merged from two separate sections 2026-08-16, by request). **Sales by Hour is the default-shown chart** (changed 2026-08-16, by request). Period-filtered, reusing `calculate_channel_breakdown()`/`calculate_hourly_breakdown()` unchanged — the same functions/charts already used on the Individual Sales Profile, just not rep-scoped. Channel bars are clickable, opening a channel-scoped team-wide Bulk Account View; the hourly chart has no drill-down. Each chart is created lazily the first time its tab is opened (`wireChartToggle()` in `static/js/charts.js`) | `<section class="td-section">` containing `<h2>Channel &amp; Time-of-Day Performance</h2>` + `{% include "_channel_hourly_toggle.html" %}` |
| **Planet Networks Records** | "Records Section" | Historical all-time individual sales achievements: Best Days Ever, Best Weeks Ever, Best Months Ever. **Collapsed by default** (added 2026-08-16, by request) — a native `<details>`/`<summary>` disclosure, no JS involved; click the heading to expand/collapse | `<section class="td-section">` containing `<details class="td-collapsible"><summary class="td-section-heading td-collapsible-summary"><h2>Planet Networks Records</h2>` |
| **Current Sales Leaders** | | Current-period individual sales leaderboards — who's leading right now: Weekly Sales, Monthly Sales, Yearly Sales (each card is its own live top-3, independent of the page's period filter). Daily Sales moved to Live Sales Activity 2026-08-16 (by request, for an even 3+3 card split with that section) | `<section class="td-section">` containing `<h2>Current Sales Leaders</h2>` |
| **Live Sales Activity** | | Today's leaderboard plus recent sales and account activity: Daily Sales (moved here from Current Sales Leaders 2026-08-16 — same `records.daily_leaders` data, unchanged), Latest Sales (the reps behind the most recent sales), and Latest Accounts Sold (the accounts behind those same sales, renamed from "Last 3 Sales"/"Last 3 Accounts Sold" 2026-08-16 since the displayed count is a display detail, not part of the card's identity), which also carries the "View All Sales" button in its top-right corner. Now a 3-column grid (`.td-activity-grid`, widened 2026-08-16 to fit the third card) | `<section class="td-section">` containing `<h2>Live Sales Activity</h2>` |
| **Individual Rep Leaderboard** | "Rep Leaderboard", "Leaderboard Table", "Rep Performance Section" | The rep search field plus the ranked table (Rank, Sales Rep, Sales, Outbound, Inbound, Installs, Pending, **Needs Attention** (replaced the Cancels count column 2026-08-17, by request), Install Rate) — every column header is clickable to sort the table by that column. **Cancel Rate column removed 2026-08-17, by request** | `<h2>Individual Rep Leaderboard</h2>` + `<form class="td-search-form">` + `<section class="td-table-card">` |

**Every rep name on this page links to that rep's Individual Sales
Profile** (added 2026-08-16, by request — "Everytime a reps name appears
in the team leaderboard... link to their individual rep profile"). This
was already true of the Individual Rep Leaderboard table (`.td-rep-link`,
added earlier); as of 2026-08-16 it also applies to every `r.rep` shown
in Planet Networks Records (Best Days/Weeks/Months Ever), Current Sales
Leaders (Weekly/Monthly/Yearly Sales), and Live Sales Activity (Daily
Sales, Latest Sales) — all now `<a class="td-record-rep"
href="{{ url_for('rep_profile', rep_name=r.rep, period=period,
start=custom_start, end=custom_end) }}">` instead of a plain `<span>`,
carrying the page's current period filter forward the same way the
"Back to Team Leaderboard" and Rep Leaderboard table links already do.
**Exception:** the Latest Accounts Sold card's `r.account` (a customer
name, not a rep) intentionally stays a plain `<span class="td-record-rep">`
— `.td-record-rep` is shared CSS between the two, but only the anchor
variant (`a.td-record-rep`) gets link styling/hover treatment. Don't
link account names to a rep profile if this pattern is extended further.

Column sorting (`?sort=<column>&dir=asc|desc`) is a pure display-ordering
concern applied after search/selection, via `sort_rep_rows()` in
`sales_metrics.py` — it reorders the rows `calculate_rep_metrics()`
already computed and never recalculates anything or changes which rep is
auto-selected. The `#`/Rank column's *value* always reflects a rep's real
sales-based standing regardless of which column the table is currently
sorted by; sorting by `rank` (the default) just restores that original
order. Rates with no denominator (`None`, shown as "—") always sort to
the bottom in either direction.

The Individual Rep Leaderboard and Channel & Time-of-Day Performance
sections all read from the period-filtered dataset (`period_df` in
`dashboard_page()`, `app.py`), so a date-range change affects them. Team Overview, Planet Networks Records, Current Sales
Leaders, Live Sales Activity, Monthly Sales Trend, and the Sales Calendar
are all independent of the selected date range — they always use the
all-time dataset / today's real date (see `calculate_total_daily_sales()`,
`calculate_daily_averages()`, `calculate_monthly_forecast()`,
`calculate_records()`, `calculate_monthly_sales_trend()`, and
`calculate_calendar_sales()` in `sales_metrics.py`). Planet Networks
Records, Current Sales Leaders, and Live Sales Activity all read from the
same `calculate_records()` output (`records` in the template) — the split
into three sections is purely a template/CSS reorganization, not a new
data function. The Sales Calendar has its own independent month
navigation (`cal_year`/`cal_month` query params, defaulting to the
current month) that doesn't affect the period filter or any other
section.

Note: there used to be a per-rep "Sales by Channel" breakdown section
(a table, per-rep counts across the 8 raw channels) between Planet
Networks Records and Rep Leaderboard — it was removed by request
2026-08-14. The current **Channel Performance** section (added
2026-08-16, at explicit user request) is a different thing: a team-wide
aggregate SalesByChannelChart, not a per-rep table, reusing the same
`calculate_channel_breakdown()` the Individual Sales Profile already
used. Don't conflate the two or assume the old per-rep table should come
back — that request would need to be made explicitly again. The
underlying channel data (`SALES_CHANNELS`, `INBOUND_CHANNELS`/`OUTBOUND_CHANNELS` in
`sales_metrics.py`) is still intact and still powers the Rep
Leaderboard's Inbound/Outbound columns. `calculate_channel_breakdown()`
was reintroduced for the Rep Profile's Overview tab 2026-08-14 (scoped to
a single rep there), then reused unchanged for this page's own Channel
Performance section 2026-08-16 (team-wide, no rep filter) — one function,
two callers, never duplicated. `calculate_hourly_breakdown()` (added
2026-08-16) is its sibling for the Time-of-Day Performance chart on both
pages — same period-scoping pattern, grouped by hour instead of channel,
called with `rep_period_df` on the Rep Profile and team-wide `period_df`
here.

The Team Overview KPI bar's Monthly Sales and Monthly Projection cards
both read `calculate_monthly_forecast()`'s output: Monthly Sales is
`month_to_date` (sales so far this calendar month), Monthly Projection is
`projected_total`, a simple run-rate projection (month-to-date sales ÷
days elapsed this month × days in the month). Both always reflect the
actual current calendar month, not whatever month the Sales Calendar is
currently browsing to. The 7 Day Average and 30 Day Average cards read
`calculate_daily_averages()` — trailing count-of-sales-in-window ÷ window
size (7 or 30), window = today back N-1 days inclusive.

## Rep Profile Page — Section Names

Clicking a rep's name in the Individual Rep Leaderboard table (`.td-rep-link`) opens
that rep's dedicated `/dashboard/reps/<rep_name>` page
(`templates/rep_profile.html`, `rep_profile()` in `app.py`), styled by
the same `static/css/team_dashboard.css` as the Team Leaderboard. It
carries over the leaderboard's currently selected `period`/`start`/`end`
query params and has its own independent period selector after that. The
page has two tabs:

| Name | What it is |
|------|------------|
| **Overview** | Metric cards for Total Sales, Installed, Install Rate (primary) and Inbound, Outbound, Pending (secondary, now 3 cards — Cancelled and Cancel Rate removed 2026-08-17, by request) — all period-filtered — plus two chart sections (added 2026-08-16): **Sales Activity** (a rep-scoped Sales Calendar beside a `MonthlySalesChart` bar chart, day-of-month with a cumulative trend line and per-day channel breakdown in the tooltip; always all-time with its own `cal_year`/`cal_month` nav, independent of the period filter, same convention as the Team Leaderboard's Sales Calendar); **Channel & Time-of-Day Performance** (one row, one card, a segmented-control toggle between a `SalesByChannelChart` horizontal bar chart — replacing the former plain-text Inbound/Outbound columns, reuses `calculate_channel_breakdown()` unchanged, bars clickable into a channel-scoped Bulk Account View — and an `HourlySalesChart` across all 24 real hours built from `sale_datetime` via `calculate_hourly_breakdown()`, merged from two separate sections into one toggle 2026-08-16 by request; Sales by Hour is the default-shown chart, changed the same day by request). Renders via Chart.js v4.4.4 (loaded in `rep_profile.html`'s `<head>`, same version already used on the `/overview` page) and the shared `static/js/charts.js` helper module — see "Chart Card" below. |
| **Needs Attention** | Accounts for this rep that are not Installed/Cancelled and have no install date in the future — i.e. no install date at all, explicitly "Not Scheduled", or an install date today or earlier (redefined 2026-08-17, see "Pending vs Needs Attention" below). Always uses the full all-time dataset regardless of the page's period filter — same reasoning as Planet Networks Records / Sales Calendar / Team Overview on the Team Leaderboard. Rendered via the shared Bulk Account View component (see below), extended with the **Needs Attention Workflow** (Attention Status + notes — see its own section further down). |

The Overview tab's metric cards reuse `calculate_rep_metrics()` — the
exact same function/output that produces the Individual Rep Leaderboard table rows
— so the two pages can never disagree for the same period. The Needs
Attention tab's count is shown directly in the tab label
(`Needs Attention (N)`), with a subtle warning treatment when `N > 0`.

## Needs Attention Workflow

> Lets a sales rep classify each Needs Attention account (why it needs
> attention, what's being done about it) and leave a running note history
> — added 2026-08-17. This is **operational metadata layered on top of**
> the Needs Attention Bulk Account View, not a change to what "Needs
> Attention" means or how many accounts are in it.

**The two rules this feature must never break:**
1. **The Needs Attention count never changes based on anything a rep does
   in this workflow.** It's still whatever `build_sales_dataset()` /
   `calculate_rep_metrics()` / `calculate_needs_attention()` compute from
   the source data alone (see "Pending vs Needs Attention" above) —
   classifying every single account in a rep's list doesn't remove or
   hide a single one of them from that count.
2. **The Install Rate formula never changes.** Still `1.0 -
   (needs_attention / total_sales)`, exactly as before this feature
   existed. Attention Status is not read by `calculate_rep_metrics()` at
   all — `sales_metrics.py` has no import of, or dependency on, this
   feature's code, in either direction.

Both hold structurally, not just by convention: `attention_store.py` (the
only module that touches this feature's data) is never imported by
`sales_metrics.py`, and nothing in `sales_metrics.py` was changed to
build this feature — only `build_bulk_account_rows()` gained one new
passthrough field (`sale_id`, see below), which no calculation reads.

### Attention Status

One of exactly six values — **Duplicate, Cancellation, Engineering
Issues, Underground Issues, Existing Customer, Other** — or unset
("Unclassified" in the UI, never a stored value; there is deliberately no
"Unreviewed"/"In Progress"/"Addressed"/"Resolved" status). Validated
against this fixed list server-side on every write
(`attention_store.ATTENTION_STATUS_SET`), regardless of what the
`<select>` in the browser already restricted it to.

**A status can never exist without a note.** The only function that ever
writes `attention_status` is `attention_store.set_attention_status(sale_id,
status, note, author)`, and it rejects a blank/whitespace-only note before
writing anything — so "a status requires a note" is enforced by there
being exactly one write path, not a rule duplicated across two code
paths that could drift apart. Changing an already-set status back to a
*different* value also requires a new note (same function, same
validation) — the previous status/notes are never deleted, only added to.

**Addressed** = has a non-null `attention_status` (which, by the rule
above, always means at least one note exists too — the two conditions
in the task's spec collapse to one check in practice).
**Remaining** = Needs Attention total − Addressed. Both are pure
workflow metrics, shown as a progress bar/stat row and a set of
client-side filter chips (All / Unclassified / Addressed / each status)
at the top of the Needs Attention Bulk Account View — filtering only
hides/shows already-rendered accounts (`static/js/attention.js`), it
never re-queries or changes the underlying list.

### Notes

Append-only activity history — one row per note, never updated or
deleted, newest first. Every note records which `attention_status` was
in effect when it was written, and (only when that save *changed* the
status) the `previous_status` it changed from, which is what lets the UI
render an audit line like "Attention Status changed from Engineering
Issues to Cancellation" without a separate audit table. After an account
is classified, a rep can add further notes without touching its status
(`attention_store.add_note()` — requires the account to already have a
status; there's nothing to "add another note" to before the first one).

### Persistence

A dedicated, app-owned PostgreSQL database — **not** the KPI PostgreSQL
source database, and not a table added to either source database. The
task that introduced this feature was explicit that this app shouldn't
assume permission to write into an existing business database, and the
README's own read-only rule already covered both PlanetWeb and KPI — so
this feature gets a third database, `appdb`, that's entirely this app's
own from the start (`docker-compose.yml` / `docker-compose.prod.yml`,
`postgres:16-alpine`, its own named volume so data survives a container
restart/redeployment). Credentials: `APPDB_HOST`/`APPDB_PORT`/
`APPDB_DATABASE`/`APPDB_USERNAME`/`APPDB_PASSWORD` in `.env` (see
"Configure credentials" above) — `db.get_appdb_connection()` is the
connection helper, parallel to `get_planetweb_connection()`/
`get_kpi_connection()`.

Schema (auto-created on first use via `CREATE TABLE IF NOT EXISTS`,
`attention_store._ensure_schema()` — no separate migration step or
tooling; this app has none today, so a startup-time idempotent schema
check is the smallest thing that could work):

```sql
account_attention (
    sale_id BIGINT PRIMARY KEY,        -- see "why sale_id" below
    attention_status TEXT NOT NULL,
    created_at, updated_at, updated_by
)

account_attention_notes (
    id SERIAL PRIMARY KEY,
    sale_id BIGINT NOT NULL REFERENCES account_attention(sale_id),
    note TEXT NOT NULL,
    attention_status TEXT NOT NULL,     -- status in effect when written
    previous_status TEXT,               -- set only on an actual status change
    created_at, created_by
)
```

`account_attention`'s primary key on `sale_id` is what makes a second
classification of the same account an upsert (`INSERT ... ON CONFLICT
(sale_id) DO UPDATE`) rather than a duplicate row — "no duplicate
attention record for the same account" holds by construction, not by a
pre-check. All queries are parameterized (`cur.execute(sql, (params,))`
throughout `attention_store.py`) — no string-built SQL anywhere in this
module.

**Why `sale_id`, not `subscriber_uuid`:** the task suggested preferring
the subscriber UUID already used for Vision links
(`vi_subscriber_uuid`/`subscriber_uuid` in the normalized dataset).
Checked against live data before committing to it — at the time of
writing, ~6.5% of Needs Attention accounts (42 of 647) have no
`subscriber_uuid` at all, concentrated in exactly the not-yet-installed
population this feature exists to serve (that field is only reliably
populated once an account is further along). `sale_id` — Main Sales' own
primary key (`FTTPFormData.ID`, the `sale_id` column already in the
normalized dataset) — has 0 nulls and 100% uniqueness across the whole
dataset, so it's the only identifier that lets *every* Needs Attention
account be classified. Full reasoning lives in `attention_store.py`'s
module docstring.

### Failure handling

If `appdb` is unreachable, every read function in `attention_store.py`
returns an explicit `available=False` (never raises, never silently
implies "zero accounts classified"). `app.py`'s
`attach_attention_metadata()` surfaces that as `attention_available` in
the template context: `_attention_overview.html` shows a plain "Attention
Status & notes are temporarily unavailable" notice instead of the
progress bar, and `_attention_controls.html` (the per-card edit UI) is
skipped entirely rather than rendering Save buttons that would just fail.
Everything else on the page — the Needs Attention account list itself,
`Needs Attention (N)` in the tab label, Install Rate, every other Rep
Profile section — renders completely normally, since none of it reads
from `attention_store.py`. Verified by stopping the `appdb` container
and confirming the Rep Profile still returns 200 with the notice shown
and the source-data numbers unchanged.

### Routes / functions

- `POST /dashboard/attention/<int:sale_id>/status` — set or change
  Attention Status (`attention_set_status()` in `app.py` →
  `attention_store.set_attention_status()`). JSON body:
  `{status, note, author}`. Returns the account's full updated note list.
- `POST /dashboard/attention/<int:sale_id>/notes` — add a note without
  changing status (`attention_add_note()` → `attention_store.add_note()`).
  JSON body: `{note, author}`.
- Both validate `sale_id` against the currently loaded normalized dataset
  (`_known_sale_id()`) before touching `attention_store` at all — a
  `sale_id` is a value the browser sends back to us, so it's never
  trusted to correspond to a real account without checking first (404 if
  not). Both validate/trim/length-cap the note and validate the status
  against the approved list **server-side**, independent of whatever the
  browser already checked — the task was explicit not to rely on
  JS-only validation.
- `attach_attention_metadata(accounts)` (`app.py`) — the one function
  that joins Attention Status/notes onto an already-built list of
  `build_bulk_account_rows()` dicts, and computes the Addressed/Remaining
  progress numbers. Called from `rep_profile()` (the inline Needs
  Attention tab), `bulk_account_view()`, and `all_sales_view()`
  (the standalone/team-wide Bulk Account View pages), every time
  **only** for `view == "needs_attention"`.

### How this extends Bulk Account View

No new list component, exactly per the project's existing rule (see
"Reusable Views" below) — the Needs Attention Bulk Account View is still
`_bulk_account_view.html`, unchanged for every other view. Two small
additions, both gated on `view == 'needs_attention'` so no other view
(Pending, All Sales, channel/metric/date drill-downs) is affected:
- `templates/_attention_overview.html` — the progress bar/stats + filter
  chips, included once near the top.
- `templates/_attention_controls.html` — the per-account badge +
  Classify/Add Note/View Notes controls, included inside each `.td-bulk-card`
  in the **Cards** view only. The **Table** view intentionally doesn't
  get inline edit controls (would mean a 7th column and a much more
  cramped CSS Grid for every other view too) but its rows still carry the
  same `data-sale-id`/`data-attention-status` attributes as their Cards
  counterpart, so the filter chips stay in sync no matter which view mode
  is showing.
- `build_bulk_account_rows()` gained one new field, `sale_id` — see "Why
  `sale_id`" above. It's a plain passthrough (Main Sales' existing
  primary key), read by no calculation, so every other Bulk Account View
  caller is unaffected by its presence.
- `static/js/attention.js` — new, loaded on `rep_profile.html` and
  `bulk_account_view.html` (both already load `team_dashboard.js`). A
  no-op on every page/view without `[data-attention-*]` elements.

## Reusable Views

### Bulk Account View

> The standardized reusable account-list interface used throughout the
> Sales Dashboard whenever a user needs to view a filtered collection of
> customer/sales accounts.

A Bulk Account View is not rebuilt per feature. **The dataset and title
can change; the component stays the same.** Concretely:

- `templates/_bulk_account_view.html` is the shared markup — one Jinja
  partial, included wherever a filtered account list needs to appear. It
  expects an `accounts` list (rows shaped by `build_bulk_account_rows()`
  in `sales_metrics.py`) and an `empty_message` string, and renders each
  account's `sale_id` (stable identifier, added 2026-08-17 for the Needs
  Attention Workflow — see its own section above), First Name, Last Name,
  Address, Scheduled Install Date, status/category badge, Sales Channel,
  and a Vision link (new tab, `noopener
  noreferrer`, built from `vi_subscriber_uuid` via `build_vision_url()`,
  omitted rather than broken when the UUID is missing).
- `BULK_ACCOUNT_VIEWS` (`sales_metrics.py`) is the registry mapping a
  view key to a title and a filter function over the normalized dataset.
  Currently defined: `pending` (period-filtered), `needs_attention`
  (all-time), and `all_sales` (period-filtered, identity filter — every
  account in the scoped date range, added 2026-08-14). **Adding a new
  Bulk Account View — e.g. "show Installed accounts" or "show Door to
  Door sales" — means adding one entry to this registry, not building a
  new list UI.**
- **Pending vs Needs Attention (redefined 2026-08-17, by request)**: both
  `pending` and `needs_attention` are just `_bulk_status_filter(status)`
  over the `status` column now — the actual classification logic lives
  in one place, `build_sales_dataset()`, not duplicated across two
  filter functions (there used to be a separate `_needs_attention_filter`
  doing its own independent date comparison; it's gone, replaced by a
  plain status-equality filter, same pattern as `pending`). For any
  account that is not Installed and not Cancelled: **Pending** = has a
  `StartDate` (install date) that falls strictly after today; **Needs
  Attention** = everything else — no install date at all, `Scheduled ==
  "Not Scheduled"`, or an install date that is today or earlier. The two
  are mutually exclusive and exhaustive by construction. Date comparison
  is calendar-date-only via `.dt.normalize()` (deliberately not `.dt.date`
  — on some pandas versions `.dt.date` on an all-NaT column silently
  stays `datetime64` dtype instead of converting to `object`, breaking a
  direct comparison against a plain `date`; `normalize()` avoids that by
  staying in `datetime64` throughout and comparing against a
  `pd.Timestamp`), so an install slot later today still counts as
  "today" (Needs Attention), not "future" (Pending).
  `calculate_rep_metrics()`'s "Pending" column count, the Rep Profile's
  clickable Pending metric, the Individual Rep Leaderboard's Pending
  column, and both Bulk Account Views all read from this single `status`
  value — they cannot disagree.
- **Install Rate formula changed 2026-08-17 (by request)**: no longer
  `installs / (installs + cancels)`. It's now `1.0 - (needs_attention /
  total_sales)` (scaled to a percentage for display, same as every other
  rate in this app). `needs_attention` is always ≤ `total_sales` for a
  given rep (it's a subset of that rep's own rows), so the result always
  stays in the 0–100% range.
- **Cancel Rate and Cancelled removed from the UI entirely, 2026-08-17
  (by request, same day)**: the Individual Rep Leaderboard's Cancel Rate
  column and the Rep Profile's Cancelled + Cancel Rate metric cards
  (`.td-profile-metrics-secondary`, now 3 cards — Inbound, Outbound,
  Pending — not 5) are gone. `calculate_rep_metrics()` in
  `sales_metrics.py` still internally computes `cancels`/`cancel_rate`
  per rep (left in place rather than deleted, since the underlying
  `status == "Cancelled"` classification and Bulk Account View badge
  color are untouched — only the two display surfaces were removed) —
  don't be surprised these fields still exist in the row dict even
  though nothing currently renders them.
- Two more view keys exist *outside* the registry: `channel` and `date`
  (added 2026-08-16, for `SalesByChannelChart` bar clicks and Sales
  Activity calendar day clicks). They're handled by
  `get_channel_account_view()` / `get_calendar_day_account_view()` in
  `sales_metrics.py` rather than a `BULK_ACCOUNT_VIEWS` entry, because
  the channel/date is a runtime value from the click (`?channel=...` /
  `?year=&month=&day=`), not a fixed filter known at import time like
  every registry entry's filter is. `date` is rep-scoped only (the Rep
  Profile's Sales Activity calendar, resolved in `bulk_account_view()`
  in `app.py`) — the Team Leaderboard's Sales Calendar isn't clickable.
  `channel` is wired into **both** `bulk_account_view()` (rep-scoped, for
  the Rep Profile's Channel Performance chart) and `all_sales_view()`
  (team-wide, `rep=None`, for the Team Leaderboard's own Channel
  Performance chart, added 2026-08-16) via the same `if view ==
  "channel" / ... / in BULK_ACCOUNT_VIEWS` branch pattern in both route
  handlers — the 3 registry entries above are otherwise unaffected in
  either route.
- `get_bulk_account_view(df, rep, view, period, start, end)` is the one
  function every feature should call to resolve a view key into
  `(title, accounts)`. It handles rep scoping (pass `rep=None` for a
  team-wide, not rep-scoped view), period filtering (unless the view
  opts into `all_time`), and shaping via `build_bulk_account_rows()`.
- Three things currently render through this component: the standalone
  rep-scoped `/dashboard/reps/<rep_name>/accounts?view=<key>` page
  (`bulk_account_view()` in `app.py` — reached today from the Rep
  Profile's clickable Pending metric); the team-wide
  `/dashboard/accounts?view=<key>` page (`all_sales_view()` in `app.py`,
  added 2026-08-14 — reached from the "View All Sales" button in the
  top-right corner of the Latest Accounts Sold card in the Live Sales
  Activity section (renamed from "Last 3 Accounts Sold" / "Planet
  Networks Records" 2026-08-16), defaults to `view=all_sales` and
  `period=today` so it opens on "what happened today"); and the Rep
  Profile's inline Needs Attention tab, which includes the partial
  directly rather than duplicating its markup. All three share
  `templates/bulk_account_view.html` — it renders a period-filter form
  (hidden when the resolved view's `all_time` is true) and a "Back to
  {rep}" link when `rep_name` is set, or "Back to Dashboard" when it's
  not (team-wide views pass `rep_name=None`).

**Architectural rule:** when new functionality needs to display multiple
customer accounts, check whether `get_bulk_account_view()` /
`BULK_ACCOUNT_VIEWS` can already handle it before writing anything new.
Extend `build_bulk_account_rows()` if a field is missing (e.g. "add phone
number") — that one change then applies everywhere the component is
used. Do not create a duplicate account-list component just because the
source metric or filter differs.

- **Needs Attention Workflow controls** (added 2026-08-17, see its own
  section above) are the one exception to "the component stays visually
  identical everywhere it's used" — `_bulk_account_view.html` conditionally
  includes `_attention_overview.html`/`_attention_controls.html`, but only
  when `view == 'needs_attention'` (true for the rep-scoped standalone
  page, the team-wide standalone page, and the Rep Profile's inline tab —
  false for every other view: Pending, All Sales, and the channel/metric/
  date drill-downs). This is a template-level `{% if %}`, not a second
  component — the underlying card/table markup, sorting, and Cards/Table
  toggle are unchanged and shared by every view exactly as before.

### Chart Card

> The standardized shell for a single Chart.js visualization, used by
> every chart on both the Rep Profile and Team Leaderboard pages (added
> 2026-08-16).

This app has no component framework (no React, no build step) — a
"chart component" here means a Jinja partial + `data-*` attributes +
a small vanilla-JS factory function, the same pattern
`_bulk_account_view.html` already established for account lists:

- `templates/_chart_card.html` is the shared card shell — title,
  optional subtitle, and either a `<canvas>` or an empty-state message
  (`chart_empty`/`chart_empty_message`, so a rep/period with zero sales
  never renders a broken or fabricated chart). Set `chart_id`,
  `chart_title`, `chart_empty`, `chart_empty_message` (and optionally
  `chart_subtitle`, `chart_canvas_attrs`) via Jinja `set` statements
  before including it — reset any of these explicitly before a second
  `include` in the same template, since Jinja `set` persists across the
  whole template rather than being scoped to the include.
- `static/js/charts.js` is the shared JS module (loaded after the
  Chart.js v4.4.4 CDN script, now on both `rep_profile.html` and
  `dashboard.html`): `getChartTokens()` reads the `--td-chart-*` /
  `--td-accent-*` CSS custom properties from `team_dashboard.css` so
  every chart automatically follows the current theme; `buildTooltipConfig()`
  gives every chart the same tooltip chrome; `createMonthlySalesChart()`,
  `createMonthlySalesTrendChart()`, `createChannelChart()`,
  `createHourlyChart()` are the four chart factories, each reading its
  data from `data-*` attributes on its own `<canvas>` (rendered
  server-side via Jinja `|tojson` — no separate JSON endpoint).
  `createChannelChart()` and `createHourlyChart()` are fully generic —
  the Team Leaderboard's team-wide Channel Performance / Time-of-Day
  Performance charts call the *exact same* factory functions as the Rep
  Profile's rep-scoped ones, just pointed at a different canvas id and
  different `data-*` payload (team-wide vs. rep-scoped counts). Only
  the trend line (`createMonthlySalesTrendChart()`, month-granularity,
  many months) is distinct from the per-rep `createMonthlySalesChart()`
  (day-granularity, one month) — they answer different questions and
  read different backend functions (`calculate_monthly_sales_trend()`
  vs. `calculate_rep_monthly_activity()`).
- `templates/_channel_hourly_toggle.html` (added 2026-08-16) is a second
  shared partial for the specific case of two charts sharing one row via
  a segmented-control toggle — currently the Channel & Time-of-Day
  Performance section on both pages. It renders both canvases (or each
  one's own empty-state message) up front; `wireChartToggle()` in
  `charts.js` shows one panel at a time and creates each chart lazily on
  first activation (a canvas created while `display:none` sizes to 0 in
  Chart.js and won't self-correct, so the default-active tab's chart is
  created immediately on page load and the other is deferred until its
  first click). `wireChartToggle()` is generic — it maps each canvas's
  `data-chart-type` attribute to a factory via `CHART_TOGGLE_FACTORIES`,
  so adding a third toggle pair later means adding one entry to that map,
  not a new toggle handler.
- Currently used by the Rep Profile's Sales Activity and Channel &
  Time-of-Day Performance sections, and the Team Leaderboard's Monthly
  Sales Trend and Channel & Time-of-Day Performance sections (see "Rep
  Profile Page — Section Names" and "Team Leaderboard Page — Section
  Names" above) — the same `_chart_card.html` / `_channel_hourly_toggle.html`
  + `charts.js` pair should be reused for any future chart rather than
  hand-rolling new canvas/tooltip/token-reading code.

## What's intentionally not built yet

User authentication, sales rep login, admin portal, scheduled ETL,
email/SMS reports, and CRM integration are all out of scope for this
phase. See code comments in `app.py` and `queries.py` for notes on the
future data model (linking `main_sales`, `vision_packages`, and
`service_cancellations` via subscriber UUID) once that relationship is
confirmed.

**Database writes** are no longer entirely out of scope — the Needs
Attention Workflow (added 2026-08-17, see its own section above) writes
to `appdb`, an app-owned Postgres database created specifically for it.
**PlanetWeb SQL Server and KPI PostgreSQL remain strictly read-only** —
nothing in this codebase executes anything but `SELECT` against either of
those two. `created_by`/`updated_by` on that feature's tables are
freeform text today (a "Your name" field, cached in the browser's
`localStorage`) rather than tied to a real user account, specifically so
real authenticated users can be wired in later without a schema change —
see "Needs Attention Workflow" → Persistence.
