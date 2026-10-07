"""Data cleaning + channel attribution pipeline for the Marketing Channel
Report -- rendered TWO ways from the exact same template
(templates/marketing_report_template.html) and the exact same pipeline
below, via render_report_html():

  1. Live, at app.py's /marketing route -- runs the FULL pipeline
     (fetch + validate + classify + guardrails) via run_cleaning_cached(),
     which only actually re-fetches/re-cleans once an hour
     (CACHE_TTL_SECONDS) and serves the cached result the rest of the
     time. See run_cleaning_cached()'s own docstring for the caching
     mechanics.
  2. As a standalone, self-contained HTML file via
     scripts/generate_marketing_report.py -- same output, generated once
     on demand, meant for sharing outside the app (e.g. with an agency
     partner who has no login) rather than being reloaded live.

Validation and channel attribution are NOT implemented here. They come
from planet_cleaning/pipeline.py -- the version 2.1.0 cleaning handoff
(see planet_cleaning/DEVELOPER_HANDOFF.md and README.md), vendored
unmodified so a future version can be dropped in as-is. This module only
fetches the rows, feeds them through that pipeline's normalize_rows() +
classify(), and shapes the result for the report. This is "integration
approach 1" from the handoff: keep the all-row dataset and dedupe per
selected reporting window in the report's own JavaScript.

Key rules that now come from the handoff (replacing this module's
earlier homegrown classifier, 2026-10-07):
  - Every MarketingToken starting MP- is Paid (confirmed business rule).
  - fbclid, utm_source=meta/adwords/bing/..., unbounce/leadpages, or
    tw_source=google ALONE no longer prove Paid -- they need independent
    advertising evidence (paid medium, gclid/msclkid/ndclid/gbraid/
    wbraid/gad_source, recent _gcl_aw cookie).
  - Other MarketingTokens are "Offline-Referral / Unconfirmed token"
    unless listed in planet_cleaning/confirmed_tokens.json -- no more
    guessing rep names/direct mail/events from the token's shape.
  - Rows with an invalid timestamp, AvailabilityID, or email hash are
    rejected (and reported), not just invalid AvailabilityID.
  - State is NJ/NY/PA/VA prefix estimate or Unknown (no DC->VA remap).
  - No hard-coded spend; CPA only for spend entered for the exact range.

IMPORTANT: this module does NOT deduplicate by email_id. Dedup must
happen AFTER date-range filtering, scoped to whatever range is currently
selected -- deduping once globally and then filtering by date drops
anyone whose first-ever entry fell outside the window. Since the report
lets the viewer change the date range client-side, dedup happens
client-side too. This module's output is therefore one row per valid
(classified) form submission, in pipeline order (earliest first), not one
row per unique submitter.
"""

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import db
from marketing_data import BASE_START_DATE, VIEW
from planet_cleaning import pipeline

EASTERN_TZ = ZoneInfo("America/New_York")
# The pipeline's timezone for naive InsertDate values AND the reporting
# calendar (day boundaries). Kept as the string the pipeline expects.
REPORT_TZ = "America/New_York"

TOKEN_MAP_PATH = Path(pipeline.__file__).with_name("confirmed_tokens.json")


def _today():
    return datetime.now(EASTERN_TZ).date()


CHANNEL_GROUPS = pipeline.GROUPS


OUTPUT_COLUMNS = [
    "InsertDate", "local_date", "AvailabilityID", "QuoteID", "Zipcode", "State", "Municipality",
    "email_id", "channel_group", "channel_detail", "MarketingToken", "match_tier",
    "needs_review", "review_flags",
]


