"""Centralized Paid Marketing Attribution logic for the Marketing Dashboard
(marketing_metrics.py) and the Admin Portal's Marketing Form Data >
Attribution Quality panel.

ONE function -- attribute_record() -- is the single source of truth for
"is this form submission a paid marketing lead, and which platform gets
credit for it". Every component that needs to know (every KPI/timeline/
platform/municipality/heatmap/campaign aggregation in marketing_metrics.py,
plus the Admin Portal's Attribution Quality debugging panel) calls this
same function on the same row shape -- nothing recomputes UTM/click-ID/
_gcl_aw rules independently, so attribution can never drift between
components.

One form submission is always exactly ONE lead, and gets exactly ONE
attribution result. Three tiers, checked in order, first match wins:

  Tier 1 -- utm_source/utm_medium (see PAID_UTM_SOURCE_PLATFORMS/
            PAID_UTM_MEDIUMS below)
  Tier 2 -- click-ID fields (gclid, gad_source, gbraid, fbclid, msclkid,
            ndclid, tw_source == "google")
  Tier 3 -- Google's _gcl_aw click recovery, only trusted when the
            recovered click happened within one hour of InsertDate

A record is never double-counted across tiers, and never counted for more
than one platform just because multiple tracking fields happen to be
populated at once.
"""

import base64
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# InsertDate on PlanetWeb is written in America/New_York local time, same
# convention documented in sales_metrics.py for sale_date -- needed here
# only to make a naive InsertDate timezone-aware before comparing it
# against decode_gcl_aw()'s UTC click timestamp (see _tier3() below).
EASTERN_TZ = ZoneInfo("America/New_York")


