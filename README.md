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

## What's intentionally not built yet

User authentication, sales rep login, admin portal, database writes,
scheduled ETL, email/SMS reports, and CRM integration are all out of
scope for this phase. See code comments in `app.py` and `queries.py`
for notes on the future data model (linking `main_sales`,
`vision_packages`, and `service_cancellations` via subscriber UUID)
once that relationship is confirmed.