def classify_rows(raw_rows, now=None):
    """Runs raw View_FormDataAnalytics rows (dicts keyed by _FETCH_COLUMNS)
    through the handoff pipeline. Returns (output_rows, rejected) --
    output_rows is a list of dicts with exactly OUTPUT_COLUMNS, sorted
    earliest submission first (the pipeline's own (UTC time, input row)
    order, which the report's client-side dedupe relies on as its
    tie-breaker); rejected is the pipeline's list of invalid rows, each
    with a ';'-joined `validation_errors`."""
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    df = pd.DataFrame(raw_rows, columns=_FETCH_COLUMNS, dtype=object)
    df["input_row"] = range(1, len(df) + 1)
    rows, rejected = pipeline.normalize_rows(df, REPORT_TZ, now, {})
    rows = pipeline.classify(rows, pipeline.load_token_map(TOKEN_MAP_PATH))

    output_rows = []
    for r in rows:
        local = r["_utc"].tz_convert(REPORT_TZ)
        output_rows.append({
            "InsertDate": local,
            "local_date": local.date(),
            "AvailabilityID": r["AvailabilityID"],
            "QuoteID": r.get("QuoteID"),
            "Zipcode": r["Zipcode"],
            "State": r["State"],
            "Municipality": r["Municipality"],
            "email_id": r["email_id"],
            "channel_group": r["channel_group"],
            "channel_detail": r["channel_detail"],
            "MarketingToken": r["MarketingToken"],
            "match_tier": r["match_tier"],
            "needs_review": r["needs_review"],
            "review_flags": r["review_flags"],
        })
    return output_rows, rejected


# ============================================================
# Fetch + orchestration
# ============================================================

_FETCH_COLUMNS = [
    "InsertDate", "AvailabilityID", "QuoteID", "Zipcode", "NJPR_municipalityName",
    "MarketingToken", "ReferralData", "email_id", "uniqueURL",
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "utm_id",
    "fbclid", "msclkid", "ndclid", "tw_source", "tw_adid", "tw_campaign", "tw_kwdid",
    "gad_source", "gad_campaignid", "gbraid", "gclid",
]
_SELECT_LIST = ", ".join(f"[{c}]" for c in _FETCH_COLUMNS)

SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__), "marketing_cleaning_snapshot.json")


def fetch_raw_rows():
    """One query, base date window only (marketing_data.BASE_START_DATE
    through today) -- same convention as marketing_data.py/
    marketing_metrics.py. Returns (rows, ok, error); rows is a list of
    dicts, never raises."""
    conn = None
    try:
        conn = db.get_planetweb_connection()
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {_SELECT_LIST} FROM {VIEW} WHERE [InsertDate] >= ? AND [InsertDate] <= GETDATE()",
            [BASE_START_DATE],
        )
        rows = [dict(zip(_FETCH_COLUMNS, r)) for r in cursor.fetchall()]
        return rows, True, None
    except Exception as exc:
        return [], False, db.sanitize_error(exc)
    finally:
        if conn is not None:
            conn.close()


