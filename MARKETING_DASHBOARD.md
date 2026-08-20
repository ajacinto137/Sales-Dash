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
  every page load (see §4 below); nothing about this query is memoized.
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

## 2. Cleaning — before any lead gets a channel

Every row goes through this before attribution even starts
(`marketing_cleaning.clean_row()`):

### Zipcode

- Whatever comes back (`"07461"`, `"07461-1234"`, or even a number that lost
  its leading zero like `7461`) is normalized to a plain 5-digit string:
  strip anything from a `-` onward, keep only digits, zero-pad to 5.
- A value with no digits at all becomes an empty string, not an error.

### State

- Derived from the zip's **first 3 digits** against the standard USPS
  Sectional Center Facility (SCF) prefix ranges (070–089 → NJ, 100–149 → NY,
  150–196 → PA, 220–246 → VA, and so on for the rest of the country).
- Anything outside a known range — or an empty zip — becomes **`"Unknown"`**,
  never guessed.
- **This is an approximation, not a per-zip lookup.** One deliberate
  deviation from the textbook USPS table: `200`–`205` is nominally "DC",
  but Planet Networks has no DC service footprint at all — every real row
  seen with a zip3 in that range (`200`, `201`) is Northern Virginia,
  processed through the DC postal SCF despite being physically in VA, so
  it's mapped to **VA** instead. Verified correct for every zip3 prefix
  actually seen in this app's real data for the four footprint states
  (NJ/NY/PA/VA) during development; treat state assignment outside that
  footprint as directionally right, not survey-grade.

### AvailabilityID

- Must be an integer `0`–`6`. Anything else (`NULL`, out of range, garbage) —
  **the entire row is dropped**, and the drop count is reported in the
  guardrail banner (see §5). Nothing gets a fabricated status.
- The 7 codes: `0` Unavailable, `1` Available Now, `2` Coming soon (no
  ordering), `3` Coming soon (preorder), `4` Permitting, `5` Strand
  Construction, `6` Fiber Construction.

### What's deliberately **not** done here: deduplication

The pipeline does **not** deduplicate by `email_id` at this stage, and never
will at this stage. Every cleaned, tagged row from a given date-window pull
is kept, one row per raw submission.

Deduplication happens **client-side, in the dashboard's own JavaScript**,
scoped to whatever date range you currently have selected — because
deduplicating once globally and then filtering by date would drop anyone
whose very first submission happened to fall outside that window. Change the
date range, and dedup re-runs from scratch against just that window. This is
why a person active in both May and August shows up once in each month's
numbers and once in the full-period total — that's correct, not a bug (same
semantics as "unique visitors" in any analytics tool).

---

## 3. Assigning a channel — one lead, one channel, first match wins

Every cleaned row gets checked against 5 tiers, **in this order**. The first
one that matches wins; nothing is double-counted.

### Tier 1 — Paid

A row is Paid if **any** of these are true:

