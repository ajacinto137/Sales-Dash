"""Data cleaning + channel attribution pipeline for the Marketing Channel
Report -- rendered TWO ways from the exact same template
(templates/marketing_report_template.html) and the exact same pipeline
below, via render_report_html():

  1. Live, at app.py's /marketing route -- runs the FULL pipeline
     (fetch + clean + tag + guardrails) via run_cleaning_cached(), which
     only actually re-fetches/re-cleans once an hour (CACHE_TTL_SECONDS)
     and serves the cached result the rest of the time. Originally ran on
     every single request ("clean on every refresh"), but a ~10s pipeline
     run against ~15k PlanetWeb rows on every page view proved too slow
     in practice -- changed to hourly caching 2026-08-20, by request. See
     run_cleaning_cached()'s own docstring for the caching mechanics.
  2. As a standalone, self-contained HTML file via
     scripts/generate_marketing_report.py -- same output, generated once
     on demand, meant for sharing outside the app (e.g. with an agency
     partner who has no login) rather than being reloaded live.

This is a DIFFERENT, more detailed attribution model than
marketing_attribution.py (which still powers the Admin Portal's
Attribution Quality panel at /admin/marketing-form-data). The two are
intentionally not merged: this module implements a 5-way channel
taxonomy (Paid / Email / Offline-Referral / AI-Referral / Organic-Direct)
with MarketingToken-based offline/referral sub-classification, click-ID
reuse dedup, and stricter paid exclusions that marketing_attribution.py
does not attempt. If the two ever need to converge, that is a deliberate
follow-up, not an accident of this file.

Pipeline stages, run in order by run_cleaning():
  1. fetch_raw_rows()      -- one query against PlanetWeb's
                               View_FormDataAnalytics, base date window
                               only (marketing_data.BASE_START_DATE
                               through today).
  2. clean_row()            -- per-row: zip normalize/zero-pad, state
                               derived from zip3 (SCF ranges), Availabi-
                               lityID shape-validated (0-6, else row
                               dropped).
  3. tag_channels()         -- assigns channel_group/channel_detail/
                               match_tier to every surviving row,
                               including the two-pass click-ID reuse
                               dedup (see _demote_reused_click_ids()).
  4. build_guardrail_report() -- flags (never silently drops/publishes):
                               invalid-AvailabilityID count, unresolved
                               -state count, day-over-day count vs the
                               previous run's snapshot, round-number/
                               truncation smells, MarketingToken
                               "-- confirm" bucket sizes.

IMPORTANT: this module does NOT deduplicate by email_id. The spec is
explicit that dedup must happen AFTER date-range filtering, scoped to
whatever range is currently selected -- "Never dedupe once globally and
then filter by date, that drops anyone whose first-ever entry fell
outside the window." Since the generated dashboard lets the viewer change
the date range client-side, dedup has to happen client-side too, in
static/js embedded in the report (see generate_marketing_report.py). This
module's output is therefore intentionally one row per raw (cleaned,
tagged) form submission, not one row per unique submitter.
"""

import base64
import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

import db
from marketing_data import BASE_START_DATE, VIEW

EASTERN_TZ = ZoneInfo("America/New_York")


def _today():
    return datetime.now(EASTERN_TZ).date()


# ============================================================
# Zipcode / State cleaning
# ============================================================

def clean_zipcode(raw):
    """"07461-1234" -> "07461"; 7461 (int, leading zero lost in a numeric
    column) -> "07461"; unparseable/empty -> "" (never raises)."""
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text or text.lower() == "nan":
        return ""
    text = text.split("-")[0].strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return ""
    if len(digits) > 5:
        digits = digits[:5]
    return digits.zfill(5)