def build_guardrail_report(raw_rows, rejected, output_rows):
    """Flags data-quality concerns -- NEVER silently fixes/drops without
    reporting. `rejected` is classify_rows()'s list of rows the pipeline
    refused (bad timestamp/AvailabilityID/email hash, or a future
    submission). Compares today's per-day counts against the previous
    run's snapshot (SNAPSHOT_PATH); the very first run has nothing to
    compare against and says so explicitly rather than pretending the
    check passed.

    Only `warnings` are shown on the report (the "Data quality flags"
    banner); `info` is diagnostic context for anyone inspecting the
    returned dict. The standalone pipeline BLOCKS its payload on rejected
    rows or completeness findings; a live dashboard can't go blank every
    time one bad row lands, so here they're surfaced as warnings instead
    -- never silently dropped."""
    report = {"warnings": [], "info": []}

    total_raw = len(raw_rows)
    report["info"].append(f"Raw rows fetched: {total_raw:,} (planet_cleaning pipeline v{pipeline.VERSION})")

    if rejected:
        reasons = {}
        for r in rejected:
            for reason in str(r.get("validation_errors", "")).split(";"):
                if reason:
                    reasons[reason] = reasons.get(reason, 0) + 1
        detail = ", ".join(f"{count:,} {reason.replace('_', ' ')}" for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]))
        report["warnings"].append(
            f"{len(rejected):,} row(s) failed validation and are excluded from every count ({detail}). "
            f"Correct them at the source before relying on this report for a readout."
        )

    unknown_state = sum(1 for r in output_rows if r["State"] == "Unknown")
    if unknown_state:
        pct = unknown_state / max(len(output_rows), 1) * 100
        report["info"].append(f"Rows with Unknown state (missing/invalid zip or outside NJ/NY/PA/VA prefixes): {unknown_state:,} ({pct:.1f}%)")

    # Suspiciously round total / truncation smell.
    if total_raw > 0 and total_raw % 1000 == 0:
        report["warnings"].append(
            f"Raw row count ({total_raw:,}) is a suspiciously round number -- "
            f"check whether this pull was truncated at a fixed limit (a past pull "
            f"truncated at exactly 10,000)."
        )
    if raw_rows:
        max_date = max((r["InsertDate"] for r in raw_rows if r.get("InsertDate")), default=None)
        if max_date is not None:
            days_short = (_today() - max_date.date()).days
            if days_short > 2:
                report["warnings"].append(
                    f"Most recent row is {days_short} day(s) before today ({max_date.date()}) -- "
                    f"expected data through roughly today; the pull may have truncated early."
                )

    # Prior-run comparison (daily counts).
    today_by_day = {}
    for r in raw_rows:
        d = r.get("InsertDate")
        if d:
            today_by_day[d.date().isoformat()] = today_by_day.get(d.date().isoformat(), 0) + 1

    prior = None
    if os.path.exists(SNAPSHOT_PATH):
        try:
            with open(SNAPSHOT_PATH) as f:
                prior = json.load(f).get("daily_counts", {})
        except Exception:
            prior = None

    if prior is None:
        report["info"].append("No prior run snapshot found -- day-over-day comparison skipped (this is the first run).")
    else:
        recent_days = sorted(today_by_day.keys())[-14:]
        dropped_days = []
        for day_key in recent_days:
            prior_count = prior.get(day_key)
            today_count = today_by_day.get(day_key, 0)
            if prior_count and prior_count > 0:
                delta_pct = (today_count - prior_count) / prior_count * 100
                if delta_pct <= -20:
                    dropped_days.append((day_key, prior_count, today_count, delta_pct))
        if dropped_days:
            for day_key, prior_count, today_count, delta_pct in dropped_days:
                report["warnings"].append(
                    f"Day {day_key}: {today_count:,} rows vs {prior_count:,} in the previous run "
                    f"({delta_pct:.0f}%) -- possible dropped data, verify before trusting this pull."
                )
        else:
            report["info"].append("Day-over-day counts vs the previous run: no drop >= 20% detected on the last 14 days.")

    try:
        with open(SNAPSHOT_PATH, "w") as f:
            json.dump({"generated_at": datetime.now(EASTERN_TZ).isoformat(), "daily_counts": today_by_day}, f)
    except Exception as exc:
        report["warnings"].append(f"Could not write guardrail snapshot for next run's comparison: {exc}")

    unconfirmed = sum(1 for r in output_rows if r["match_tier"] == "unconfirmed_token")
    if unconfirmed:
        report["info"].append(
            f"{unconfirmed:,} row(s) carry a MarketingToken with no confirmed meaning (Offline-Referral / "
            f"Unconfirmed token) -- add confirmed codes to planet_cleaning/confirmed_tokens.json."
        )
    needs_review = sum(1 for r in output_rows if r["needs_review"])
    report["info"].append(f"Rows with review flags: {needs_review:,} (informational -- not exclusions).")

    if output_rows and output_rows[-1]["local_date"] == _today():
        report["info"].append("Most recent day is today and is partial -- the report's default range ends yesterday.")

    return report


def run_cleaning():
    """Full pipeline entry point. Returns (output_rows, guardrail_report)
    -- output_rows is classify_rows()'s list, one row per valid
    submission, NOT deduplicated (see module docstring). Never raises; a
    fetch failure comes back as an empty output_rows plus a guardrail
    report explaining why."""
    raw_rows, ok, error = fetch_raw_rows()
    if not ok:
        return [], {"warnings": [f"Could not fetch marketing data: {error}"], "info": []}

    output_rows, rejected = classify_rows(raw_rows)
    guardrail_report = build_guardrail_report(raw_rows, rejected, output_rows)
    return output_rows, guardrail_report


# ============================================================
# Hourly cache (added 2026-08-20, replacing "clean on every refresh")
# ============================================================
# run_cleaning() takes ~10s against ~15k PlanetWeb rows; re-running it on
# every single page view (the original, explicitly-requested behavior --
# see this module's own header comment history) made /marketing too slow
# in practice, so this now caches its result for CACHE_TTL_SECONDS and
# only actually re-fetches/re-cleans when that expires. generate_report()
# (the /marketing route) is the only live caller today; run_cleaning_cached()
# is still its own function (not inlined) so a future second consumer of
# this same pipeline output can share the cache instead of re-fetching
# independently, same as the removed Available Now accounts drill-down
# used to (see git history if that feature ever needs to come back).

