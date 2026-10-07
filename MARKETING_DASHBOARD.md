# Marketing Dashboard — Data Source & Cleaning

This document explains, in one place, exactly where the Marketing Dashboard's
data comes from and what happens to it before it reaches the page. It's meant
to answer two questions on its own, without needing to read the rest of this
repo's `README.md`:

1. **What SQL are we actually running against?**
2. **How is the data cleaned/tagged before it's shown?**

For where this fits in the broader app (routes, other admin tools, the
standalone report generator), see `README.md`'s own "Marketing Dashboard" and
"Marketing Channel Report" sections. This file is the data-pipeline reference;
that one is the feature/architecture reference.

---

## 1. Where the data comes from

**Server:** `sql1.planetweb.planet.net`
**Database:** `PlanetWeb`
**View:** `dbo.View_FormDataAnalytics`

Every marketing lead in this app — the live `/marketing` dashboard, the
standalone Marketing Channel Report, and the Admin Portal's raw-row browser —
reads from this **same one view**, nothing else. No table is written to, and
nothing here ever runs anything but `SELECT`.

### The SQL

This is the actual query the cleaning pipeline (`marketing_cleaning.py`)
runs. It's built from a fixed, hardcoded column list (`_FETCH_COLUMNS` in
that file) plus a date-range `WHERE` clause — parameterized, never
string-interpolated:

```sql
SELECT
    [InsertDate],
    [AvailabilityID],
    [Zipcode],
    [NJPR_municipalityName],
    [MarketingToken],
    [ReferralData],
    [email_id],
    [uniqueURL],
    [utm_source],
    [utm_medium],
    [utm_campaign],
    [utm_content],
    [utm_term],
    [utm_id],
    [fbclid],
    [msclkid],
    [ndclid],
    [tw_source],
    [tw_adid],
    [tw_campaign],
    [tw_kwdid],
    [gad_source],
    [gad_campaignid],
    [gbraid],
    [gclid]
FROM [PlanetWeb].[dbo].[View_FormDataAnalytics]
WHERE [InsertDate] >= '2026-03-01'
  AND [InsertDate] <= GETDATE()
```

- **`2026-03-01`** is `marketing_data.BASE_START_DATE` — the beginning of the
  Marketing dataset in this app. It's a hardcoded constant, not something the
  dashboard lets you page past; if the real data starts earlier, that
  constant is the one place to change.
- **`GETDATE()`** means "through right now, on the SQL Server itself" — not a
  cached snapshot. The live `/marketing` route re-runs this exact query on
  every page load (see §3 below); nothing about this query is memoized.
- This is a **read-only view query with no joins to sales/installs/revenue**.
  A lead here is not yet connected to whether it became a sale — see
  `README.md`'s "Prepare for Marketing V2" for what that would take.

### Where this SQL lives in code

`marketing_cleaning.py`'s `fetch_raw_rows()` is the one function that ever
issues this query. `marketing_metrics.py` (used only by the Admin Portal's
Attribution Quality panel) and `marketing_data.py` (the Admin Portal's raw-row
browser) each run a narrower variant of the same `SELECT ... FROM
View_FormDataAnalytics WHERE InsertDate BETWEEN ...` shape, over the same
view, with the same base date window — never a different table, never a
different date floor.

---

## 2. Cleaning and attribution — the `planet_cleaning` pipeline

Since 2026-10-07, every rule in this section comes from
**`planet_cleaning/pipeline.py`** — the version 2.1.0 cleaning handoff,
vendored unmodified (with its own `README.md`, `DEVELOPER_HANDOFF.md`,
`expected_counts.json` and `confirmed_tokens.json`). **`planet_cleaning/README.md`
is the authoritative rule reference**; this is a summary. To pick up a new
handoff version, replace the files in `planet_cleaning/` and re-run
`tests/test_planet_cleaning_pipeline.py`.

`marketing_cleaning.classify_rows()` feeds the SQL rows through the
pipeline's `normalize_rows()` + `classify()` — "integration approach 1"
from the handoff: keep every valid submission, and dedupe per selected
range in the report's own JavaScript (see "Deduplication" below).

### Validation — rows that are rejected

