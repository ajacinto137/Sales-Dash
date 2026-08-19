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
- [psycopg2](https://www.psycopg.org/) — connects to the KPI **PostgreSQL** database (read-only) and the app-owned **appdb** PostgreSQL database (read/write, see "Needs Attention Workflow" and "Authentication, Roles & Admin Portal")
- [python-dotenv](https://github.com/theskumar/python-dotenv) — loads credentials from `.env`
- [openpyxl](https://openpyxl.readthedocs.io/) — reads `.xlsx` for the Admin Portal's user import (`import_service.py`); pandas needs it as an engine, it's not used directly
- [pytest](https://pytest.org/) — this app's first test suite (`tests/`), added 2026-08-18 alongside authentication
- Raw SQL queries defined in `queries.py`, DB connection/health-check helpers in `db.py`
- No ORM anywhere in this codebase — every appdb-backed module (`attention_store.py`, `user_store.py`, `needs_attention_service.py`) talks to Postgres with raw parameterized `psycopg2` SQL. `db_migrations.py` is a small hand-rolled, versioned migration runner (see "Authentication, Roles & Admin Portal" → Database Models) rather than Alembic/SQLAlchemy, to stay consistent with that
- `attention_store.py` — the Needs Attention workflow's persistence layer (Attention Status + notes, now also the audit log — see below)
- `auth.py` / `user_store.py` / `permissions.py` / `email_service.py` / `import_service.py` / `needs_attention_service.py` — authentication, user/role/Sales-Rep-mapping persistence, the one authoritative Needs Attention permission rule, transactional email, Excel import, and `needs_attention_since` tracking, respectively (added 2026-08-18, see "Authentication, Roles & Admin Portal")

**Frontend**
- Server-rendered [Jinja2](https://jinja.palletsprojects.com/) templates (`templates/`, `templates/admin/`)
- Vanilla JavaScript (`static/js/dashboard.js`, `static/js/team_dashboard.js`, `static/js/attention.js`, `static/js/search.js`, `static/js/admin_users.js`, `static/js/admin_import.js`) — no frontend framework or build step
- [Chart.js](https://www.chartjs.org/) (v4, via CDN) — sales-over-time chart
- Plain CSS (`static/css/dashboard.css`, `static/css/team_dashboard.css`)
- `static/images/` — badge icons for the Individual Sales Profile's Achievements (see its own section below)

**Infrastructure**
- Docker / Docker Compose — the entire app, including the SQL Server ODBC driver, runs in a container (`Dockerfile`, `docker-compose.yml`)
- A second container, `appdb` (`postgres:16-alpine`, its own named volume) — the app-owned database backing the Needs Attention workflow **and** authentication/users/roles, entirely separate from the two read-only source databases
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

SECRET_KEY=
```

The first four are the source-database credentials your team already
uses. Everything else about them (hosts, database names, ports, driver
name, SSL settings, and the queries themselves) is already filled in.

`APPDB_PASSWORD` is different in kind: it's not an existing credential to
look up anywhere — it sets the password for a brand-new, empty Postgres
database (`appdb` in `docker-compose.yml`) that this app creates for
itself on first `docker compose up`, used by the Needs Attention workflow
and by authentication/user management (see "Needs Attention Workflow"
and "Authentication, Roles & Admin Portal" below). Any value works;
there's nothing to "get right" beyond picking one.

`SECRET_KEY` signs the login session cookie — **required**, the app
refuses to start without it (see "Authentication, Roles & Admin Portal").
Generate one with:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

`SMTP_*` (see `.env.example`) are optional — leave `SMTP_HOST` blank for
local dev and setup/reset links print to the container logs instead of
emailing (see "Authentication, Roles & Admin Portal" → Email
Configuration).

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

Note: `/health` itself never triggers a data load — `planetweb_connected`/
`kpi_connected`/`last_refreshed` are whatever the last real page load (or
manual "Refresh Data" click) already populated, and are `null` before
that has happened once.

### Data loading

Every full-page request re-queries both source databases (`ensure_data_loaded()`
in `app.py`, changed 2026-08-17, by request — previously it loaded once
lazily and served that same in-memory snapshot until someone clicked
"Refresh Data"). This trades request latency for always-current data:
every page view now costs a live PlanetWeb (Main Sales) + KPI (Vision
Packages, Service Cancellations) round trip. The "Refresh Data" button
(`POST /refresh`) still works the same way; it's just redundant with what
a plain page load already does now.

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

## Authentication, Roles & Admin Portal

> Real login, four roles, an Admin Portal, Excel-based user import, and
> a 15-day ownership-protection rule for who can work someone else's
> Needs Attention accounts — added 2026-08-18. Before this, the entire
> dashboard was open to anyone with the URL; every route except
> `/health` now requires being logged in, and four existing pages
> (`/overview`, `/main-sales`, `/cancellations`, `/vision-packages`) plus
> everything under `/admin` require the **Admin** role specifically.

### Roles

Exactly four, stored on `users.role`: **Admin**, **Sales Rep**,
**Customer Success**, **Other**. These are *authentication/authorization*
roles — not to be confused with any future organizational grouping;
`user_store.ROLES` is the single source of truth for the list, and
`permissions.py` is the only place role names are ever compared against
in a permission decision.

| Role | Can do |
|------|--------|
| **Admin** | Everything — every dashboard page, the 4 legacy Admin-only pages, the whole Admin Portal (user management, role/rep mapping, Excel import, sending setup/reset emails, the audit log), and works any Needs Attention account regardless of owner or age. |
| **Customer Success** | Normal dashboard access; can work *any* Needs Attention account immediately, regardless of who owns it or how long it's been in Needs Attention. No Admin Portal access. |
| **Sales Rep** | Normal dashboard access; can immediately work their own accounts and any account with no owning Sales Rep (i.e. Customer-Success/Other-attributed); can only work *another* Sales Rep's account once it's been in Needs Attention 15+ full days. No Admin Portal access. |
| **Other** | Normal dashboard *read* access; conservative default — cannot work any Needs Attention account at all, regardless of ownership or age (spec was explicit: never let Other quietly inherit Sales Rep permissions). No Admin Portal access. |

### User Records vs. Sales Rep Records — deliberately separate

A **Sales Rep** (`sales_reps` table) is not, and has never been, the same
thing as a **User** (`users` table) — this was true even before
authentication existed: `sales_rep` in the normalized dataset
(`sales_metrics.py`) has always been a name string derived from
`SourceToken`, not tied to any login. A rep can rack up sales and show up
on the Individual Rep Leaderboard having never logged in or received an
invitation; `sales_reps` rows are auto-synced from live source data
(`user_store.sync_sales_reps()`, called every `load_all_data()` refresh)
with zero manual entry. A `users` row's `sales_rep_id` is an *optional*
mapping onto an already-existing `sales_reps` row — never an identity.
Concretely: creating a user never creates a rep, disabling/deleting a
user never touches that rep's sales attribution, and a rep can exist
indefinitely with zero mapped users. Ownership used for reporting
(Sales/Outbound/Installs/Needs Attention/Install Rate — everything in
`sales_metrics.py`) is always read from the source data directly, never
from `users`/`sales_reps`.

**One deliberate exception:** the Team Leaderboard's Sales Volume tab
(see "Team Leaderboard Page — Section Names" below) only plots reps
currently mapped to a `users` row with role exactly `"Sales Rep"`
(`user_store.list_sales_rep_role_names()`), so an Admin/Customer Success
staffer's own manual entry in the raw data can't clutter a chart meant to
compare actual Sales Reps. This filtering happens in `app.py` before
calling `calculate_sales_volume_trend()` — that function itself stays a
pure function of whatever rows it's handed, with no `users`/`sales_reps`
knowledge of its own, same as everything else in `sales_metrics.py`.
`list_sales_rep_role_names()` returns `None` (not an empty set) on an
appdb outage specifically so `app.py` can fail OPEN — skip the filter and
show every rep — rather than fail closed and silently blank the chart.

### Authentication Flow

Session-based, not JWT/OAuth — Flask's own signed-cookie `session`
(needs `SECRET_KEY`, see "Configure credentials") holds only a
`user_id`; every request re-fetches that user's current row from appdb
(`auth.current_user()`, cached on `flask.g` for that one request only),
so a role change or a disable takes effect on the user's *very next
request*, not next login. No Flask-Login or other auth package — this
stays consistent with the rest of the app's zero-framework-dependencies
philosophy (no ORM, no frontend framework either). Passwords are hashed
with `werkzeug.security` (already bundled with Flask, no new dependency)
— `generate_password_hash()`/`check_password_hash()` — and a plaintext
password is never stored, logged, or emailed anywhere.

- `POST /login` — email + password. A successful login updates
  `users.last_login_at` (`user_store.record_login()`) and redirects to
  `?next=` if present (validated to be a same-site relative path only —
  an open-redirect guard on a query-string value).
- `POST /logout` — clears the session.
- Disabled users can never log in (`status != 'active'` is checked both
  at login and on every `current_user()` lookup — a session that was
  valid when created is invalidated the moment an Admin disables that
  account, without waiting for the cookie to expire).

### First-Time Admin Setup

`avelino@planet.net` becomes the initial Admin with **no manual database
work and no routine `.env` editing** — see `app._bootstrap_initial_admin()`
in `app.py`, called from `load_all_data()` (the same lazy-init pattern
`ensure_data_loaded()` already used for source data):

1. The first time this process ever finds no user matching
   `INITIAL_ADMIN_EMAIL` (env var, defaults to `avelino@planet.net`), it
   creates one with `role=Admin`, `status=pending`, no password.
2. Generates a secure random single-use setup token
   (`user_store.create_token()` — `secrets.token_urlsafe(32)`, only its
   sha256 hash stored, expires in 72 hours) and emails a setup link via
   `email_service.send_setup_email()`.
3. **If SMTP isn't configured yet** (a fresh deployment, before anyone's
   filled in the `SMTP_*` vars) — the normal state right after first
   deploy — the email "send" fails gracefully and **the setup link is
   printed to the container's stdout logs** (`docker compose logs`)
   instead. This is the actual mechanism that means you're never
   blocked getting into your own dashboard waiting on email to be live.
4. Click the link (`GET /setup/<token>`), set a password, and you're
   logged in immediately with the Admin role already assigned.

Runs at most once per process; a still-pending admin gets the *current*
valid link re-logged (never re-emailed) on every later restart before
setup is finished, so a restart mid-setup doesn't spam a real inbox but
also never leaves you stuck. Every *other* user's setup email is
Admin-triggered only, from the Admin Portal — this one bootstrap
exception is what makes the very first login possible at all.

### First-Time Account Setup (everyone else) & Password Reset

Both flows share one mechanism — `account_setup_tokens`
(`purpose='setup'` or `'reset'`), a random token whose sha256 hash is
stored (raw token exists only in the emailed URL, never persisted),
single-use (`used_at` set only after the password is *actually* changed
— a failed submit doesn't burn a valid link), and expiring (72h for
setup, 24h for reset).

- **Setup**: an Admin clicks "Send Setup Email" / "Resend Setup Email"
  for a `pending` user in `/admin/users` → `email_service.send_setup_email()`
  → `GET/POST /setup/<token>` → set password → auto-logged-in,
  `status` flips to `active`. **Never automatic** — importing users via
  Excel (below) never sends an email by itself; an Admin has to
  explicitly trigger it per user, per the spec.
- **Reset**: `GET/POST /reset-password` (request by email) →
  `email_service.send_reset_email()` → `GET/POST /reset-password/<token>`
  (set new password). The request step always shows the same
  confirmation regardless of whether the email matched a real account —
  this form must never be usable to enumerate which emails have
  accounts. An Admin can also trigger this directly from `/admin/users`
  ("Send Reset") without the user having to ask.
- **Set Password directly** (added 2026-08-18) — `POST
  /admin/users/<id>/set-password` — an Admin types a password for a user
  right there in `/admin/users` and relays it out of band (phone, in
  person), bypassing the token/email flow entirely. Added specifically
  for when SMTP isn't configured yet (see "Email Configuration" below):
  the setup/reset emails still work in that state, they just log their
  link to stdout instead of sending — this gives an Admin a way to get
  someone in immediately without touching server logs at all. Still goes
  through `user_store.set_password()` (hashed immediately, same as every
  other path — never stored or logged in plaintext) and still activates
  the account. The one deliberate UI difference from the self-service
  forms: this field is `type="text"`, not `type="password"`, since the
  Admin needs to read back what they typed to tell the user.

### Needs Attention Ownership & the 15-Day Rule

**The account owner never changes.** Working, classifying, or resolving
a Needs Attention account never moves it to the acting user's book of
business and never rewrites the original Sales Rep's attribution —
`sales_metrics.py`'s `sale_rep` column (and everything computed from it:
Sales/Outbound/Installs/Needs Attention count/Install Rate) is untouched
by anything in this section. Three concepts stay separate, tracked
independently:

- **Account owner** — whoever `sales_metrics.py` already attributes the
  sale to. Read fresh from source data on every check, never stored by
  this feature.
- **Acting user** — whoever actually performed a write (set/changed
  Attention Status, added a note). `account_attention_notes.acting_user_id`.
- **Resolved by** — implicit in the notes history: whichever note last
  set/changed the Attention Status is, by definition, who most recently
  acted on it. No separate "resolved by" column was needed — the
  append-only note history already answers that.

**`needs_attention_since`** (spec's exact suggested name) didn't exist
before this — Needs Attention status is recomputed fresh from source
data on every single request, so there was previously no durable answer
to "how long has this account actually been in Needs Attention."
`needs_attention_tracking` (one row per currently-Needs-Attention
`sale_id`, `first_seen_at` set once and never overwritten) is that
answer, synced every `load_all_data()` refresh
(`needs_attention_service.sync_tracking()`): a `sale_id` newly seen in
Needs Attention gets a fresh `first_seen_at`; one that's left (installed,
reclassified) has its row deleted, so a *later* re-entry starts a
genuinely fresh 15-day clock rather than inheriting a stale timestamp.
The 15-day threshold (`needs_attention_service.AGE_THRESHOLD_DAYS`) is
compared as **exact elapsed time, not calendar-date rounding** — "15
full days" per the spec.

**The one authoritative permission function** —
`permissions.can_work_account(user, owner_rep_name, needs_attention_since)`
→ `(allowed: bool, reason: str | None)` — is called from exactly one
place server-side, `app._authorize_account_action()`, itself called by
both `POST /dashboard/attention/<sale_id>/status` and
`.../notes` *before either ever touches `attention_store`*. The same
function also computes `can_work`/`cannot_work_reason` per account in
`app.attach_attention_metadata()`, which the Needs Attention Bulk
Account View uses to proactively grey out Classify/Add Note with the
exact reason (spec's own example wording: *"This account belongs to
another Sales Rep and has only been in Needs Attention for 8 days. It
becomes available to other Sales Reps after 15 days."*) — but that's a
courtesy, never the real protection. A rejected write returns a real
`403` with that same message as JSON, not a silently-hidden button; there
is no second copy of the 15-day rule anywhere in the codebase to drift
out of sync with this one.

Rules, in order (see `permissions.py` for the exact implementation):
1. No user, or a disabled/non-active user → never.
2. Admin → always, any account, any age.
3. Customer Success → always, any account, any age.
4. Sales Rep:
   - Account has no owning Sales Rep (Customer-Success/Other-attributed)
     → always.
   - Account's owner IS this user's own mapped rep → always.
   - A *different* Sales Rep owns it → only once
     `needs_attention_since` is 15+ full days old.
5. Other → never (conservative default — never inherits Sales Rep
   permissions by accident).

### Admin Portal

`/admin` (redirects to `/admin/users`) and everything under it —
`templates/admin/`, reusing `team_dashboard.css`'s tokens/cards/table
styling and the shared `_topnav.html` throughout, no separate visual
language. Admin-only (`@auth.admin_required` on every route in this
section) — a non-admin hitting any of these URLs directly gets a real
`403` (`templates/unauthorized.html`), not just a hidden nav link.

**Admin Portal Navigation** (`templates/_admin_nav.html`, added
2026-08-19) — one reusable nav partial, included at the top of every
Admin Portal page, replacing three near-identical hand-copied
`<nav class="td-admin-tabs">` blocks that used to live separately in
`admin/users.html`/`admin/import.html`/`admin/audit.html`. Two labelled
pill-tab groups:
- **Admin Tools** — User Management, Sales Reps, Import Users, Audit Log.
- **Data Tools** — Overview, Main Sales, Service Cancellations, Vision
  Packages. **Moved here 2026-08-19** from the main top nav
  (`_topnav.html`) — by request, since the main nav is meant for daily
  sales-dashboard navigation, not admin/data tooling. This is a UX
  reorganization only: every one of these 4 routes already had, and
  still has, its own `@auth.admin_required` in `app.py` — moving a link
  never changes what's actually protected (see "Authorization" below).
  Their own page shell (`templates/base.html` + `static/css/dashboard.css`,
  a separate, older light-theme layout predating the dark `team_dashboard.css`
  redesign) is intentionally left as-is rather than reskinned — an
  unrequested visual overhaul of 4 working data pages — but now carries a
  "← Admin Portal" link back into the rest of the portal.

Each tab highlights via `active_page`, one distinct value per route
(`admin_users`/`admin_sales_reps`/`admin_import`/`admin_audit`/
`overview`/`main_sales`/`cancellations`/`vision_packages`) — add a new
Admin Portal tool by adding one route with its own `active_page` value
and one more `<a>` in `_admin_nav.html`, not a new nav system.

**Sales Reps** (`/admin/sales-reps`, `templates/admin/sales_reps.html` +
`static/js/admin_sales_reps.js`, added 2026-08-19) — one row per
`sales_reps` record (every rep who has ever sold, regardless of whether
they have a `users` login), with a **Team** `<select>`
(Junior/NJ - Sales Reps/NY - Sales Reps/VA - Sales Reps, or
"— Unassigned —") that auto-saves on change — no Edit/Save/Cancel
toggle, since there's exactly one editable field per row. Success
re-renders the team pill and shows a transient "Saved" status in place;
failure reverts the `<select>` to its last-saved value and shows the
server's error message, both without a page reload. See "Sales Rep
Roles / Teams" under "Database Models" above for the full model and what
changing a team does/doesn't affect.

**User Management** (`/admin/users`, `templates/admin/users.html`) — one
table, columns per the spec: Name/Rep, Email, Role, Sales Rep mapping,
Account Status (Pending / Never Logged In · Active · Disabled), Last
Login, Last Needs Attention Activity, current Needs Attention count,
Needs Attention accounts 15+ days old, actions in the last 7/30 days,
and a **Needs Review** flag. Inline edit (email/role/rep mapping, no
page reload — `static/js/admin_users.js`, same fetch-and-update-in-place
idiom as `attention.js`/`search.js`) plus row actions: disable/enable,
send setup email, send password reset. **Never automatic** — creating or
importing a user never emails them; an Admin has to explicitly click
"Send Setup Email"/"Send Reset" per the spec.

The **Needs Review** flag (`app._needs_review_status()`) is deliberately
isolated in one small function so its threshold is trivial to retune
later without touching the template: a Sales Rep/Customer Success user
with open Needs Attention accounts and no meaningful activity in 3+ days
shows "Needs Review"; otherwise "Active". Admin/Other are never judged
by this. "Meaningful activity" (`users.last_needs_attention_activity_at`,
updated by `user_store.record_needs_attention_activity()`) is set only
after a Needs Attention write actually **succeeds** — setting/changing
Attention Status or adding a note — never for simply opening/viewing an
account.

**Excel Import** (`/admin/users/import`, `templates/admin/import.html` +
`static/js/admin_import.js`, backend in `import_service.py`) — a 5-step
flow (Select → Validate → Preview → Import → Results, each its own
visible stepper state):
- Required columns, exactly: **Rep Name**, **Email**, **Group**.
  `.xlsx` only (`.xls` intentionally out of scope — `.xlsx` was the
  hard requirement, `.xls` needs a different library/dependency for
  marginal benefit).
- The Upload/Import button stays disabled until
  `POST /admin/users/import/validate` (multipart) comes back
  structurally valid — right columns, non-empty, readable — showing
  either `File Valid — N rows ready to import.` or the specific
  structural reason it can't be (missing column, empty workbook,
  unsupported file type, unreadable/corrupt file).
- **Preview** table, one row per spreadsheet row: Rep Name, Email,
  Group, Matching Dashboard Rep, and a **Proposed Action** — `Create
  User` / `Update Existing User` / `Needs Review` / `Cannot Import`.
  Per-row validation: required fields non-blank, valid email format,
  Group is one of Sales Rep/Customer Success/Other, no duplicate email
  *within the file*. Rep-name matching (`user_store.find_sales_rep_by_name()`,
  exact case-insensitive) only ever applies to **Sales Rep** rows — a
  Customer Success/Other row's Rep Name is just their name, not a claim
  to a dashboard rep identity, so it's never flagged "Needs Review" for
  not matching one. An unmatched Sales Rep name is flagged `Needs
  Review` and imported *without* a rep mapping — **never** silently
  guessed at.
- **Commit** (`POST /admin/users/import/commit`) re-validates every row
  from scratch server-side — it never trusts the client-held preview,
  only the raw Rep Name/Email/Group triples — and writes row-by-row in
  independent operations, so one bad row is reported and skipped, never
  blocking the good rows around it. Results: `{added, updated,
  needs_review, failed: [...]}`, each failure with its row number, Rep
  Name, Email, and the *exact* reason (never a bare "failed") — plus a
  client-side "Download Failed Rows (CSV)" button so they're easy to
  fix and re-upload.
- Admin is never an importable Group value — "Admin should be managed
  separately," per the spec; `user_store.IMPORTABLE_ROLES` is the
  3-value subset of `ROLES` the importer will ever assign.

**Audit Log** (`/admin/audit`, `templates/admin/audit.html`) — reads
straight from `account_attention_notes`, which **is** the Needs
Attention audit log (see next section) rather than a second, largely
duplicate table. Newest first: when, acting user, account owner at the
time, account (`sale_id`), action type (Changed Attention Status / Added
Note), previous → new value, and the note text.

### Database Models

No ORM anywhere in this app (see "Stack") — `db_migrations.py` is a
small, hand-rolled, **versioned** migration runner: a numbered,
forward-only list of SQL blocks (`MIGRATIONS` in that file), each
applied exactly once inside its own transaction, tracked in a
`schema_migrations(version, description, applied_at)` table. Every
appdb-backed module calls `db_migrations.ensure_schema(conn)` before its
first query in a process (passing the *same* connection it's about to
query with, never a second one). Migration 1 is the original
`attention_store.py` schema (relocated, unchanged) so there's one
migration history for the whole app, not two competing systems. Add a
new migration by appending a new `(version, description, sql)` tuple —
never edit an already-shipped migration's SQL after a real database has
recorded that version.

```sql
sales_reps                     -- auto-synced, never hand-created
  id, name UNIQUE, first_seen_at, last_seen_at,
  team NULL                    -- migration 3, 2026-08-19 -- see below

users
  id, email UNIQUE, password_hash NULL, role,
  sales_rep_id NULL -> sales_reps.id,     -- optional mapping, not identity
  status ('pending'|'active'|'disabled'),
  last_login_at, last_needs_attention_activity_at,
  created_at, updated_at

account_setup_tokens           -- covers BOTH first-time setup and password reset
  id, user_id -> users.id, token_hash (sha256, raw token never stored),
  purpose ('setup'|'reset'), expires_at, used_at, created_at

needs_attention_tracking       -- the missing needs_attention_since
  sale_id PK, first_seen_at

account_attention_notes        -- EXTENDED (not replaced) to double as the audit log
  ...existing note/status columns (see "Needs Attention Workflow")...
  + acting_user_id NULL -> users.id
  + owner_sales_rep TEXT          -- snapshot, for audit display only
  + action TEXT DEFAULT 'note'    -- 'status_change' | 'note'
```

`account_attention_notes.sale_id` is deliberately **not** a foreign key
into `account_attention` (it was, briefly, when the Needs Attention
Workflow first shipped — see that section's own note on this) — a
migration in `db_migrations.py` drops that constraint on any database
that still has it, since reclassifying an account to Unclassified
deletes its `account_attention` row, and notes must survive that.
`NeedsAttentionAudit`, as named in the original spec, is this same
extended `account_attention_notes` table, not a separate one — it
already had previous/new status, note text, and a timestamp; these three
new columns were what it was missing to also serve as the audit log.

#### Sales Rep Roles / Teams (`sales_reps.team`, added 2026-08-19)

Every Sales Rep now belongs to one of four **teams** (`user_store.SALES_REP_TEAMS`):

- **Junior**
- **NJ - Sales Reps**
- **NY - Sales Reps**
- **VA - Sales Reps**

This is migration 3 in `db_migrations.py`: a single nullable
`sales_reps.team TEXT` column, validated against `SALES_REP_TEAMS` in
application code only (no DB `CHECK` constraint — same convention
`users.role`/`ROLE_SET` already uses). It lives on **`sales_reps`, not
`users`** — deliberately distinct from `users.role`
(Admin/Sales Rep/Customer Success/Other) above. A rep can rack up sales
and need a team assignment having never been given a `users` row at all
(see "User Records vs Sales Rep Records"), so a `users`-table column
could never cover every rep that needs one.

**No default, no backfill.** An existing rep with no team assigned
stays `NULL` ("Unassigned") until an Admin explicitly sets one —
guessing a team from state/history/anything else was ruled out
on purpose, since a wrong guess would misreport that rep on every
team-filtered view. An unassigned rep simply doesn't appear in *any*
team's Sales Volume Over Time panel until assigned.

**Where it's managed:** Admin Portal → **Sales Reps** tab
(`/admin/sales-reps`, `templates/admin/sales_reps.html` +
`static/js/admin_sales_reps.js`) — see "Admin Portal" below. Each row is
one `sales_reps` record with a team `<select>` that auto-saves on change
(`POST /admin/sales-reps/<id>/team` → `user_store.update_sales_rep_team()`),
no page reload, with the pill re-rendered and a "Saved"/error status
shown in place on success/failure.

**What changing a team does NOT do:** `update_sales_rep_team()` only
ever writes `sales_reps.team`. It never touches `main_sales`/
`vision_packages`/`service_cancellations` rows, `account_attention`,
`account_attention_notes`, or `needs_attention_tracking` — a rep's
historical sales, Needs Attention accounts/counts, Attention Status,
notes, and Install Rate are all computed straight from source data
(`sales_metrics.py`), completely unaware `sales_reps.team` exists.
Reassigning a rep from one team to another is purely an organizational/
reporting change.

**Where it's used:** currently the Team Leaderboard's Sales Volume Over
Time chart (see "Team Leaderboard Page — Section Names" →
**Sales Volume**) — `user_store.list_sales_reps_by_team()` returns
`{team: [rep_name, ...]}` for the team selector's filtering. Reusable
anywhere else a rep-level team/permission grouping is needed later
(dashboard filtering, reporting, etc.) without inventing a second
mechanism — that's the point of storing it on the shared `sales_reps`
row instead of hardcoding team behavior into one chart's code.

### Email Configuration

`email_service.py` — plain `smtplib` (zero new dependency), config via
`SMTP_HOST`/`SMTP_PORT`/`SMTP_USERNAME`/`SMTP_PASSWORD`/`SMTP_FROM_ADDRESS`/
`SMTP_USE_TLS` in `.env` (same pattern as every other credential in this
app), plus `APP_BASE_URL` for building absolute links
(`https://sales.planet.net/setup/<token>` in production). Isolated
behind exactly three functions (`send_setup_email()`, `send_reset_email()`,
and the shared `_send()`) so swapping the transport later (a provider
API, a queue) never touches `auth.py`, `user_store.py`, or any route —
they only ever call these three names. **`SMTP_HOST` left blank is a
supported, working state** (the default for local dev) — every send
gracefully fails and logs the actual link to stdout instead of raising,
which is what makes the initial Admin bootstrap work before email is
configured. Never sends a password, ever — only a single-use link.

### Authorization (server-side, not just hidden nav items)

Every protected route checks the *server-side* session on every
request — nothing here ever trusts a client-supplied user ID, role, or
rep ID:
- `@auth.login_required` — any active, logged-in user. On every
  `/dashboard*` route, `/refresh`, `/search`.
- `@auth.admin_required` — Admin role specifically. On `/overview`,
  `/main-sales`, `/cancellations`, `/vision-packages`, and everything
  under `/admin` (including `/admin/sales-reps` and
  `/admin/sales-reps/<id>/team`). These routes/URLs were **not** changed
  when Overview/Main Sales/Service Cancellations/Vision Packages moved
  out of the main top nav into the Admin Portal's own nav 2026-08-19
  (see "Admin Portal" → "Admin Portal Navigation") — only where they're
  *linked from* changed, not what guards them.
- `_topnav.html` hides its one remaining Admin link for non-admins, and
  `_admin_nav.html` is itself only ever reached by way of an
  `@auth.admin_required` route — **a convenience, not the protection**;
  every route behind either nav has its own `@auth.admin_required`
  regardless, so typing the URL directly (Sales Rep or not, logged in or
  not) gets a real `403` or a redirect to `/login`, never the real page.
- The Needs Attention 15-day rule is enforced exactly the same way —
  see "Needs Attention Ownership & the 15-Day Rule" above.

### Development / Testing

First test suite in this repo (`tests/`, `pytest`) — the permission rule
and Excel-validation logic are pure functions with no database/Flask
dependency, so they run instantly:

```bash
docker compose exec dashboard pytest tests/ -v
# or, from inside the container:
docker compose exec dashboard sh -c "pytest tests/ -v"
```

`tests/test_permissions.py` covers every rule in
`permissions.can_work_account()` directly — Day 0/14/15 boundaries for a
Sales Rep working another rep's account, Customer-Success-owned
accounts, Admin bypass, the Other role's conservative default, and
disabled/logged-out users. `tests/test_import_validation.py` covers
`import_service.py`'s validation — a fully valid file, a missing
required column, an invalid Group value, a blank/malformed email, a
duplicate email within the file, an unmatched Sales Rep name (flagged
`Needs Review`, not `Cannot Import`), an empty workbook, an unsupported
file type, and a partial import where some rows succeed while others
fail with independent reasons.

**Testing the Admin login locally:**
1. `docker compose up -d --build`, then hit any page once (or
   `docker compose logs -f dashboard`) to trigger the first data load —
   this is what runs the initial-Admin bootstrap.
2. Find the setup link in the logs: `docker compose logs dashboard | grep setup`.
3. Open it, set a password, you're logged in as Admin.

**Testing the Excel import:** build a `.xlsx` with columns `Rep Name`,
`Email`, `Group` — include at least one row matching a real
`sales_reps` name (any rep currently on the Individual Rep Leaderboard),
one with an invalid Group value, one with a blank email, and one with a
Rep Name that doesn't match anyone, to see all four `Proposed Action`
outcomes in the preview before committing.

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
| **Channel & Time-of-Day Performance** | | One row, one card, a segmented-control toggle across three tabs: **Sales Volume**, **Channel Performance**, **Time of Day**. **Sales Volume is the default-shown tab** (added 2026-08-18, by request, replacing Time of Day as the default). Sales Volume plots cumulative OUTBOUND sales per rep for the current calendar month to date (`OutboundSalesVolumeChart`, a Chart.js multi-line chart) plus a **Team Average** line (thicker, dashed, drawn on top) so leadership can see who's pacing above/below the team — scoped by a **Team View** selector (Junior / NJ - Sales Reps / NY - Sales Reps / VA - Sales Reps, a premium segmented pill control reusing `.td-chart-toggle`) placed **below the chart and legend**, per spec (`_sales_volume_panel.html`, reworked 2026-08-19). This selector filters by each rep's own assigned **team** (`sales_reps.team`, see "Sales Rep Roles / Teams" under "Database Models") — a *different* grouping from the Sales Calendar & Monthly Sales Trend section's own Team/NJ/NY/VA toggle above, which groups by the sale's `state` column; the two don't share data or code. Two filters stack in `app.py` before `calculate_sales_volume_trend()` runs, both exceptions to "read straight from source data" (see "User Records vs Sales Rep Records"): (1) only reps mapped to a user with the Sales Rep role (`user_store.list_sales_rep_role_names()`), then (2) only reps assigned to the currently-viewed team (`user_store.list_sales_reps_by_team()`) — a rep with no team assigned appears in **no** team view rather than being guessed into one. Switching teams is instant, client-side only (`wireChartToggle()`, no page reload) and the selection survives switching to Channel Performance/Time of Day and back, since the inner toggle keeps its own state independently of the outer one. An empty team shows one of two distinct messages — "No reps currently assigned to `<team>`." when the team has zero reps, or "No outbound sales recorded for `<team>` this month." when it has reps but no outbound sales yet — never a broken chart. Sales Volume is **always the current month, independent of the page's period filter** — same reasoning as Records/Calendar (`calculate_sales_volume_trend()` in `sales_metrics.py`, one call per team, each a single groupby, no per-rep queries). Channel Performance (`SalesByChannelChart`) and Time of Day (`HourlySalesChart`) are unchanged from before except their button labels (`Sales by Channel`/`Sales by Hour` → `Channel Performance`/`Time of Day`) and are still period-filtered, reusing `calculate_channel_breakdown()`/`calculate_hourly_breakdown()` unchanged. Channel bars are clickable, opening a channel-scoped team-wide Bulk Account View; Time of Day has no drill-down. Every chart is created lazily the first time its tab is opened (`wireChartToggle()` in `static/js/charts.js`); Sales Volume's own rep legend is a custom scrollable HTML legend (not Chart.js's canvas legend) so a team with many reps never grows the card unbounded — click a rep to toggle their line, same interaction Chart.js's built-in legend would offer. **Hovering a rep's legend name** (desktop, added 2026-08-19) fades every other rep's line to low opacity while keeping that rep's line **and** Team Average at full opacity, with a smooth Chart.js-animated color tween — purely visual (`setSalesVolumeFocus()` in `charts.js`), no data/scale/tooltip change, restored on mouse-out — see "Chart Card" below for the mechanics. **Team-only** — the Individual Sales Profile's own copy of this section (see below) does not get a Sales Volume tab, since a single rep has no team to pace against | `<section class="td-section">` containing `<h2>Channel &amp; Time-of-Day Performance</h2>` + `{% include "_channel_hourly_toggle.html" %}` (which itself conditionally includes `_sales_volume_panel.html`) |
| **Planet Networks Records** | "Records Section" | Historical all-time individual sales achievements: Best Days Ever, Best Weeks Ever, Best Months Ever. **Collapsed by default** (added 2026-08-16, by request) — a native `<details>`/`<summary>` disclosure, no JS involved; click the heading to expand/collapse | `<section class="td-section">` containing `<details class="td-collapsible"><summary class="td-section-heading td-collapsible-summary"><h2>Planet Networks Records</h2>` |
| **Current Sales Leaders** | | Current-period individual sales leaderboards — who's leading right now: Weekly Sales, Monthly Sales, Yearly Sales (each card is its own live top-3, independent of the page's period filter). Daily Sales moved to Live Sales Activity 2026-08-16 (by request, for an even 3+3 card split with that section) | `<section class="td-section">` containing `<h2>Current Sales Leaders</h2>` |
| **Live Sales Activity** | | Today's leaderboard plus recent sales and account activity: Daily Sales (moved here from Current Sales Leaders 2026-08-16 — same `records.daily_leaders` data, unchanged), Latest Sales (the reps behind the most recent sales), and Latest Accounts Sold (the accounts behind those same sales, renamed from "Last 3 Sales"/"Last 3 Accounts Sold" 2026-08-16 since the displayed count is a display detail, not part of the card's identity), which also carries the "View All Sales" button in its top-right corner. Now a 3-column grid (`.td-activity-grid`, widened 2026-08-16 to fit the third card) | `<section class="td-section">` containing `<h2>Live Sales Activity</h2>` |
| **Individual Rep Leaderboard** | "Rep Leaderboard", "Leaderboard Table", "Rep Performance Section" | The rep search field plus the ranked table (Rank, Sales Rep, Sales, Outbound, **Outbound Installs** (added 2026-08-18, by request — Outbound-channel sales that also installed, i.e. outbound ∩ installed), Inbound, Installs, Pending, **Needs Attention** (replaced the Cancels count column 2026-08-17, by request), Install Rate) — every column header is clickable to sort the table by that column. **Cancel Rate column removed 2026-08-17, by request** | `<h2>Individual Rep Leaderboard</h2>` + `<form class="td-search-form">` + `<section class="td-table-card">` |

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

The Individual Rep Leaderboard and Channel & Time-of-Day Performance's
Channel Performance/Time of Day tabs all read from the period-filtered
dataset (`period_df` in `dashboard_page()`, `app.py`), so a date-range
change affects them. Team Overview, Planet Networks Records, Current Sales
Leaders, Live Sales Activity, Monthly Sales Trend, the Sales Calendar, and
Channel & Time-of-Day Performance's **Sales Volume** tab are all
independent of the selected date range — they always use the all-time
dataset / today's real date (see `calculate_total_daily_sales()`,
`calculate_daily_averages()`, `calculate_monthly_forecast()`,
`calculate_records()`, `calculate_monthly_sales_trend()`,
`calculate_sales_volume_trend()`, and `calculate_calendar_sales()` in
`sales_metrics.py`). Planet Networks
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
| **Overview** | Metric cards for Total Sales, Installed, Install Rate (primary) and Inbound, Outbound, Pending (secondary, now 3 cards — Cancelled and Cancel Rate removed 2026-08-17, by request) — all period-filtered — plus two chart sections (added 2026-08-16): **Sales Activity** (a rep-scoped Sales Calendar beside a `MonthlySalesChart` bar chart, day-of-month with a cumulative trend line and per-day channel breakdown in the tooltip; always all-time with its own `cal_year`/`cal_month` nav, independent of the period filter, same convention as the Team Leaderboard's Sales Calendar); **Channel & Time-of-Day Performance** (one row, one card, a segmented-control toggle between a `SalesByChannelChart` horizontal bar chart — replacing the former plain-text Inbound/Outbound columns, reuses `calculate_channel_breakdown()` unchanged, bars clickable into a channel-scoped Bulk Account View — and an `HourlySalesChart` across all 24 real hours built from `sale_datetime` via `calculate_hourly_breakdown()`, merged from two separate sections into one toggle 2026-08-16 by request; **Time of Day** is the default-shown tab here — this page does not get the Team Leaderboard's Sales Volume tab, since a single rep has no team to pace against; button labels are shared with the Team Leaderboard's copy of this section and read **Channel Performance**/**Time of Day** as of 2026-08-18). Renders via Chart.js v4.4.4 (loaded in `rep_profile.html`'s `<head>`, same version already used on the `/overview` page) and the shared `static/js/charts.js` helper module — see "Chart Card" below. |
| **Needs Attention** | Accounts for this rep that are not Installed/Cancelled and have an install date strictly before today — i.e. no install date at all, explicitly "Not Scheduled", or a past install date (redefined 2026-08-17, twice the same day — today itself moved from Needs Attention to Pending in the second pass, see "Pending vs Needs Attention" below). Always uses the full all-time dataset regardless of the page's period filter — same reasoning as Planet Networks Records / Sales Calendar / Team Overview on the Team Leaderboard. Rendered via the shared Bulk Account View component (see below), extended with the **Needs Attention Workflow** (Attention Status + notes — see its own section further down). |

The Overview tab's metric cards reuse `calculate_rep_metrics()` — the
exact same function/output that produces the Individual Rep Leaderboard table rows
— so the two pages can never disagree for the same period. The Needs
Attention tab's count is shown directly in the tab label
(`Needs Attention (N)`), with a subtle warning treatment when `N > 0`.

## Global Search

> A search box in the top nav (added 2026-08-17) that looks up reps and
> customer accounts at once, as you type.

Present on every `team_dashboard.css` page (Team Leaderboard, Rep
Profile, every Bulk Account View) via a new shared partial,
`templates/_topnav.html` — factored out of the 3 templates that used to
each hand-copy the same top nav markup, specifically so the search box
couldn't end up on some pages and not others by accident of a missed
copy-paste.

**How it searches:** `GET /search?q=<query>` (`search()` in `app.py` →
`search_dataset()` in `sales_metrics.py`) does a case-insensitive
substring match against rep names *and* customer accounts (first name +
last name + address) in the same request, returning both lists
separately as JSON. A name that starts with the query ranks above one
that merely contains it. Each list is capped at `SEARCH_RESULT_LIMIT`
(8) so a broad single-letter query doesn't dump the whole dataset into a
dropdown.

**Staying fast:** every other route now reloads fresh from both source
databases on every request (see "Data loading" above) — `/search`
deliberately does **not**. It fires on every keystroke (debounced
~150ms client-side, `static/js/search.js`, with in-flight requests
aborted via `AbortController` if a newer keystroke arrives first so a
slow, stale response can never overwrite a newer one), and reloading
live from PlanetWeb/KPI on every character typed would make the box feel
sluggish instead of fast. It reads whatever is already sitting in
`data_store` from the last real page load — never more than one
navigation stale, since every page load now refreshes it.

**Telling a rep result from an account result:** each row in the results
dropdown carries a small colored pill — cyan **"Rep"** or purple
**"Account"** (`.td-global-search-type-rep`/`-account` in
`team_dashboard.css`) — plus the results are grouped under separate
"Reps"/"Accounts" headers, so it's unambiguous even scanning quickly.
- A **rep** result links straight to that rep's Individual Sales Profile.
- An **account** result links to its Vision page (external, new tab —
  this app has no per-account detail page of its own, so Vision is the
  closest thing to one, same reasoning as the "Vision ↗" link on every
  Bulk Account View card) when a `subscriber_uuid` exists, or falls back
  to that account's rep's Individual Sales Profile when it doesn't (so a
  result is never a dead end) — its subtitle shows the address and
  `Sold by <rep>` either way.

`build_bulk_account_rows()` gained a `sales_rep` field for this feature
(previously the shared Bulk Account View row shape didn't carry who sold
an account) — a plain passthrough, read by no calculation, so every
existing Bulk Account View caller is unaffected by its presence.

## Individual Sales Profile — Achievements

> Small cosmetic badges (icon + hover tooltip) shown next to a rep's name
> at the top of their Individual Sales Profile — added 2026-08-17, when
> the first one (the outbound badge) started as a one-off request for a
> specific rep and was generalized into a real, criteria-based badge
> system the same day.

**Purely cosmetic.** A badge is earned by meeting a threshold computed
directly from the normalized sales dataset — never the other way around.
No badge is read by, or can influence, any metric calculation
(Sales/Outbound/Installs/Pending/Needs Attention/Install Rate/etc.) —
earning or losing one never changes a number anywhere else on the
dashboard. Always **all-time**, independent of the page's period filter,
same convention as Planet Networks Records / Sales Calendar / Team
Overview.

Currently defined, in `ACHIEVEMENTS` (`sales_metrics.py`):

| Badge | Icon | Criteria | Check function |
|-------|------|----------|-----------------|
| **10+ Outbound Sales in a Day** | `static/images/samurai.gif` | Rep has recorded 10 or more outbound-channel sales (`OUTBOUND_CHANNELS`) on at least one single calendar day, ever. | `_has_daily_outbound_badge()` |
| **More Than 60 Sales in a Month** | `static/images/squirtle_cool.gif` | Rep has recorded more than 60 total sales (any channel/status) in at least one single calendar month, ever. | `_has_monthly_sales_badge()` |
| **10+ Door to Door Sales** | `static/images/door.gif` | Rep has recorded 10 or more Door to Door-channel sales, cumulative all-time — **not** a per-day/per-month threshold like the other two (its wording had no time-window qualifier, and checked against live data, no rep has ever hit more than 5 Door to Door sales in a single day, so a per-day reading would be unearnable). | `_has_door_to_door_badge()` |

**Architecture:**
- `ACHIEVEMENTS` (`sales_metrics.py`) is the registry — a list of
  `{key, icon, label, check}` dicts, one entry per badge. `check` is a
  `(df, rep) -> bool` function. **Adding a new badge means writing one
  `_has_*_badge()` function and adding one entry here** — not touching
  `app.py` or `rep_profile.html`. Keep this table in sync with that list
  whenever a badge is added, changed, or removed.
- `get_rep_achievements(df, rep)` — the one function `app.py` calls
  (from `rep_profile()`). Returns the subset of `ACHIEVEMENTS` a rep has
  earned, each dict unchanged (`key`/`icon`/`label`) so the template can
  render straight from it. Empty list, never raises, for a missing
  dataset or a rep with no rows.
- `rep_profile.html` loops over `achievements` next to `{{ rep_name }}`
  in the page `<h1>`, rendering one `<img class="td-profile-name-icon">`
  per earned badge (`alt`/`title` both set to the badge's `label`, so
  hovering an icon explains what it means). A rep can display any number
  of badges at once, including zero.
- Icon files live in `static/images/` (new directory, added alongside
  the first badge — nothing else in this app served static images
  before). Keep new badge icons small (the two above are 4–130 KB) since
  they're inlined into every Rep Profile page load.

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

One of exactly eight values — **Duplicate, Cancellation, Engineering
Issues, Underground Issues, Existing Customer, Other, Called, Re-Sold**
(`attention_store.ATTENTION_STATUSES`; "Called"/"Re-Sold" added
2026-08-17, by request) — or unset ("Unclassified" in the UI, never a
stored value; there is deliberately no "Unreviewed"/"In
Progress"/"Resolved" status, and — despite briefly having been assumed
to be one during that same request — no stored "Addressed" status
either; see below). Validated against this fixed list server-side on
every write (`attention_store.ATTENTION_STATUS_SET`), regardless of what
the `<select>` in the browser already restricted it to.

**A status can never exist without a note.** The only function that ever
writes `attention_status` is `attention_store.set_attention_status(sale_id,
status, note, user, owner_rep)`, and it rejects a blank/whitespace-only
note before writing anything — so "a status requires a note" is enforced
by there being exactly one write path, not a rule duplicated across two
code paths that could drift apart. Changing an already-set status back
to a *different* value also requires a new note (same function, same
validation) — the previous status/notes are never deleted, only added
to. `user` (added 2026-08-18) is the authenticated acting user — see
"Authentication, Roles & Admin Portal" → Needs Attention Ownership; this
function no longer takes a free-text "author" string.

**Addressed** = has a non-null `attention_status` (which, by the rule
above, always means at least one note exists too — the two conditions
in the task's spec collapse to one check in practice).
**Remaining** = Needs Attention total − Addressed. Both are pure
workflow metrics, shown as a progress bar/stat row at the top of the
Needs Attention Bulk Account View. **"Addressed" is not a filter chip**
(removed 2026-08-17, by request, in the same change that added
"Called"/"Re-Sold" — a same-day mix-up briefly treated "Addressed" as if
it were a real category to swap out, which it was never actually stored
as; it stays a progress stat only). The filter chips that remain are All
/ Unclassified / one per real status — filtering only hides/shows
already-rendered accounts (`static/js/attention.js`), it never
re-queries or changes the underlying list.

**If an Attention Status is ever removed or renamed from
`ATTENTION_STATUSES`** (as opposed to added, like this change), any
account still holding that now-invalid value must be reclassified to
Unclassified rather than left pointing at a value that's no longer
valid. `attention_store._reclassify_removed_statuses()` handles this
automatically — it runs once per process alongside the schema check
(`_ensure_schema()`) and deletes the `account_attention` row for any
`sale_id` whose `attention_status` isn't in the current
`ATTENTION_STATUSES` (deleting that row *is* "reclassify to
Unclassified" — see Persistence below for why Unclassified is the
absence of a row, not a stored value). `account_attention_notes` is
never touched by this, so that account's full note/audit history
survives untouched even after its current classification is cleared —
verified by hand: inserted a row with a made-up invalid status plus a
note, restarted the app, confirmed the `account_attention` row was gone
and the note was still there afterward. This is a no-op in the common
case (every write already validates against the approved list, so
nothing becomes orphaned by normal use) — it only ever does something
the moment `ATTENTION_STATUSES` itself changes out from under
already-existing data, which is exactly what happened here.

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
    sale_id BIGINT NOT NULL,            -- plain column, NOT a foreign key -- see below
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

**`account_attention_notes.sale_id` is deliberately not a foreign key**
into `account_attention(sale_id)`, even though it originally was one when
this feature first shipped. Reclassifying an account to Unclassified
means *deleting* its `account_attention` row (see "Attention Status"
above) — with the original FK in place, that delete would be blocked by
Postgres for any account with even one note, directly breaking "notes
are never deleted." Dropping the constraint is what makes the two rules
("Unclassified = no row" and "notes survive forever") compatible.
`_SCHEMA_SQL` includes `ALTER TABLE account_attention_notes DROP
CONSTRAINT IF EXISTS account_attention_notes_sale_id_fkey` so this
self-heals on any database that still has the original constraint
(local dev, and the production `appdb` from before 2026-08-17) the next
time the app starts — `IF EXISTS` makes it a no-op on a database that
never had it.

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
  `attention_store.set_attention_status()`). JSON body: `{status, note}`.
  Returns the account's full updated note list. Requires login
  (`@auth.login_required`) and the one authoritative permission check
  (`permissions.can_work_account()`, via `_authorize_account_action()`)
  before anything is written — see "Authentication, Roles & Admin
  Portal" → Needs Attention Ownership & the 15-Day Rule.
- `POST /dashboard/attention/<int:sale_id>/notes` — add a note without
  changing status (`attention_add_note()` → `attention_store.add_note()`).
  JSON body: `{note}`. Same permission check as above.
- Both validate `sale_id` against the currently loaded normalized dataset
  (`_lookup_account_owner()`) before touching `attention_store` at all —
  a `sale_id` is a value the browser sends back to us, so it's never
  trusted to correspond to a real account without checking first (404 if
  not). Both validate/trim/length-cap the note and validate the status
  against the approved list **server-side**, independent of whatever the
  browser already checked — the task was explicit not to rely on
  JS-only validation. `author` (a free-text "Your name" field) was
  removed 2026-08-18 once real authentication existed — who acted is now
  the logged-in user, recorded server-side.
- `attach_attention_metadata(accounts)` (`app.py`) — the one function
  that joins Attention Status/notes/the current user's `can_work`
  permission onto an already-built list of `build_bulk_account_rows()`
  dicts, and computes the Addressed/Remaining progress numbers. Called
  from `rep_profile()` (the inline Needs Attention tab),
  `bulk_account_view()`, and `all_sales_view()` (the standalone/team-wide
  Bulk Account View pages), every time **only** for
  `view == "needs_attention"`.

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
- **Pending vs Needs Attention (redefined 2026-08-17, by request; today's
  bucket flipped again the same day, also by request)**: both `pending`
  and `needs_attention` are just `_bulk_status_filter(status)` over the
  `status` column now — the actual classification logic lives in one
  place, `build_sales_dataset()`, not duplicated across two filter
  functions (there used to be a separate `_needs_attention_filter` doing
  its own independent date comparison; it's gone, replaced by a plain
  status-equality filter, same pattern as `pending`). For any account
  that is not Installed and not Cancelled: **Pending** = has a
  `StartDate` (install date) today or in the future; **Needs Attention**
  = everything else — no install date at all, `Scheduled == "Not
  Scheduled"`, or an install date strictly before today. The two are
  mutually exclusive and exhaustive by construction. Date comparison is
  calendar-date-only via `.dt.normalize()` (deliberately not `.dt.date`
  — on some pandas versions `.dt.date` on an all-NaT column silently
  stays `datetime64` dtype instead of converting to `object`, breaking a
  direct comparison against a plain `date`; `normalize()` avoids that by
  staying in `datetime64` throughout and comparing against a
  `pd.Timestamp`), so an install slot later today still counts as
  "today" (Pending), not "past" (Needs Attention). **This flipped from
  the original 2026-08-17 rule**, where today was deliberately put in
  Needs Attention as "the more conservative/actionable default" — a
  same-day follow-up request explicitly moved it to Pending instead
  ("should not be flagged as Needs Attention if the install date is for
  today, only if scheduled before today"). If this is ever revisited
  again, treat it the same way both previous changes were treated: a
  business decision to confirm with the user, not infer.
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
  - **Rep Calendar daily-sales drill-down** (`view=date`, reworked
    2026-08-19): the clickable element on each Sales Activity calendar
    day is now the sales **count** itself (`.td-calendar-count-link`,
    `templates/_rep_sales_calendar.html`) — a subtle accent-colored hover
    state (existing `--td-accent-blue`/`--td-accent-blue-bg` tokens),
    not the whole day cell, which is now a plain non-interactive `<div>`
    (the day number is static text). A day with 0 sales renders a plain
    `—` `<span>`, never a link — clicking it can't happen, so it can
    never open an empty Bulk Account View. URL params are unchanged:
    `/dashboard/reps/<rep_name>/accounts?view=date&year=YYYY&month=M&day=D`
    — `rep_name` in the path (this app's existing, only rep identifier
    everywhere in routing — there is no numeric rep ID surfaced to any
    route, so introducing one here alone would be inconsistent, not more
    "stable") and `year`/`month`/`day` as separate integers (already
    unambiguous — no locale/format risk the way a single delimited date
    string could have), consistent with how the same link has always
    been built, and with the identical drill-down the Sales Activity
    section's `MonthlySalesChart` bar clicks already send to the same
    URL shape (`createMonthlySalesChart()`'s `onClick` in `charts.js`,
    unchanged). **Consistency guarantee:** `sales_metrics.py` now has one
    shared `_rep_sales_on_date(df, rep, target_date)` helper — the single
    definition of "a sale happened on this day for this rep" (same
    `sales_rep` equality check, same `sale_date` column, same "drop rows
    with no `sale_date`" rule) — that `get_calendar_day_account_view()`
    calls directly, and that `calculate_rep_monthly_activity()` (the
    function that produces each day cell's `count`) is documented against
    so the two can never quietly diverge even though the month view stays
    a single vectorized groupby for performance rather than 31 separate
    calls to the same helper. The number on a calendar day and the row
    count behind its drill-down are therefore mathematically guaranteed
    to agree.
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
  `createHourlyChart()`, `createSalesVolumeChart()` (added 2026-08-18, for
  the Team Leaderboard's Sales Volume tab) are the chart factories, each
  reading its data from `data-*` attributes on its own `<canvas>`
  (rendered server-side via Jinja `|tojson` — no separate JSON endpoint).
  `createSalesVolumeChart()` pairs each rep's dataset with a bolder dashed
  Team Average dataset (`order: 0`, drawn on top) and renders its own
  scrollable HTML legend via `renderVolumeLegend()` instead of Chart.js's
  built-in canvas legend, so a team with many reps never grows the chart
  card unbounded — click a legend entry to toggle that
  rep's line, same interaction Chart.js's own legend would give.
  **Legend hover-to-focus** (added 2026-08-19): hovering a rep's name in
  that legend calls `setSalesVolumeFocus(chart, legendItems, index)`,
  which fades every *other* rep's line/legend text down to
  `VOLUME_FADE_ALPHA` (`0.12`) while keeping the hovered rep **and** Team
  Average at full opacity — mousing off calls the same function with
  `index = null` to restore every line. Only `borderColor`/
  `backgroundColor` are touched (via `withAlpha()`, a small hex/rgba→rgba
  helper) — never `data`, scales, or the click-to-toggle-visibility
  `hidden` state, so hovering never changes what the chart shows, only
  how bright each line is; a rep already hidden by a click stays hidden.
  The fade itself tweens smoothly rather than snapping because Chart.js
  v4's built-in `colors` animation collection is explicitly configured
  (`options.animations.colors`, `duration: 400` matching the rest of this
  chart's `baseAnimation()`) to animate `borderColor`/`backgroundColor`
  changes on `chart.update()` — no hand-rolled `requestAnimationFrame`
  loop needed. Desktop-only by design (`mouseenter`/`mouseleave` on each
  legend `<button>`) — touch devices keep only the pre-existing
  tap-to-toggle-visibility behavior; a tap gesture doing double duty for
  both focus *and* hide/show was deliberately avoided as the "complicated
  mobile interaction" the spec asked not to introduce.
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
  shared partial for the specific case of several charts sharing one row
  via a segmented-control toggle — currently the Channel & Time-of-Day
  Performance section on both pages, now a 3-way toggle on the Team
  Leaderboard (Sales Volume / Channel Performance / Time of Day) once
  `has_volume` is true, and unchanged 2-way (Time of Day / Channel
  Performance) on the Rep Profile page. It renders every canvas (or each
  one's own empty-state message) up front; `wireChartToggle()` in
  `charts.js` shows one panel at a time and creates each chart lazily on
  first activation (a canvas created while `display:none` sizes to 0 in
  Chart.js and won't self-correct, so the default-active tab's chart is
  created immediately on page load and the rest are deferred until their
  first click). `wireChartToggle()` is generic — it maps each canvas's
  `data-chart-type` attribute to a factory via `CHART_TOGGLE_FACTORIES`,
  so adding another toggle pane means adding one entry to that map, not a
  new toggle handler. It also takes an optional `scopeSelector` (default
  `.td-chart-card`) so a toggle can be scoped to a nested ancestor instead
  of the outer chart card — used by the Sales Volume tab's own nested
  Team View selector (Junior/NJ - Sales Reps/NY - Sales Reps/VA - Sales
  Reps, `templates/_sales_volume_panel.html`, scoped to
  `.td-volume-group`, wired independently in `charts.js`, positioned
  below the chart+legend per spec), so the two nested toggles'
  `[data-chart-panel]` elements don't collide.
- Currently used by the Rep Profile's Sales Activity and Channel &
  Time-of-Day Performance sections, and the Team Leaderboard's Monthly
  Sales Trend and Channel & Time-of-Day Performance sections (see "Rep
  Profile Page — Section Names" and "Team Leaderboard Page — Section
  Names" above) — the same `_chart_card.html` / `_channel_hourly_toggle.html`
  + `charts.js` pair should be reused for any future chart rather than
  hand-rolling new canvas/tooltip/token-reading code.

## What's intentionally not built yet

Scheduled ETL, email/SMS reports, and CRM integration are all out of
scope for this phase. See code comments in `app.py` and `queries.py` for
notes on the future data model (linking `main_sales`, `vision_packages`,
and `service_cancellations` via subscriber UUID) once that relationship
is confirmed.

**User authentication, roles, and an Admin Portal are no longer out of
scope** — see "Authentication, Roles & Admin Portal" above (added
2026-08-18). Every route except `/health` now requires login;
`/overview`/`/main-sales`/`/cancellations`/`/vision-packages` and
everything under `/admin` require the Admin role specifically.
`account_attention_notes.created_by`/`updated_by` (freeform text,
originally a browser-side "Your name" field before real auth existed)
are kept as a display fallback for pre-auth historical rows, but every
write since 2026-08-18 also records a real `acting_user_id` — see that
section's "Database Models".

**Database writes** are no longer entirely out of scope — the Needs
Attention Workflow (added 2026-08-17) and authentication/user management
(added 2026-08-18) both write to `appdb`, an app-owned Postgres database
created specifically for this app's own data. **PlanetWeb SQL Server and
KPI PostgreSQL remain strictly read-only** — nothing in this codebase
executes anything but `SELECT` against either of those two.

Not built: 2FA, OAuth/SSO, JWT-based auth (session cookies were the
simpler fit given this app's zero-framework-dependency style — see
"Authentication, Roles & Admin Portal"), `.xls` import (`.xlsx` was the
hard requirement), and note-editing (notes are deliberately append-only
— see "Needs Attention Workflow" → Notes).