CACHE_TTL_SECONDS = 3600  # 1 hour, by request

_cache_lock = threading.Lock()
_cache = {"output_rows": None, "guardrail_report": None, "computed_at": None}


def run_cleaning_cached():
    """Same (output_rows, guardrail_report) shape as run_cleaning(), but
    only actually re-runs the full fetch+clean+tag+guardrail pipeline
    once every CACHE_TTL_SECONDS -- every other call within that window
    returns the cached result immediately (no PlanetWeb round trip at
    all). time.monotonic(), not wall-clock time, so this can't be thrown
    off by a system clock adjustment.

    A FAILED refresh (run_cleaning() returning empty output_rows -- see
    its own docstring) is deliberately NOT cached: it doesn't reset
    computed_at, so the very next request retries immediately instead of
    the whole dashboard being stuck showing "Could not fetch marketing
    data" for up to an hour because of one transient PlanetWeb blip. Only
    a genuinely successful pull starts a new hour-long window.

    Guarded by _cache_lock so two requests that both arrive while the
    cache is cold/expired can't each kick off their own redundant ~10s
    pipeline run at the same time (a "stampede") -- the second simply
    waits for the first's result and reuses it."""
    with _cache_lock:
        now = time.monotonic()
        if _cache["computed_at"] is not None and (now - _cache["computed_at"]) < CACHE_TTL_SECONDS:
            return _cache["output_rows"], _cache["guardrail_report"]

        output_rows, guardrail_report = run_cleaning()
        if output_rows:
            _cache["output_rows"] = output_rows
            _cache["guardrail_report"] = guardrail_report
            _cache["computed_at"] = now
        return output_rows, guardrail_report


# ============================================================
# Dashboard payload (compact parallel-array shape)
# ============================================================
# Shapes output_rows (one row per valid, classified, NOT deduplicated
# submission -- see run_cleaning()'s docstring) into the compact `const D`
# object the generated report's client-side JS expects. Deduplication-by-
# submitter is NOT done here -- it happens in the browser, scoped to
# whichever date range the viewer currently has selected (dedup must
# happen AFTER date filtering, never before). This function's only job is
# to make the raw rows small enough to embed inline: repeated strings
# (zip/municipality/state, channel group/detail, and the 64-char email_id
# hash) are each replaced with a small integer index into a lookup table.
#
# Row order is preserved from classify_rows() (earliest submission first),
# so the browser's "first row seen for this submitter within the range"
# is exactly the pipeline's select_period() "earliest submission per email
# hash in the period" -- same tie-break, same counts.

# Fixed section order the generated report always uses -- NOT the order
# distinct (channel_group, channel_detail) pairs happen to appear in the
# data.
CHANNEL_GROUP_ORDER = pipeline.GROUPS