| Signal | Platform |
|---|---|
| `utm_source = adwords`, or `gclid`/`gad_source`/`gbraid` populated, or `tw_source = google` | **Google** |
| `utm_source` starts with `meta` (catches `meta_fb`, `meta_ig`, `meta_an`, `meta_th`, and even an unresolved template value like `meta_{{site_source_name}}`), or `fbclid` populated | **Meta** |
| `utm_source = bing`, or `msclkid` populated | **Bing** |
| `utm_source = next-door`, or `ndclid` populated | **Nextdoor** |
| `utm_source = reddit` | **Reddit** |
| `utm_source` in `unbounce`, `leadpages` | **Paid (LP)** — a landing-page host is reliably paid traffic, but doesn't identify a specific ad platform, so it's never guessed as Google or Meta |
| `utm_medium` in `paid-social`, `ppc`, `paid` (source doesn't matter) | **Other Paid** |

**Exclusion:** `utm_medium = social` (exact — *not* `paid-social`) always
wins over everything else, even a populated `fbclid`. This matters in
practice: in this app's real data, every row with `utm_source = ig` pairs
with `utm_medium = social` and often carries a real Facebook click ID — that's
an organic Instagram bio-link click, not a paid ad, and is correctly kept out
of Paid.

**Last-resort fallback — Google's `_gcl_aw` cookie:** only tried if a row has
*no* qualifying UTM signal and *no* click ID at all. The pipeline looks for a
`_gcl_aw` value in the landing URL (`uniqueURL` — never `_gcl_au`, a
different cookie set on nearly every visit that carries no ad signal),
base64-decodes it, and only trusts the recovered click if it happened within
one hour of the submission.

**Real format, confirmed against production data (fixed 2026-08-20):**
Google does not send `_gcl_aw` as its own `_gcl_aw=<value>` query parameter.
It packs it inside a single `_gl=` linker parameter, asterisk-delimited
alongside other sub-values — typically `_gcl_au` right next to it:

```
?_gl=1*<linker-id>*_gcl_aw*<VALUE>*_gcl_au*<other-value>
```

The `<VALUE>` itself is also non-standard: Google terminates it with one or
more literal `.` characters instead of standard base64 `=` padding. An
earlier version of this pipeline assumed the textbook `_gcl_aw=<value>`
shape with `=` padding, which matched **zero** of this app's real rows — the
extraction pattern and the padding step were both silently failing on every
one of the ~1,069 real rows that actually carry a `_gcl_aw` value. Fixed by
matching the real `_gcl_aw*` delimiter and stripping trailing `.` before
padding; verified against a real captured value and covered by a regression
test in both `tests/test_marketing_cleaning.py` and
`tests/test_marketing_attribution.py`. With the fix, Tier 3 recovers
roughly 200+ additional Google-paid leads that were previously falling
through to Organic-Direct.

**Click IDs belong to one person.** If the same `gclid`/`fbclid`/`msclkid`/
`ndclid` value shows up on more than one submission, only the earliest one
(plus anything within 60 minutes of it) is credited as Paid via that click;
later reuses are re-evaluated on whatever other signal they have, with the
reused click id itself ignored. `gbraid` is the one exception — it's shared
by design (Google's privacy-preserving attribution), so it's never subject to
this reuse check.

### Tier 2 — Email

`utm_medium = email`, or `utm_source` in `sfmc`, `hs_email`, `hs_automation`,
`newsletter`, `Community Newsletter`, or a `MarketingToken` matching `^EM\d`
(e.g. `EM1208B`).

### Tier 3 — Offline / Referral

Attributed via **`MarketingToken`** only (not the URL's `token=` parameter —
some tokens are stored server-side only, and `MarketingToken` is the
authoritative field). The token gets sub-classified by pattern:

| Pattern | Example | Label |
|---|---|---|
| `^PC\d` | `PC1`, `PC2` | Direct mail — confirm |
| `^MP-` | `MP-AUG-26` | Agency campaign |
| `^SFED` or a short mostly-uppercase code | `SFEDa`, `PVA1`, `LOB7`, `NSW`, `PN25` | Unknown — confirm |
| contains `facebook`, `instagram`, `gaming`, or `strausnews` | — | Organic social / press |
| ends in a 4-digit year (optionally + more digits) | `NJFAIR2025`, `LMM20260803` | Event / one-off |
| looks like an initial + surname | `MRitchie`, `jmortensen` | Sales rep / referral — confirm |
| anything else | — | Unknown — confirm |

**This is pattern-based against real values, not a maintained lookup table.**
It was calibrated against the ~90 distinct `MarketingToken` values actually
seen in production during development — new codes will still resolve through
these same patterns (or land safely in "Unknown — confirm"), but a handful of
short, ambiguous tokens can land in the wrong "— confirm" bucket. Every label
with "— confirm" in it is explicitly flagged as needing a human to verify —
that's by design, not an oversight.

### Tier 4 — AI Referral

`utm_source` in `chatgpt.com`, `chatgpt`, `copilot.com`, `perplexity`.

### Tier 5 — Organic / Direct

Everything left over. Includes CTV and radio, which can't set a click ID —
don't read a spike in this bucket as purely organic if brand media is
running.

---

## 4. Where this runs, and when

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

## 5. Guardrails — flagged, never hidden

Every report run shows a **"Data quality flags"** banner (or nothing, if
there's nothing to flag) built from `marketing_cleaning.build_guardrail_report()`:

- Rows dropped for an invalid/missing `AvailabilityID`
- Rows with an unresolved State (zip3 outside any known range)
- Day-over-day submission counts vs. the previous run — flags a drop of 20%+
  on any of the last 14 days (a past pull once silently dropped ~30% of
  recent rows; this is meant to catch a repeat of that)
- A suspiciously round total row count, or a most-recent date well short of
  "today" (a past pull once silently truncated at exactly 10,000 rows)
- MarketingToken "— confirm" bucket sizes, so the volume of
  pattern-classified-but-unverified Offline/Referral leads is always visible
- A note that the most recent day is very likely a partial day — exclude or
  label it in any per-day average

Day-over-day comparison needs a previous run to compare against
(`marketing_cleaning_snapshot.json`, written after every run, gitignored —
it's runtime state, not source). The very first run says so explicitly
rather than silently skipping the check.

---

## 6. Known limitations, in one place

- **CPA is directional except the blended figure.** Blended CPA (spend ÷
  Available Now ad entries) needs no assumption. Every other CPA — by
  channel, state, or zip — apportions spend by that segment's share of ad
  entries, because ad platforms don't report spend by geography. The
  dashboard labels this explicitly wherever it appears.
- **`spend_total` is a manually-maintained figure**
  (`marketing_cleaning.TOTAL_AD_SPEND_USD`), confirmed real by the
  marketing team 2026-08-20 — not a live ad-spend data source. It does not
  auto-update as new days of data arrive, so CPA accuracy will drift the
  longer this constant goes un-refreshed; update it whenever a current
  total is available.
- **MarketingToken sub-classification is a calibrated heuristic**, not a
  maintained lookup — see Tier 3 above.
- **State is a zip3-prefix approximation**, not a per-zip lookup — see §2
  above.
- **Not joined to sales/installs/revenue.** A lead here isn't yet connected
  to whether it converted — see `README.md`'s "Prepare for Marketing V2".