def _norm(value):
    """Trim + lowercase for comparison. None/NaN/whitespace-only all
    normalize to "" so every caller can treat "" as "not populated"
    without special-casing None separately. Never raises."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    return text.lower()


def _has_value(value):
    """True if `value` is a real, non-whitespace value -- used for the
    Tier 2 click-ID presence checks, where a blank string or an
    all-whitespace column value must count as "not populated", never as a
    populated-but-empty click ID."""
    return _norm(value) != ""


# Tier 1 -- utm_source values that are unambiguously paid, mapped to the
# platform that gets credit (comparison always against the normalized --
# trimmed + lowercased -- value, see _norm()).
PAID_UTM_SOURCE_PLATFORMS = {
    "meta_fb": "Meta",
    "meta_ig": "Meta",
    "meta_an": "Meta",
    "meta_th": "Meta",
    "ig": "Meta",
    "adwords": "Google Ads",
    "bing": "Bing",
    "next-door": "Nextdoor",
    "reddit": "Reddit",
    # Landing-page builders -- reliably PAID in this app's traffic, but
    # neither name identifies a specific ad platform on its own, so they
    # classify as paid without being mis-labeled Google/Meta (spec: "do
    # not arbitrarily call them Google or Meta unless another reliable
    # field identifies the actual platform").
    "unbounce": "Other Paid",
    "leadpages": "Other Paid",
}

# Tier 1 -- utm_medium values that mark a record paid even when
# utm_source isn't one of the known sources above. A medium-only match has
# no source to read a platform from, so it always classifies as "Other
# Paid" here -- Tier 2/3 below are what can still recover a specific
# platform for a record like this, if a click ID or _gcl_aw is present.
PAID_UTM_MEDIUMS = {"paid-social", "ppc", "paid"}

PLATFORMS = ["Google Ads", "Meta", "Bing", "Nextdoor", "Reddit", "Other Paid"]


def _tier1(row):
    source = _norm(row.get("utm_source"))
    medium = _norm(row.get("utm_medium"))

    if source in PAID_UTM_SOURCE_PLATFORMS:
        return PAID_UTM_SOURCE_PLATFORMS[source], "utm_source"
    if medium in PAID_UTM_MEDIUMS:
        return "Other Paid", "utm_medium"
    return None, None


# Tier 2 -- click-ID fields, checked in this order; the first one
# actually populated wins (one lead, one attribution -- two populated
# click IDs never count as two platforms). Order matches the spec's own
# listing.
_CLICK_ID_RULES = [
    ("gclid", "Google Ads"),
    ("gad_source", "Google Ads"),
    ("gbraid", "Google Ads"),
    ("fbclid", "Meta"),
    ("msclkid", "Bing"),
    ("ndclid", "Nextdoor"),
]


def _tier2(row):
    for field, platform in _CLICK_ID_RULES:
        if _has_value(row.get(field)):
            return platform, field
    if _norm(row.get("tw_source")) == "google":
        return "Google Ads", "tw_source"
    return None, None


# Tier 3 -- Google's _gcl_aw click recovery. View_FormDataAnalytics has no
# dedicated _gcl_aw column, so this looks for a _gcl_aw token inside the
# three free-text fields that can carry raw querystring/referral data --
# uniqueURL, ReferralData, MarketingToken, in that order -- and uses the
# first one found.
#
# Google's real linker-parameter format packs _gcl_aw INSIDE a single
# _gl= query parameter, asterisk-delimited alongside other sub-values
# (typically _gcl_au right after it): e.g.
#   ?_gl=1*<id>*_gcl_aw*<VALUE>*_gcl_au*<other-value>
# NOT a standalone "_gcl_aw=<value>" parameter -- confirmed against real
# captured uniqueURL values (~1,069 real rows carry one). An earlier
# version of this pattern looked for "=" and matched zero of them.
_GCL_AW_PATTERN = re.compile(r"_gcl_aw\*([^*&\s]+)")

# A recovered click is only trusted if it happened within this many
# seconds of the form submission (InsertDate) -- spec: "within the same
# hour as the form submission". Anything older is stale and must NOT be
# classified as paid.
_GCL_AW_MAX_AGE_SECONDS = 3600


def _extract_gcl_aw(row):
    for field in ("uniqueURL", "ReferralData", "MarketingToken"):
        raw = row.get(field)
        if not raw:
            continue
        match = _GCL_AW_PATTERN.search(str(raw))
        if match:
            return match.group(1)
    return None


def decode_gcl_aw(raw_value):
    """Decodes a Google `_gcl_aw` token into (gclid, click_datetime_utc).
    Expected decoded shape is Google's own "GCL.<unix_timestamp>.<gclid>"
    cookie format (a literal "GCL" tag, the click's unix timestamp, then
    the gclid itself, dot-separated).

    Returns (None, None) on ANY failure -- malformed base64, wrong inner
    format, non-numeric timestamp, anything -- this must never raise,
    since a bad/garbled value here can never be allowed to break the rest
    of the Marketing Dashboard (spec: "Parsing failures must never break
    the Marketing Dashboard").

    Google terminates the raw value with one or more literal "."
    characters instead of standard "=" padding -- confirmed against real
    captured values, which otherwise fail to decode ("Incorrect
    padding") -- so those are stripped before re-padding with "="."""
    if not raw_value or not str(raw_value).strip():
        return None, None
    try:
        raw = str(raw_value).strip().rstrip(".")
        padded = raw + "=" * (-len(raw) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8", errors="strict")
        parts = decoded.split(".")
        if len(parts) < 3:
            return None, None
        timestamp = int(parts[1])
        gclid = ".".join(parts[2:])
        if not gclid:
            return None, None
        click_dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        return gclid, click_dt
    except Exception:
        return None, None


def _tier3(row):
    raw = _extract_gcl_aw(row)
    if not raw:
        return None, None
    gclid, click_dt = decode_gcl_aw(raw)
    if not gclid or click_dt is None:
        return None, None

    insert_date = row.get("InsertDate")
    if insert_date is None:
        return None, None
    try:
        insert_dt = insert_date if insert_date.tzinfo else insert_date.replace(tzinfo=EASTERN_TZ)
        age_seconds = abs((insert_dt.astimezone(timezone.utc) - click_dt).total_seconds())
    except Exception:
        return None, None

    if age_seconds <= _GCL_AW_MAX_AGE_SECONDS:
        return "Google Ads", "_gcl_aw"
    return None, None


def attribute_record(row):
    """The one attribution function. `row` is a dict (or dict-like/
    pandas-Series) exposing at least: utm_source, utm_medium, gclid,
    gad_source, gbraid, fbclid, msclkid, ndclid, tw_source, InsertDate,
    and (for Tier 3) uniqueURL/ReferralData/MarketingToken. Missing keys
    are treated as empty/absent -- never raises.

    Returns {isPaid, paidPlatform, attributionTier, attributionMethod},
    exactly one tier winning per the precedence documented in this
    module's docstring."""
    platform, method = _tier1(row)
    if platform:
        return {"isPaid": True, "paidPlatform": platform, "attributionTier": 1, "attributionMethod": method}

    platform, method = _tier2(row)
    if platform:
        return {"isPaid": True, "paidPlatform": platform, "attributionTier": 2, "attributionMethod": method}

    platform, method = _tier3(row)
    if platform:
        return {"isPaid": True, "paidPlatform": platform, "attributionTier": 3, "attributionMethod": method}

    return {"isPaid": False, "paidPlatform": None, "attributionTier": None, "attributionMethod": None}