A row is rejected (excluded from every count, and reported in the
"Data quality flags" banner with a per-reason breakdown) if it has:

- a missing/invalid `InsertDate`, or an ambiguous/nonexistent DST wall time
- an `InsertDate` in the future
- an `AvailabilityID` that isn't an integer `0`–`6`
- an `email_id` that isn't a 64-character hex hash

The standalone pipeline refuses to publish at all when anything is
rejected; the live dashboard can't go blank over one bad row, so it shows
the warning instead.

### Time

Naive `InsertDate` values are treated as `America/New_York`, and day
boundaries are Eastern. **Today is a partial day**: the report's presets
and default view end yesterday; picking today manually is labelled
"includes today (partial day)".

### Zipcode and State

- Accepts 1–5 digits (leading zeros restored), integer-style `.0`
  suffixes, ZIP+4, or 9 digits. Anything else is blank and flagged — digits
  are never pulled out of arbitrary text.
- State is a **NJ/NY/PA/VA zip3-prefix estimate** (070–089, 100–149,
  150–196, 220–246) or `Unknown`. The report shows everything else as
  "Outside footprint". The old `200`–`205` → VA remap is gone (the handoff
  deliberately has no DC→VA mapping).
- Municipality is kept per submission, never replaced by a ZIP-wide label.

### Channel assignment — one lead, one channel

| Priority | Evidence | Channel |
| --- | --- | --- |
| 0 | `MarketingToken` starts with `MP-` (trimmed, any case) | **Paid** — confirmed business rule; covers MP-AUG-26, MP-SEP-26 and every future MP- code. Platform comes from independent evidence (e.g. a `gclid` → Google), else "Paid (other)". |
| 1 | `utm_medium=email`, email source (`sfmc`, `hs_email`, …), or `EM` + digit token | Email |
| 2 | ChatGPT / Copilot / Perplexity source | AI-Referral |
| 3 | `utm_medium` of `social`, `organic`, `organic-social`, `referral` | Organic-Direct ("Organic social" / "Organic / referral") |
| 4 | Paid medium (`cpc`, `ppc`, `paid`, `paid-social`, `display`, …), `gclid`/`msclkid`/`ndclid`/`gbraid`/`wbraid`/`gad_source`, or a `_gcl_aw` cookie under an hour old with no other tracking | Paid, with a platform when the evidence supports one |
| 5 | Exact token listed in `planet_cleaning/confirmed_tokens.json` | Its confirmed channel |
| 6 | Any other `MarketingToken` | Offline-Referral / "Unconfirmed token" — a provisional bucket, not proof the lead was offline |
| 7 | Nothing usable | Organic-Direct / "Unattributed / review" or "Direct / unknown" — not a claim of organic acquisition |

What changed from the old homegrown classifier:

- **`MP-` tokens are Paid** (they used to be Offline-Referral "Agency campaign").
- **These no longer prove Paid on their own:** `fbclid`, `utm_source`
  `meta_*`/`adwords`/`bing`/`reddit`/`next-door`, `unbounce`/`leadpages`, or
  `tw_source=google`. Each needs independent ad evidence.
- **Tokens aren't guessed from their shape.** "Direct mail — confirm",
  "Sales rep / referral — confirm", "Event / one-off" and the rest are gone.
  When the team confirms a code, add it to `planet_cleaning/confirmed_tokens.json`.
- **Click-ID reuse** (`gclid`, `fbclid`, `msclkid`, `ndclid`): the first
  email hash owns the ID; another hash within 60 minutes keeps it with a
  flag, and later reuse loses that ID as evidence. `gbraid`/`wbraid` are
  exempt.

Every row keeps a `match_tier` and `review_flags` (conflicts, missing
`utm_campaign`, unconfirmed token, inferred cookie, …). Flags are
informational, not exclusions. The report's "Available Now (ads)" card
shows how many have flags.

### Deduplication

The pipeline does **not** dedupe — every valid submission is kept, in
earliest-first order. The report's JavaScript dedupes **within whatever
range is selected**: it filters dates first, keeps the earliest submission
per `email_id`, *then* counts channel and availability. A person whose
first in-range submission is Unavailable/organic stays that way, even if a
later one is Available Now/paid. Weekly or monthly unique counts are not
additive. `tests/test_marketing_cleaning.py` checks that this matches the
pipeline's own `select_period()`.