# Standard USPS Sectional Center Facility (SCF) zip3 prefix ranges,
# inclusive. Approximate by design -- spec explicitly allows
# "unresolvable -> Unknown" rather than requiring a full per-zip lookup
# database. Verified against this app's real data for the footprint
# states (NJ/NY/PA/VA) specifically.
#
# 200-205 is the one deliberate deviation from the textbook USPS table:
# that range is nominally "DC", but Planet Networks' real footprint has
# no DC service area at all -- every real row seen with a zip3 in this
# range (200, 201) is Northern Virginia, processed through the DC postal
# SCF despite being physically in VA. Mapped to VA so those rows count
# toward the real VA footprint instead of silently landing in "Outside
# footprint".
_ZIP3_STATE_RANGES = [
    (0, 5, "Unknown"), (6, 9, "PR"),
    (10, 27, "MA"), (28, 29, "RI"), (30, 38, "NH"), (39, 49, "ME"),
    (50, 59, "VT"), (60, 69, "CT"), (70, 89, "NJ"), (90, 99, "Unknown"),
    (100, 149, "NY"), (150, 196, "PA"), (197, 199, "DE"),
    (200, 205, "VA"), (206, 219, "MD"), (220, 246, "VA"), (247, 268, "WV"),
    (270, 289, "NC"), (290, 299, "SC"),
    (300, 319, "GA"), (320, 349, "FL"), (350, 369, "AL"), (370, 385, "TN"),
    (386, 397, "MS"), (398, 399, "GA"),
    (400, 427, "KY"), (430, 459, "OH"), (460, 479, "IN"), (480, 499, "MI"),
    (500, 528, "IA"), (530, 549, "WI"), (550, 567, "MN"), (570, 577, "SD"),
    (580, 588, "ND"), (590, 599, "MT"),
    (600, 629, "IL"), (630, 658, "MO"), (660, 679, "KS"), (680, 693, "NE"),
    (700, 714, "LA"), (716, 729, "AR"), (730, 749, "OK"), (750, 799, "TX"),
    (800, 816, "CO"), (820, 831, "WY"), (832, 838, "ID"), (840, 847, "UT"),
    (850, 865, "AZ"), (870, 884, "NM"), (889, 898, "NV"),
    (900, 961, "CA"), (962, 966, "Unknown"), (967, 968, "HI"), (969, 969, "GU"),
    (970, 979, "OR"), (980, 994, "WA"), (995, 999, "AK"),
]


def derive_state(zip5):
    if not zip5 or len(zip5) < 3 or not zip5[:3].isdigit():
        return "Unknown"
    prefix = int(zip5[:3])
    for lo, hi, state in _ZIP3_STATE_RANGES:
        if lo <= prefix <= hi:
            return state
    return "Unknown"


FOOTPRINT_STATES = ["NJ", "NY", "PA", "VA"]


# ============================================================
# AvailabilityID validation
# ============================================================

VALID_AVAILABILITY_IDS = set(range(7))  # 0..6