def build_dashboard_payload(output_rows):
    """Returns a JSON-serializable dict matching the generated report's
    `const D` shape: day0/maxday, today_day (day index of today's
    partial day -- the report's default range ends the day before),
    spend_total (always None: there is no supported spend source -- the
    viewer enters actual paid spend for the exact range they selected),
    raw_rows (count, pre-dedup), zips[], chans[], and the parallel
    per-entry arrays d/zi/s/a/c/e/r (r = 1 if the row has review flags)."""
    today = _today()
    if not output_rows:
        return {"day0": today.isoformat(), "maxday": 0, "today_day": 0, "spend_total": None,
                "raw_rows": 0, "pipeline_version": pipeline.VERSION,
                "zips": [], "chans": [], "d": [], "zi": [], "s": [], "a": [], "c": [], "e": [], "r": []}

    day0 = min(r["local_date"] for r in output_rows)

    zip_index = {}
    zips = []
    chan_index = {}
    chans = []
    detail_by_group = {}
    for r in output_rows:
        detail_by_group.setdefault(r["channel_group"], set()).add(r["channel_detail"])
    for group in CHANNEL_GROUP_ORDER:
        for detail in sorted(detail_by_group.get(group, [])):
            chan_index[(group, detail)] = len(chans)
            chans.append({"g": group, "d": detail})

    email_index = {}
    d_arr, zi_arr, s_arr, a_arr, c_arr, e_arr, r_arr = [], [], [], [], [], [], []

    for r in output_rows:
        zip_key = (r["Zipcode"] or "", r["Municipality"] or "", r["State"] or "Unknown")
        if zip_key not in zip_index:
            zip_index[zip_key] = len(zips)
            zips.append({"z": zip_key[0], "m": zip_key[1], "s": zip_key[2]})

        email_id = r["email_id"]
        if email_id not in email_index:
            email_index[email_id] = len(email_index)

        d_arr.append((r["local_date"] - day0).days)
        zi_arr.append(zip_index[zip_key])
        s_arr.append(int(r["AvailabilityID"]))
        a_arr.append(1 if r["channel_group"] == "Paid" else 0)
        c_arr.append(chan_index[(r["channel_group"], r["channel_detail"])])
        e_arr.append(email_index[email_id])
        r_arr.append(1 if r["needs_review"] else 0)

    return {
        "day0": day0.isoformat(),
        "maxday": max(d_arr),
        "today_day": (today - day0).days,
        "spend_total": None,
        "raw_rows": len(output_rows),
        "pipeline_version": pipeline.VERSION,
        "zips": zips,
        "chans": chans,
        "d": d_arr, "zi": zi_arr, "s": s_arr, "a": a_arr, "c": c_arr, "e": e_arr, "r": r_arr,
    }


# ============================================================
# HTML rendering -- shared by the live /marketing route (app.py) and
# scripts/generate_marketing_report.py's standalone file output. ONE
# template, ONE substitution function -- so the live page and the
# shareable file can never silently drift apart.
# ============================================================

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "marketing_report_template.html")
_DATA_PLACEHOLDER = "/*__MARKETING_DATA__*/"
_BACK_LINK_PLACEHOLDER = "<!--__BACK_LINK__-->"


def render_report_html(payload, guardrail_report, back_url=None):
    """Fills templates/marketing_report_template.html's data placeholder
    with this run's payload/guardrails, and its back-link placeholder
    with a small "back to Dashboard" link when `back_url` is given (the
    live Flask route passes one; the standalone generator script does
    not -- a shared file meant for an agency partner with no login has
    nothing to link back to)."""
    with open(TEMPLATE_PATH) as f:
        template = f.read()

    data_js = "var D = " + json.dumps(payload, separators=(",", ":")) + ";\n"
    data_js += "var GUARDRAILS = " + json.dumps({"warnings": guardrail_report.get("warnings", [])}) + ";\n"

    if _DATA_PLACEHOLDER not in template:
        raise RuntimeError(f"Template is missing the {_DATA_PLACEHOLDER} placeholder")
    html = template.replace(_DATA_PLACEHOLDER, data_js)

    back_link_html = ""
    if back_url:
        back_link_html = (
            '<a href="' + back_url + '" style="position:fixed;top:16px;right:16px;z-index:20;'
            "font-family:var(--font-mono);font-size:11px;color:var(--text-muted);text-decoration:none;"
            'border:1px solid var(--border);padding:5px 10px;border-radius:999px;background:var(--surface-card);">'
            "&larr; Dashboard</a>"
        )
    html = html.replace(_BACK_LINK_PLACEHOLDER, back_link_html)

    return html


def generate_report(back_url=None):
    """Runs the pipeline (via run_cleaning_cached() -- see its own
    docstring for the hourly cache) and returns the rendered HTML string
    -- the one call both render entry points (the live route and the
    standalone script) make.

    Safe for scripts/generate_marketing_report.py too, even though that
    script wants a genuinely fresh pull every time it's run: the cache is
    an in-memory, process-local dict, and the script is a separate
    one-shot `python3` process each time -- it always starts with an
    empty/expired cache, so run_cleaning_cached() always does a real
    fetch+clean there. Only the long-running Flask app process (the
    /marketing route) ever actually serves a cached result."""
    output_rows, guardrail_report = run_cleaning_cached()
    payload = build_dashboard_payload(output_rows)
    html = render_report_html(payload, guardrail_report, back_url=back_url)
    return html, guardrail_report
