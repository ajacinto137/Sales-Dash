# Planet Networks — Sales Performance Dashboard

An internal Flask + pandas dashboard that replaces the Excel/VBA sales
reporting workflow. It connects to the PlanetWeb SQL Server database and
the KPI PostgreSQL database, runs the same three queries currently used by
Excel/VBA, and displays the results in a web dashboard.

The entire application runs in Docker — the SQL Server ODBC driver is
installed **inside the container**, so you do not need to install it on
your Mac to use this project.

This application is **read-only**. It only ever executes `SELECT`
statements against both source databases.

## Stack

**Backend**
- [Flask](https://flask.palletsprojects.com/) (Python) — web app and routes (`app.py`)
- [pandas](https://pandas.pydata.org/) — data wrangling and metrics calculations (`sales_metrics.py`)
- [pyodbc](https://github.com/mkleehammer/pyodbc) + Microsoft ODBC Driver 18 — connects to the PlanetWeb **SQL Server** database
- [psycopg2](https://www.psycopg.org/) — connects to the KPI **PostgreSQL** database
- [python-dotenv](https://github.com/theskumar/python-dotenv) — loads credentials from `.env`
- Raw SQL queries defined in `queries.py`, DB connection/health-check helpers in `db.py`

**Frontend**
- Server-rendered [Jinja2](https://jinja.palletsprojects.com/) templates (`templates/`)
- Vanilla JavaScript (`static/js/dashboard.js`, `static/js/team_dashboard.js`) — no frontend framework or build step
- [Chart.js](https://www.chartjs.org/) (v4, via CDN) — sales-over-time chart
- Plain CSS (`static/css/dashboard.css`, `static/css/team_dashboard.css`)

**Infrastructure**
- Docker / Docker Compose — the entire app, including the SQL Server ODBC driver, runs in a container (`Dockerfile`, `docker-compose.yml`)
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

Copy the template and fill in the four database credential values:

```bash
cp .env.example .env
```

```env
PLANETWEB_USERNAME=
PLANETWEB_PASSWORD=

KPI_USERNAME=
KPI_PASSWORD=
```

These are the **only four values** you should need to manually enter.
Everything else (hosts, database names, ports, driver name, SSL settings,
and the queries themselves) is already filled in.

`.env` is excluded from Git via `.gitignore` — never commit it once it
contains real credentials. `.env.example` is the tracked template that's
safe to commit; keep it in sync when you add new variables.

### 3. Start

```bash
docker compose up --build
```

This builds the image (installing ODBC Driver 18 for SQL Server and all
Python dependencies inside the container), then starts Flask on
`0.0.0.0:3005` inside the container, mapped to your Mac at `3005:3005`.

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
`static/css/team_dashboard.css`) has four standing sections, in this page
order. These are the canonical names to use when referring to a section of
this page — they're also marked with matching HTML comments in the
template itself.

| Name | Aka | What it is | Template markup |
|------|-----|------------|------------------|
| **Team Overview** | | Team-wide KPI cards: Total Sales, Installed, Pending, Cancelled, Team Install Rate, Monthly Forecast (Chargebacks was removed 2026-08-14) | `<section class="td-kpi-bar">` |
| **Sales Calendar** | | Navigable month-by-month calendar of team total sales per day | `<section class="td-section">` containing `<h2>Sales Calendar</h2>` |
| **Planet Networks Records** | "Records Section" | All-time single-rep bests (Best Days/Weeks/Months Ever) plus live top-3 rep leaderboards for the ongoing day/week/month/year (Daily/Weekly/Monthly/Yearly Sales) | `<section class="td-section">` containing `<h2>Planet Networks Records</h2>` |
| **Individual Rep Leaderboard** | "Rep Leaderboard", "Leaderboard Table", "Rep Performance Section" | The rep search field plus the ranked table (Rank, Sales Rep, Sales, Outbound, Inbound, Installs, Pending, Cancels, Install Rate, Cancel Rate) — every column header is clickable to sort the table by that column | `<h2>Individual Rep Leaderboard</h2>` + `<form class="td-search-form">` + `<section class="td-table-card">` |

Column sorting (`?sort=<column>&dir=asc|desc`) is a pure display-ordering
concern applied after search/selection, via `sort_rep_rows()` in
`sales_metrics.py` — it reorders the rows `calculate_rep_metrics()`
already computed and never recalculates anything or changes which rep is
auto-selected. The `#`/Rank column's *value* always reflects a rep's real
sales-based standing regardless of which column the table is currently
sorted by; sorting by `rank` (the default) just restores that original
order. Rates with no denominator (`None`, shown as "—") always sort to
the bottom in either direction.

Team Overview and the Individual Rep Leaderboard both read from the same period-filtered
dataset (`period_df` in `dashboard_page()`, `app.py`), so a date-range
change affects them together. Planet Networks Records, the Sales
Calendar, and the Monthly Forecast KPI are the exceptions — they always
use the all-time dataset regardless of the selected date range (see
`calculate_records()`, `calculate_calendar_sales()`, and
`calculate_monthly_forecast()` in `sales_metrics.py`). The Sales Calendar
has its own independent month navigation (`cal_year`/`cal_month` query
params, defaulting to the current month) that doesn't affect the period
filter or any other section.

Note: there used to be a "Sales by Channel" section (per-rep breakdown
across the 8 raw channels) between Planet Networks Records and Rep
Leaderboard — it was removed by request and must not be reintroduced on
this page without being asked again. The underlying channel data
(`SALES_CHANNELS`, `INBOUND_CHANNELS`/`OUTBOUND_CHANNELS` in
`sales_metrics.py`) is still intact and still powers the Rep
Leaderboard's Inbound/Outbound columns. `calculate_channel_breakdown()`
was later reintroduced, but scoped to a single rep for the Rep Profile's
Overview tab (see below) — it does not power anything on this page.

The Monthly Forecast is a simple run-rate projection: month-to-date sales
÷ days elapsed this month × days in the month. It always projects the
actual current calendar month, not whatever month the Sales Calendar is
currently browsing to.

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
| **Overview** | Metric cards for Total Sales, Installed, Install Rate (primary) and Inbound, Outbound, Pending, Cancelled, Cancel Rate (secondary), plus a Sales by Channel breakdown (Inbound/Outbound columns). All period-filtered. |
| **Needs Attention** | Accounts for this rep where the Scheduled Install Date is strictly before today and the account is still Not Yet Installed. Always uses the full all-time dataset regardless of the page's period filter — same reasoning as Planet Networks Records / Sales Calendar / Monthly Forecast on the Team Leaderboard. Rendered via the shared Bulk Account View component (see below). |

The Overview tab's metric cards reuse `calculate_rep_metrics()` — the
exact same function/output that produces the Individual Rep Leaderboard table rows
— so the two pages can never disagree for the same period. The Needs
Attention tab's count is shown directly in the tab label
(`Needs Attention (N)`), with a subtle warning treatment when `N > 0`.

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
  account's First Name, Last Name, Address, Scheduled Install Date,
  status/category badge, and a Vision link (new tab, `noopener
  noreferrer`, built from `vi_subscriber_uuid` via `build_vision_url()`,
  omitted rather than broken when the UUID is missing).
- `BULK_ACCOUNT_VIEWS` (`sales_metrics.py`) is the registry mapping a
  view key to a title and a filter function over the normalized dataset.
  Currently defined: `pending` (period-filtered) and `needs_attention`
  (all-time). **Adding a new Bulk Account View — e.g. "show Installed
  accounts" or "show Door to Door sales" — means adding one entry to this
  registry, not building a new list UI.**
- `get_bulk_account_view(df, rep, view, period, start, end)` is the one
  function every feature should call to resolve a view key into
  `(title, accounts)`. It handles rep scoping, period filtering (unless
  the view opts into `all_time`), and shaping via
  `build_bulk_account_rows()`.
- Two things currently render through this component: the standalone
  `/dashboard/reps/<rep_name>/accounts?view=<key>` page
  (`bulk_account_view()` in `app.py`, `templates/bulk_account_view.html`
  — reached today from the Rep Profile's clickable Pending metric), and
  the Rep Profile's inline Needs Attention tab, which includes the same
  partial directly rather than duplicating its markup.

**Architectural rule:** when new functionality needs to display multiple
customer accounts, check whether `get_bulk_account_view()` /
`BULK_ACCOUNT_VIEWS` can already handle it before writing anything new.
Extend `build_bulk_account_rows()` if a field is missing (e.g. "add phone
number") — that one change then applies everywhere the component is
used. Do not create a duplicate account-list component just because the
source metric or filter differs.

## What's intentionally not built yet

User authentication, sales rep login, admin portal, database writes,
scheduled ETL, email/SMS reports, and CRM integration are all out of
scope for this phase. See code comments in `app.py` and `queries.py`
for notes on the future data model (linking `main_sales`,
`vision_packages`, and `service_cancellations` via subscriber UUID)
once that relationship is confirmed.