def clean_availability_id(raw):
    """Returns an int 0-6, or None if unparseable/out-of-range -- rows
    with None get dropped by tag_channels()/run_cleaning(), counted in
    the guardrail report, never silently kept with a bogus status."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value in VALID_AVAILABILITY_IDS else None


# ============================================================
# Paid platform matching (Tier "PAID")
# ============================================================

def _norm(value):
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    return text.lower()


def _has_value(value):
    return _norm(value) != ""


# utm_source -> platform, for sources that unambiguously identify one.
# "meta_..." is matched by prefix (see _paid_from_utm below), not listed
# here, since Meta sends several source variants including an unresolved
# templating placeholder ("meta_%7B%7Bsite_source_name%7D%7D") seen in
# real data -- prefix match handles all of them without enumeration.
_PAID_SOURCE_PLATFORMS = {
    "adwords": "Google",
    "bing": "Bing",
    "next-door": "Nextdoor",
    "reddit": "Reddit",
}
_PAID_LP_SOURCES = {"unbounce", "leadpages"}
_PAID_MEDIUM_ALONE = {"paid-social", "ppc", "paid"}

# Click-ID fields that name a platform on their own, checked when no
# qualifying utm_source matched. gad_source is a boolean-ish flag ("1"),
# not a per-click identifier, so it's handled separately (still Google,
# but never subject to the reuse-dedup pass below -- see
# _CLICK_ID_REUSE_FIELDS).
_CLICK_ID_PLATFORMS = {
    "gclid": "Google",
    "gbraid": "Google",
    "fbclid": "Meta",
    "msclkid": "Bing",
    "ndclid": "Nextdoor",
}

# Click-ID fields treated as "belongs to exactly one person" for the
# reuse-dedup pass (spec: "If shared across email_ids, credit the first
# plus anything within 60 min; demote later ones lacking independent UTM
# evidence"). gbraid is explicitly excluded (shared by design, privacy-
# preserving attribution) and gad_source is excluded because it is a
# flag field, not a unique per-click identifier -- reusing it across many
# submissions is normal, not a reuse signal.
_CLICK_ID_REUSE_FIELDS = ["gclid", "fbclid", "msclkid", "ndclid"]
_CLICK_ID_REUSE_WINDOW = timedelta(minutes=60)


def _paid_from_utm(source, medium):
    """UTM-only paid check (no click IDs) -- this is what "independent
    UTM evidence" means everywhere else in this module. Returns
    (platform, detail) or (None, None)."""
    if source in _PAID_SOURCE_PLATFORMS:
        return _PAID_SOURCE_PLATFORMS[source], _PAID_SOURCE_PLATFORMS[source]
    if source.startswith("meta"):
        return "Meta", "Meta"
    if source in _PAID_LP_SOURCES:
        return "Paid (LP)", "Paid (LP)"
    if medium in _PAID_MEDIUM_ALONE:
        return "Other Paid", "Other Paid"
    return None, None


def _paid_from_click_ids(row):
    """Checks gclid/gbraid/fbclid/msclkid/ndclid presence, in that order.
    Returns (platform, field) or (None, None). Does NOT apply the reuse-
    dedup window -- that is a separate pass over the whole dataset (see
    _demote_reused_click_ids()) since it needs every row's InsertDate to
    compare, not just this one row."""
    for field, platform in _CLICK_ID_PLATFORMS.items():
        if _has_value(row.get(field)):
            return platform, field
    return None, None


def _decode_gcl_aw(raw_value):
    """Base64-decodes a `_gcl_aw` token, expecting Google's own
    "GCL.<unix_ts>.<gclid>" cookie format. Returns (gclid, click_ts_utc)
    or (None, None) on ANY failure -- must never raise (a garbled/absent
    value can never break the pipeline).

    Two things this needs that a naive URL-safe-base64 decode gets wrong,
    both confirmed against real captured uniqueURL values during
    development (a raw pipeline handoff script surfaced the bug; verified
    directly against this app's own PlanetWeb data before fixing):
      1. Google terminates the value with one or more literal "."
         characters instead of standard "=" padding (e.g. ...QndE..) --
         these must be stripped before re-padding with "=", or every
         real value fails to decode ("Incorrect padding").
      2. "-"/"_" are the URL-safe base64 alphabet substitutions, applied
         same as anywhere else.
    Before this fix, Tier 3 (_gcl_aw recovery) never fired against real
    production data -- not because the fallback itself was unreachable
    (~1,069 real rows carry a _gcl_aw value), but because both the
    extraction pattern below AND this decode step silently failed on
    every one of them."""
    if not raw_value:
        return None, None
    try:
        std = raw_value.rstrip(".").replace("-", "+").replace("_", "/")
        padded = std + "=" * (-len(std) % 4)
        decoded = base64.b64decode(padded).decode("utf-8", errors="strict")
        parts = decoded.split(".")
        if len(parts) < 3 or parts[0].upper() != "GCL":
            return None, None
        ts = int(parts[1])
        gclid = ".".join(parts[2:])
        if not gclid:
            return None, None
        return gclid, datetime.fromtimestamp(ts, tz=ZoneInfo("UTC"))
    except Exception:
        return None, None


# Google's real linker-parameter format packs _gcl_aw INSIDE a single
# _gl= query parameter, asterisk-delimited alongside other sub-values
# (typically _gcl_au right after it): e.g.
#   ?_gl=1*<id>*_gcl_aw*<VALUE>*_gcl_au*<other-value>
# NOT a standalone "_gcl_aw=<value>" parameter -- an earlier version of
# this pattern looked for "=" and never matched a single real row as a
# result. Delimited by the next "*" or "&" (or end of string), never by
# "." (which is part of the value's own trailing-dot padding, see
# _decode_gcl_aw()).
_GCL_AW_PATTERN = re.compile(r"_gcl_aw\*([^*&\s]+)")
# Deliberately does NOT match _gcl_au -- spec: "set on nearly all visits,
# not an ad signal." A pattern like r"_gcl_a[uw]" would silently start
# treating _gcl_au as a paid signal; kept as two separate, unambiguous
# literals so that mistake can't happen by a future regex "simplification".


def _gcl_aw_fallback(unique_url, insert_date):
    """Google cookie recovery -- ONLY meaningful when the row has no UTM
    paid signal and no click ID at all (enforced by the caller, not here).
    Extracts _gcl_aw from `uniqueURL` (spec: "the landing URL" --
    deliberately NOT MarketingToken/ReferralData, which this module uses
    for a different purpose, Offline/Referral sub-classification -- see
    classify_offline_referral()). Trusts the recovered click only if it
    is within one hour of InsertDate.

    InsertDate is Eastern local time; the recovered click timestamp is
    UTC. The spec describes this as a fixed +4h (EDT) / +5h (EST) offset
    added to InsertDate; using zoneinfo's America/New_York conversion
    here is equivalent but avoids a hand-picked-offset bug during the
    one week each spring/fall where the fixed-offset version would be
    wrong (DST transition)."""
    if not unique_url:
        return None
    match = _GCL_AW_PATTERN.search(str(unique_url))
    if not match:
        return None
    gclid, click_ts_utc = _decode_gcl_aw(match.group(1))
    if not gclid or click_ts_utc is None or insert_date is None:
        return None
    try:
        insert_dt = insert_date if insert_date.tzinfo else insert_date.replace(tzinfo=EASTERN_TZ)
        age = abs((insert_dt.astimezone(ZoneInfo("UTC")) - click_ts_utc).total_seconds())
    except Exception:
        return None
    return "Google" if age < 3600 else None


def _assign_paid(row):
    """Returns (platform, detail, method) or (None, None, None). Method
    is one of "utm", "click_id", "gcl_aw" -- used by the reuse-dedup pass,
    not exposed in the final output schema."""
    source = _norm(row.get("utm_source"))
    medium = _norm(row.get("utm_medium"))

    # Exclusion: utm_medium=social (exact -- NOT paid-social) always wins
    # over any click ID, even a populated fbclid -- spec's own example is
    # IG bio-link clicks, which is exactly what real data shows (every
    # utm_source="ig" row in this dataset pairs with utm_medium="social").
    if medium == "social":
        return None, None, None

    platform, detail = _paid_from_utm(source, medium)
    if platform:
        return platform, detail, "utm"

    # tw_source=google is listed alongside Google's other signals in the
    # spec's platform table -- treated as independent UTM-tier evidence
    # (not a click ID), so it is never subject to the reuse-dedup pass.
    if _norm(row.get("tw_source")) == "google":
        return "Google", "tw_source=google", "utm"

    click_platform, click_field = _paid_from_click_ids(row)
    if click_platform:
        return click_platform, click_field, "click_id"

    return None, None, None


# ============================================================
# EMAIL tier
# ============================================================

_EMAIL_SOURCES = {"sfmc", "hs_email", "hs_automation", "newsletter", "community newsletter"}
_EMAIL_TOKEN_PATTERN = re.compile(r"^EM\d")


def _is_email(row):
    medium = _norm(row.get("utm_medium"))
    source = _norm(row.get("utm_source"))
    token = row.get("MarketingToken") or ""
    if medium == "email":
        return True, source or "email"
    if source in _EMAIL_SOURCES:
        return True, source
    if _EMAIL_TOKEN_PATTERN.match(str(token).strip()):
        return True, "MarketingToken"
    return False, None


# ============================================================
# OFFLINE / REFERRAL tier -- MarketingToken sub-classification
# ============================================================
# Precedence here is chosen for CONFIDENCE, not the order the spec's
# table happens to list examples in: the most structurally distinctive
# patterns (PC\d, MP-, dated codes, the 4 named keywords) are checked
# first; the "personal name" shape -- inherently the fuzziest pattern,
# since it is just "a short lowercase-ish word" -- is checked last,
# right before the Unknown catch-all, specifically so it cannot steal
# short campaign/agency codes (LOB7, PVA1, NSW, ...) that don't match
# anything more specific. Real production MarketingToken values were
# reviewed during development (87 distinct values) to calibrate this,
# but the classifier is pattern-based, not a lookup of today's literal
# values -- new rep codes/campaign codes will still resolve via the
# patterns below, or fall safely into "Unknown -- confirm" rather than
# being silently dropped or guessed wrong with false confidence.

_PC_PATTERN = re.compile(r"^PC\d", re.IGNORECASE)
_MP_PATTERN = re.compile(r"^MP-", re.IGNORECASE)
_SFED_PATTERN = re.compile(r"^SFED", re.IGNORECASE)
_DATED_CODE_PATTERN = re.compile(r"20\d{2}(\d{4})?$")  # e.g. ...2025, ...20260803
_ORGANIC_KEYWORDS = ("facebook", "instagram", "gaming", "strausnews")
# Short agency/campaign codes (PVA1, LOB7, PMG1, MG1, NSW, PN25, PNVA):
# the LETTER portion must be entirely uppercase, optionally followed by
# digits -- deliberately does NOT allow a lowercase tail, since that is
# exactly what distinguishes a real code from a Capital+lowercase name
# like "Jberg"/"MRitchie" (both would otherwise match a looser
# "2-5 letters + digits" pattern; "Jberg" alone forced this to be
# tightened during development against real MarketingToken values).
_SHORT_CODE_PATTERN = re.compile(r"^[A-Z]{2,5}\d{0,3}$")
# Personal name shape: a single capitalized OR lowercase initial-plus-
# surname token, letters only (optionally one trailing digit for variants
# like "mastle2"), length capped generously (jmcglothlin is 11 chars) but
# not unbounded (rules out long multi-word slugs like
# "welcometoplanet-em-butler" or "constructionsqsp"). Checked LAST, right
# before the Unknown catch-all -- it is the fuzziest pattern here (just
# "a short word"), so a handful of non-name lowercase tokens that don't
# match anything more specific (seen in real data: "polaroid", "rdcgpt")
# will still land here rather than Unknown. Both labels carry "-- confirm"
# for exactly this reason; treat this bucket as a starting point for
# human review, not a guaranteed-accurate rep-name list.
_NAME_PATTERN = re.compile(r"^[A-Za-z]{3,14}\d?$")


def classify_offline_referral(token):
    """token is MarketingToken (already known non-empty, non-EM\\d, by
    the caller). Returns a channel_detail string, always one of the
    labels in the spec's table -- never None; "Unknown -- confirm" is the
    final catch-all so an offline/referral lead is never silently
    dropped for having an unrecognized code."""
    text = str(token).strip()

    if _PC_PATTERN.match(text):
        return "Direct mail — confirm"
    if _MP_PATTERN.match(text):
        return "Agency campaign"
    if _SFED_PATTERN.match(text):
        return "Unknown — confirm"
    lowered = text.lower()
    if any(kw in lowered for kw in _ORGANIC_KEYWORDS):
        return "Organic social / press"
    if _DATED_CODE_PATTERN.search(text):
        return "Event / one-off"
    if _SHORT_CODE_PATTERN.match(text):
        return "Unknown — confirm"
    if _NAME_PATTERN.match(text):
        return "Sales rep / referral — confirm"
    return "Unknown — confirm"


# ============================================================
# AI REFERRAL tier
# ============================================================

_AI_SOURCES = {"chatgpt.com", "chatgpt", "copilot.com", "perplexity"}


def _is_ai_referral(row):
    source = _norm(row.get("utm_source"))
    return source in _AI_SOURCES, source


# ============================================================
# Per-row cleaning + tagging
# ============================================================

CHANNEL_GROUPS = ["Paid", "Email", "Offline-Referral", "AI-Referral", "Organic-Direct"]


def clean_row(raw):
    """Zip/state/availability cleaning only -- returns a new dict, or
    None if the row must be dropped (invalid AvailabilityID). Channel
    tagging happens separately in tag_channels() since the click-ID
    reuse-dedup pass needs to see every surviving row at once."""
    availability = clean_availability_id(raw.get("AvailabilityID"))
    if availability is None:
        return None

    zip5 = clean_zipcode(raw.get("Zipcode"))
    cleaned = dict(raw)
    cleaned["AvailabilityID"] = availability
    cleaned["Zipcode"] = zip5
    cleaned["State"] = derive_state(zip5)
    return cleaned


def _assign_channel_pre_reuse(row):
    """First pass: assigns a channel WITHOUT applying the click-ID
    reuse-dedup rule yet (that needs a second pass across the whole
    dataset -- see tag_channels()). Returns a dict with channel_group,
    channel_detail, match_tier, and (for Paid-via-click-id rows only)
    the raw click field name so the reuse pass can find them again."""
    platform, detail, method = _assign_paid(row)
    if platform:
        return {"channel_group": "Paid", "channel_detail": platform, "match_tier": 1,
                "_paid_method": method, "_paid_click_field": detail if method == "click_id" else None}

    # Tier 3 (Google _gcl_aw fallback) only applies with NO UTM paid
    # signal and NO click ID at all present -- checked here, after
    # confirming _assign_paid() found nothing.
    has_any_click_id = any(_has_value(row.get(f)) for f in list(_CLICK_ID_PLATFORMS) + ["gad_source"])
    if not has_any_click_id and _norm(row.get("utm_medium")) != "social":
        gcl_platform = _gcl_aw_fallback(row.get("uniqueURL"), row.get("InsertDate"))
        if gcl_platform:
            return {"channel_group": "Paid", "channel_detail": gcl_platform, "match_tier": 1,
                    "_paid_method": "gcl_aw", "_paid_click_field": None}
    # gad_source alone (no gclid/gbraid, no UTM match) -- flag-style
    # field, still a valid Google paid signal per the spec's platform
    # table, just never subject to reuse dedup (see module docstring).
    if _has_value(row.get("gad_source")) and _norm(row.get("utm_medium")) != "social":
        return {"channel_group": "Paid", "channel_detail": "Google", "match_tier": 1,
                "_paid_method": "gad_source", "_paid_click_field": None}

    is_email, email_detail = _is_email(row)
    if is_email:
        return {"channel_group": "Email", "channel_detail": email_detail, "match_tier": 2,
                "_paid_method": None, "_paid_click_field": None}

    token = row.get("MarketingToken")
    if token and str(token).strip():
        detail = classify_offline_referral(token)
        return {"channel_group": "Offline-Referral", "channel_detail": detail, "match_tier": 3,
                "_paid_method": None, "_paid_click_field": None}

    is_ai, ai_source = _is_ai_referral(row)
    if is_ai:
        return {"channel_group": "AI-Referral", "channel_detail": ai_source, "match_tier": 4,
                "_paid_method": None, "_paid_click_field": None}

    return {"channel_group": "Organic-Direct", "channel_detail": "Organic/Direct", "match_tier": 5,
            "_paid_method": None, "_paid_click_field": None}


def _demote_reused_click_ids(rows):
    """Second pass: for each reuse-tracked click-ID field (gclid, fbclid,
    msclkid, ndclid -- see _CLICK_ID_REUSE_FIELDS), groups rows sharing
    the same non-empty value. Within each group, sorted by InsertDate,
    the earliest row plus any row within 60 minutes of it keeps its Paid-
    via-click-id classification; every later row is demoted UNLESS it
    also has independent UTM evidence (in which case it was never
    "Paid via this click id" in the first place -- its channel is left
    untouched). A demoted row is re-run through
    _assign_channel_pre_reuse() with that one click field blanked out, so
    it falls through to whatever the next real signal says (another
    click id, Email, Offline-Referral, AI-Referral, or Organic-Direct) --
    never force-set to Organic-Direct outright."""
    by_click_value = {}
    for i, row in enumerate(rows):
        if row.get("_paid_method") != "click_id":
            continue
        field = row.get("_paid_click_field")
        if field not in _CLICK_ID_REUSE_FIELDS:
            continue
        value = row["_raw"].get(field)
        if not _has_value(value):
            continue
        by_click_value.setdefault((field, value), []).append(i)

    demoted_count = 0
    for (field, _value), indices in by_click_value.items():
        if len(indices) < 2:
            continue
        indices.sort(key=lambda i: rows[i]["_raw"].get("InsertDate") or datetime.min)
        first_dt = rows[indices[0]]["_raw"].get("InsertDate")
        for i in indices[1:]:
            dt = rows[i]["_raw"].get("InsertDate")
            if first_dt and dt and (dt - first_dt) <= _CLICK_ID_REUSE_WINDOW:
                continue  # within the credit window -- still counts
            raw_without_field = dict(rows[i]["_raw"])
            raw_without_field[field] = None
            replacement = _assign_channel_pre_reuse(raw_without_field)
            rows[i].update(replacement)
            demoted_count += 1
    return demoted_count


OUTPUT_COLUMNS = [
    "InsertDate", "AvailabilityID", "QuoteID", "Zipcode", "State", "Municipality",
    "email_id", "channel_group", "channel_detail", "MarketingToken", "match_tier",
]


def tag_channels(cleaned_rows):
    """Assigns channel_group/channel_detail/match_tier to every cleaned
    row (from clean_row()), including the click-ID reuse-dedup pass.
    Returns (output_rows, guardrail_notes) where output_rows is a list
    of dicts with exactly OUTPUT_COLUMNS."""
    working = []
    for raw in cleaned_rows:
        tags = _assign_channel_pre_reuse(raw)
        entry = dict(tags)
        entry["_raw"] = raw
        working.append(entry)

    demoted_count = _demote_reused_click_ids(working)

    output_rows = []
    for entry in working:
        raw = entry["_raw"]
        output_rows.append({
            "InsertDate": raw.get("InsertDate"),
            "AvailabilityID": raw.get("AvailabilityID"),
            "QuoteID": raw.get("QuoteID"),
            "Zipcode": raw.get("Zipcode"),
            "State": raw.get("State"),
            "Municipality": raw.get("NJPR_municipalityName") or "",
            "email_id": raw.get("email_id"),
            "channel_group": entry["channel_group"],
            "channel_detail": entry["channel_detail"],
            "MarketingToken": raw.get("MarketingToken"),
            "match_tier": entry["match_tier"],
        })

    guardrail_notes = {
        "reused_click_ids_demoted": demoted_count,
    }
    return output_rows, guardrail_notes


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

_EMAIL_ID_HEX64 = re.compile(r"^[0-9A-Fa-f]{64}$")

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


def build_guardrail_report(raw_rows, cleaned_count, invalid_availability_count,
                            unresolved_state_count, tag_notes, output_rows):
    """Flags data-quality concerns -- NEVER silently fixes/drops without
    reporting. Compares today's per-day counts against the previous run's
    snapshot (SNAPSHOT_PATH); the very first run has nothing to compare
    against and says so explicitly rather than pretending the check
    passed."""
    report = {"warnings": [], "info": []}

    total_raw = len(raw_rows)
    report["info"].append(f"Raw rows fetched: {total_raw:,}")
    report["info"].append(f"Rows dropped for invalid/missing AvailabilityID: {invalid_availability_count:,}")
    if unresolved_state_count:
        pct = unresolved_state_count / max(cleaned_count, 1) * 100
        report["info"].append(f"Rows with unresolved State (zip3 outside known ranges): {unresolved_state_count:,} ({pct:.1f}%)")

    # Shape validation fallback -- email_id expected to be 64-char hex.
    bad_email_shape = sum(
        1 for r in output_rows
        if r.get("email_id") and not _EMAIL_ID_HEX64.match(str(r["email_id"]).strip())
    )
    if bad_email_shape:
        report["warnings"].append(
            f"{bad_email_shape:,} row(s) have an email_id that is NOT 64-char hex -- "
            f"dedup-by-submitter may be unreliable for these; verify the field mapping."
        )

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

    if tag_notes.get("reused_click_ids_demoted"):
        report["info"].append(
            f"{tag_notes['reused_click_ids_demoted']:,} row(s) shared a gclid/fbclid/msclkid/ndclid with "
            f"an earlier submission more than 60 minutes prior and lacked independent UTM evidence -- "
            f"demoted out of Paid-via-click-id and re-evaluated on their remaining signals."
        )

    confirm_counts = {}
    for r in output_rows:
        if r["channel_group"] == "Offline-Referral" and "confirm" in str(r["channel_detail"]).lower():
            confirm_counts[r["channel_detail"]] = confirm_counts.get(r["channel_detail"], 0) + 1
    for label, count in sorted(confirm_counts.items(), key=lambda kv: -kv[1]):
        report["info"].append(f"Offline-Referral '{label}': {count:,} row(s) -- pattern-classified, not confirmed by a human.")

    # Most recent day is usually partial -- label, don't silently average.
    if raw_rows:
        latest_day = max((r["InsertDate"].date() for r in raw_rows if r.get("InsertDate")), default=None)
        if latest_day == _today():
            report["info"].append(f"Most recent day ({latest_day}) is today and is very likely partial -- exclude or label it in any per-day average.")

    return report


def run_cleaning():
    """Full pipeline entry point. Returns (output_rows, guardrail_report)
    -- output_rows is a list of dicts shaped exactly like OUTPUT_COLUMNS,
    one row per surviving (cleaned + tagged) raw submission, NOT
    deduplicated (see module docstring). Never raises; a fetch failure
    comes back as an empty output_rows plus a guardrail report explaining
    why."""
    raw_rows, ok, error = fetch_raw_rows()
    if not ok:
        return [], {"warnings": [f"Could not fetch marketing data: {error}"], "info": []}

    cleaned_rows = []
    invalid_availability_count = 0
    for raw in raw_rows:
        cleaned = clean_row(raw)
        if cleaned is None:
            invalid_availability_count += 1
            continue
        cleaned_rows.append(cleaned)

    unresolved_state_count = sum(1 for r in cleaned_rows if r["State"] == "Unknown")

    output_rows, tag_notes = tag_channels(cleaned_rows)
    guardrail_report = build_guardrail_report(
        raw_rows, len(cleaned_rows), invalid_availability_count,
        unresolved_state_count, tag_notes, output_rows,
    )
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
# Shapes output_rows (one row per raw, TAGGED, NOT deduplicated submission
# -- see run_cleaning()'s docstring) into the compact `const D` object the
# generated report's client-side JS expects. Deduplication-by-submitter
# is NOT done here -- it happens in the browser, scoped to whichever date
# range the viewer currently has selected (spec: dedup must happen AFTER
# date filtering, never before). This function's only job is to make the
# ~15k raw rows small enough to embed inline: repeated strings (zip/
# municipality/state, channel group/detail, and the 64-char email_id
# hash) are each replaced with a small integer index into a lookup table.

# Fixed section order the generated report always uses -- NOT the order
# distinct (channel_group, channel_detail) pairs happen to appear in the
# data. chans[] is built in this order (a group's detail values grouped
# together, groups in this order) so the report's per-channel table can
# render every group even if a given day/filter combo happens to have
# zero rows in it.
CHANNEL_GROUP_ORDER = ["Paid", "Email", "Offline-Referral", "AI-Referral", "Organic-Direct"]

# Total paid ad spend covering the same window every payload spans
# (BASE_START_DATE through today) -- there is still no live ad-spend data
# source wired into this pipeline, so this is a manually-maintained
# figure, confirmed real by the marketing team 2026-08-20. It does NOT
# auto-update as new days of data arrive: the report's own spend input
# prorates this total by day-count share of the range (see the generated
# report's applySpendProration()), so as the live pipeline's date range
# keeps growing day over day while this number stays fixed, the
# auto-prorated CPA will drift further from reality the longer this goes
# un-refreshed. Update this constant whenever a current total is
# available, or clear it to 0 to fall back to "enter spend manually" (the
# report treats 0/falsy as unset, never as literal $0 spend).
TOTAL_AD_SPEND_USD = 195675


def build_dashboard_payload(output_rows):
    """Returns a JSON-serializable dict matching the generated report's
    `const D` shape: day0/maxday, spend_total (TOTAL_AD_SPEND_USD --
    see that constant's own comment for why this needs periodic manual
    upkeep), raw_rows (count, pre-dedup), zips[], chans[], and the
    parallel per-entry arrays d/zi/s/a/c/e."""
    if not output_rows:
        return {"day0": date.today().isoformat(), "maxday": 0, "spend_total": 0, "raw_rows": 0,
                "zips": [], "chans": [], "d": [], "zi": [], "s": [], "a": [], "c": [], "e": []}

    day0 = min(r["InsertDate"].date() for r in output_rows if r["InsertDate"])

    zip_index = {}
    zips = []
    chan_index = {}
    chans = []
    # Build chans[] in CHANNEL_GROUP_ORDER first, so index 0..4 are always
    # the (group, group's-first-detail) in a predictable order; any
    # further detail values within a group are appended as encountered.
    detail_by_group = {}
    for r in output_rows:
        detail_by_group.setdefault(r["channel_group"], set()).add(r["channel_detail"])
    for group in CHANNEL_GROUP_ORDER:
        for detail in sorted(detail_by_group.get(group, [])):
            chan_index[(group, detail)] = len(chans)
            chans.append({"g": group, "d": detail})

    email_index = {}
    d_arr, zi_arr, s_arr, a_arr, c_arr, e_arr = [], [], [], [], [], []

    for r in output_rows:
        insert_date = r["InsertDate"]
        if insert_date is None:
            continue
        day_idx = (insert_date.date() - day0).days

        zip_key = (r["Zipcode"] or "", r["Municipality"] or "", r["State"] or "Unknown")
        if zip_key not in zip_index:
            zip_index[zip_key] = len(zips)
            zips.append({"z": zip_key[0], "m": zip_key[1], "s": zip_key[2]})
        zi = zip_index[zip_key]

        chan_key = (r["channel_group"], r["channel_detail"])
        c = chan_index[chan_key]

        email_id = r.get("email_id") or f"__missing_{len(e_arr)}"
        if email_id not in email_index:
            email_index[email_id] = len(email_index)
        e = email_index[email_id]

        d_arr.append(day_idx)
        zi_arr.append(zi)
        s_arr.append(int(r["AvailabilityID"]))
        a_arr.append(1 if r["channel_group"] == "Paid" else 0)
        c_arr.append(c)
        e_arr.append(e)

    return {
        "day0": day0.isoformat(),
        "maxday": max(d_arr) if d_arr else 0,
        "spend_total": TOTAL_AD_SPEND_USD,
        "raw_rows": len(output_rows),
        "zips": zips,
        "chans": chans,
        "d": d_arr, "zi": zi_arr, "s": s_arr, "a": a_arr, "c": c_arr, "e": e_arr,
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