**Verifying against the handoff:** `planet_cleaning/expected_counts.json`
lists the counts from the 2026-09-11 export (e.g. Paid Available Now:
Aug 1–31 = 201, Sep 1–10 = 65, Sep 4–10 = 46). The live dashboard reads
today's view, which has more and newer rows, so its numbers for those
dates will differ somewhat. To reconcile exactly, run
`python planet_cleaning/pipeline.py "Results 9-11-26.csv" out.json --start … --end …`
on that original export.

---

## 3. Where this runs, and when

- **Live, cached for one hour** — `app.py`'s `/marketing` route calls
  `marketing_cleaning.generate_report()`, which runs the entire pipeline
  above (SQL fetch → clean → tag → guardrails) through
  `run_cleaning_cached()`. That function only actually re-runs the pipeline
  once every `CACHE_TTL_SECONDS` (1 hour); every other page load in between
  is served from an in-memory cache instead, with no PlanetWeb round trip.
  Originally re-ran the full pipeline on every single request ("clean on
  every refresh," ~10 seconds against ~15,000 rows) — switched to hourly
  caching 2026-08-20 once that turned out to be too slow for real usage. A
  failed pull is never cached, so a transient PlanetWeb outage doesn't lock
  the page into an error state for the rest of the hour — the next request
  just retries.
- **On demand, as a standalone file** —
  `python3 scripts/generate_marketing_report.py` runs the exact same
  pipeline once and writes a single self-contained HTML file (no server
  needed to open it), for sharing outside this app — with an agency partner
  who has no login, for instance.

Both paths call the same `marketing_cleaning.py` functions and render the
same `templates/marketing_report_template.html` — there is exactly one
cleaning pipeline and one template, not two copies that can drift apart.

---

## 4. Guardrails — flagged, never hidden

Every report run shows a **"Data quality flags"** banner (or nothing, if
there's nothing to flag) built from `marketing_cleaning.build_guardrail_report()`:

- Rows rejected by the pipeline, broken down by reason (see §2)
- Day-over-day submission counts vs. the previous run — flags a drop of 20%+
  on any of the last 14 days (a past pull once silently dropped ~30% of
  recent rows; this is meant to catch a repeat of that)
- A suspiciously round total row count, or a most-recent date well short of
  "today" (a past pull once silently truncated at exactly 10,000 rows)

The returned report's `info` list also records Unknown-state rows,
unconfirmed-token rows, review-flag counts and the partial-day note.

Day-over-day comparison needs a previous run to compare against
(`marketing_cleaning_snapshot.json`, written after every run, gitignored —
it's runtime state, not source). The very first run says so explicitly
rather than silently skipping the check.

---

## 5. Known limitations, in one place

- **No spend is stored or assumed.** The old hard-coded $195,675 total and
  its day-count proration are gone. Enter actual Paid spend for exactly
  the selected dates; it clears whenever the range changes. CPA = spend ÷
  paid Available Now, for the blended figure and the Paid group row only.
  Platform, state and zip CPA show "n/a": a period-wide spend figure
  can't be split by platform, geography or day. The app can't verify a
  spend figure someone types in.
- **Attribution is recorded form evidence**, not causal lift, view-through
  CTV/radio impact, or cross-device attribution.
- **Unconfirmed tokens** sit in Offline-Referral / "Unconfirmed token" until
  someone adds them to `planet_cleaning/confirmed_tokens.json`.
- **State is a zip3-prefix estimate** for NJ/NY/PA/VA only.
- **Counts are unique email hashes**, not verified people, households,
  sales or installs. Available Now is serviceability, not a qualified lead.
- **Not joined to sales/installs/revenue.** A lead here isn't yet connected
  to whether it converted — see `README.md`'s "Prepare for Marketing V2".
- **The Admin Portal's Attribution Quality panel** (`marketing_attribution.py`)
  still uses the older, separate Paid/Not-Paid model, so its numbers won't
  match this report's.
