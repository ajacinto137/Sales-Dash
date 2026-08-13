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

## 1. Prerequisites

- Docker Desktop (that's it — no Python or ODBC driver install needed on
  the Mac for the Docker workflow)

## 2. Configure credentials

Open the `.env` file in this folder and enter the four database
credential values:

```env
PLANETWEB_USERNAME=
PLANETWEB_PASSWORD=

KPI_USERNAME=
KPI_PASSWORD=
```

These are the **only four values** you should need to manually enter.
Everything else (hosts, database names, ports, driver name, SSL settings,
and the queries themselves) is already filled in.

`.env` is excluded from Git via `.gitignore` — do not remove it from
there, and do not commit `.env` once it contains real credentials.
`.env.example` is the template that's safe to commit.

## 3. Start

```bash
docker compose up --build
```

This builds the image (installing ODBC Driver 18 for SQL Server and all
Python dependencies inside the container), then starts Flask on
`0.0.0.0:3005` inside the container, mapped to your Mac at `3005:3005`.

## 4. Open

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

## 5. Stop

```bash
docker compose down
```

## Other useful commands

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

## What's intentionally not built yet

User authentication, sales rep login, admin portal, database writes,
scheduled ETL, email/SMS reports, CRM integration, and production
deployment are all out of scope for this phase. See code comments in
`app.py` and `queries.py` for notes on the future data model (linking
`main_sales`, `vision_packages`, and `service_cancellations` via
subscriber UUID) once that relationship is confirmed.
