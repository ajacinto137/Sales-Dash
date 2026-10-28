"""Lead Feeder rules (added 2026-10-28) -- screening, buying signals,
lead kinds and Priority scoring, ported line for line from the original
claude.ai Lead Feeder page (lead-feeder.html by Reiss Little: prescreen(),
isCustomer(), sweepReason(), signals(), leadKind(), scoreLead(), rank()).
Pure functions over one contact's HubSpot properties (a dict of strings,
as HubSpot returns them) -- no I/O, so tests can pin the behavior.
lead_feeder_store.py does the HubSpot reads, claims and caching.

Where this differs from the original page, it's on purpose and noted
inline ("Port:"). The one rule-level difference: the page computed
"today" and the cool-down in the viewer's browser time zone; here both
are Eastern (BUSINESS_TZ), the zone every rep works in."""

import math
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BUSINESS_TZ = ZoneInfo("America/New_York")
DAY = timedelta(days=1)

PORTAL_ID = "23875264"

# ---------- screening constants (verbatim from the page) ----------
BAD_NOTE = re.compile(
    r"\b(dnc|do not call|don'?t call|stop calling|sold|sale\b|moved|wrong number|not interested|employee|"
    r"works for planet|deceased|passed away|no longer (lives|resides))", re.I)
JUNK_NAME = re.compile(r"(fake|test|john doe|asdf|qwer|xxx|\bnope\b|\bnone\b|\bn/a\b)", re.I)
BIZ = re.compile(r"(fire department|salon|church|school|shoprite|llc|\binc\b|industrial|\bste\b|suite|corp\b)", re.I)
BAD_STATUS = ["Do Not Contact - Catalyst", "Customer", "OBJ Cable", "HWCS - Mobile HotSpot", "HWCS - Optimum",
              "HWCS - Starlink", "Interested/ Not Serviceable", "Moving"]
MISSED_STAGES = ["lead", "marketingqualifiedlead", "opportunity", "other"]
QUIET_DAYS = 30
STAGE_NAME = {"lead": "Lead", "marketingqualifiedlead": "MQL", "opportunity": "Opportunity", "other": "Other"}
PROPS = ["firstname", "lastname", "phone", "address", "city", "state", "zip", "notes_last_contacted", "of_calls",
         "hs_email_optout", "sakari_opt_out", "sakari_message_sent_message", "hs_lead_status", "prequal_date",
         "lifecyclestage", "hubspot_owner_id", "availability", "has_service",
         "hasaserviceatthisplace", "service_plan_id", "client_id", "hs_current_customer", "hs_v2_date_entered_customer",
         "recent_deal_amount", "hs_v2_date_entered_salesqualifiedlead", "hs_sales_email_last_replied",
         "hs_last_sales_activity_timestamp", "prequal_count", "likely_to_switch", "starting_avail_status",
         "availability_date", "mobilephone",
         # Port: email added so Lead Feeder sales can be matched to PlanetWeb orders (admin view).
         "email"]
PRE_SERVICE = ["0", "2", "3", "4", "5", "6"]    # signed up before Planet could serve them
NONORDER = "90871183"                           # HubSpot stage "Customer - Non Ordering"
SWEEP_BAD_STAGES = ["customer", "93166937", "90583214", "1068448346", "subscriber"]
OK_STATES = re.compile(r"^(nj|new jersey|va|virginia)$", re.I)
STATE_VALS = {"all": ["NJ", "New Jersey", "VA", "Virginia"], "VA": ["VA", "Virginia"], "NJ": ["NJ", "New Jersey"]}
STATE_RE = {"VA": re.compile(r"^(va|virginia)$", re.I), "NJ": re.compile(r"^(nj|new jersey)$", re.I)}
AREAS = {
    "farmville": {"state": "VA", "label": "Farmville area", "towns": [
        "farmville", "keysville", "prospect", "rice", "hampden sydney", "hampden-sydney", "burkeville", "crewe",
        "meherrin", "green bay", "pamplin", "pamplin city", "cumberland", "dillwyn", "buckingham", "appomattox",
        "charlotte court house", "drakes branch", "blackstone", "victoria", "kenbridge", "amelia",
        "amelia court house", "scottsburg", "phenix", "darlington heights", "cullen", "saxe", "red oak", "randolph",
        "nottoway", "jetersville", "spout spring"]},
}
COOL_CHOICES = [1, 2, 3, 5, 7, 14]

# Every per-rep setting the page kept in localStorage, with the page's defaults.
DEFAULT_PREFS = {
    "state": "all", "area": "all", "cool_days": 3,
    "quiet_on": True,            # lf_quiet2 defaulted to on
    "sweep_on": True, "sweep_active_on": False, "sweep_manual_on": False,
    "auto_hs": True, "auto_maps": True,
}

# ---------- scoring constants ----------
AGE_PTS = [7, 14, 30, 60, 90, 180, 365]
CALL_BUCKETS = [(0, 0, "0"), (1, 1, "1"), (2, 2, "2"), (3, 3, "3"), (4, 4, "4"), (5, 5, "5"), (6, 7, "6–7"),
                (8, 10, "8–10"), (11, 15, "11–15"), (16, 20, "16–20"), (21, 10 ** 9, "21+")]
# The page's fallback before its live measurement: full-database numbers taken Oct 21 2026.
FALLBACK_CONV = {"n": 18196, "le": {7: 8977, 30: 11503, 90: 13650}}
DEFAULT_WEIGHTS = {"cameBack": 1.6, "hot": 1.6, "stalled": 1.6, "newly": 1.4, "repeatStep": 0.1, "repeatMax": 0.4,
                   "lts9": 1.15, "lts46": 0.85, "rested": 1.15}
TIERS = {1: "P1 Call now", 2: "P2 High", 3: "P3 Standard", 4: "P4 Reactivate", 5: "P5 Low"}
SOURCE_LABELS = {"sql": "Open SQL", "missed": "Fell through", "quiet": "Owned, gone quiet", "nonorder": "Non-ordering",
                 "sweep": "PAS sweep", "priority": "Call first"}
OUTCOMES = ["No answer", "Left VM", "Talked", "Sale", "Not interested", "Skipped"]
# Outcomes after which the page took the next lead automatically.
AUTO_NEXT_OUTCOMES = {"No answer", "Left VM", "Skipped", "Called"}


# ================================================================
# small helpers
# ================================================================

def parse_ts(value):
    """HubSpot datetime/date property -> aware UTC datetime, or None.
    Accepts ISO strings (with or without time) and epoch milliseconds."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        if text.isdigit():
            return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc)
        if len(text) == 10:
            return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def day_start(now):
    """Eastern midnight of `now`'s Eastern day, as an aware datetime."""
    local = now.astimezone(BUSINESS_TZ)
    return datetime(local.year, local.month, local.day, tzinfo=BUSINESS_TZ)


def cool_cutoff(now, cool_days):
    """Called at or after this = inside the cool-down (1 = today)."""
    return day_start(now) - (cool_days - 1) * DAY


def yes(value):
    return bool(re.fullmatch(r"(yes|true|1)", str(value if value is not None else "").strip(), re.I))


def digits(phone):
    d = re.sub(r"\D", "", str(phone or ""))
    return d[1:] if len(d) == 11 and d.startswith("1") else d


def fmt_phone(phone):
    d = digits(phone)
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else str(phone or "")


def full_name(p):
    return f"{p.get('firstname') or ''} {p.get('lastname') or ''}".strip()


def lead_addr(p):
    return ", ".join(str(p[k]) for k in ("address", "city", "state", "zip") if p.get(k))


def _num(value, default=0.0):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(n) else n


def calls_of(p):
    return int(_num(p.get("of_calls"), 0))


def junk_name(first, last):
    name = f"{first or ''} {last or ''}".strip()
    if not first or len(name) < 3:
        return True
    if JUNK_NAME.search(name) or BIZ.search(name):
        return True
    for word in name.split():
        if len(word) >= 4 and not re.search(r"[aeiouy]", word, re.I):
            return True
    return bool(re.search(r"(.)\1\1\1", name.replace(" ", ""), re.I))


def junk_phone(phone):
    d = digits(phone)
    return (len(d) != 10 or bool(re.match(r"[01]", d)) or bool(re.match(r"\d{3}555", d))
            or bool(re.search(r"(\d)\1{6}", d)))


def last_touch(p):
    """Latest of notes_last_contacted / hs_last_sales_activity_timestamp, or None."""
    stamps = [parse_ts(p.get(k)) for k in ("notes_last_contacted", "hs_last_sales_activity_timestamp")]
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None


def quiet_days(p, now):
    t = last_touch(p)
    return math.floor((now - t).total_seconds() / 86400) if t else None


def in_state(p, state_sel):
    return state_sel == "all" or bool(STATE_RE[state_sel].match(str(p.get("state") or "").strip()))


def in_area(p, area_sel):
    if area_sel == "all" or area_sel not in AREAS:
        return True
    town = re.sub(r"\s+", " ", str(p.get("city") or "").strip().lower())
    return town in AREAS[area_sel]["towns"]


def clean_prefs(raw):
    """Stored/posted prefs -> a complete, valid prefs dict (page defaults
    for anything missing). An area pins its state, like the page did."""
    prefs = dict(DEFAULT_PREFS)
    raw = raw or {}
    if raw.get("state") in STATE_VALS:
        prefs["state"] = raw["state"]
    if raw.get("area") in AREAS or raw.get("area") == "all":
        prefs["area"] = raw["area"]
    try:
        cool = int(raw.get("cool_days"))
        if cool in COOL_CHOICES:
            prefs["cool_days"] = cool
    except (TypeError, ValueError):
        pass
    for key in ("quiet_on", "sweep_on", "sweep_active_on", "sweep_manual_on", "auto_hs", "auto_maps"):
        if key in raw and raw[key] is not None:
            prefs[key] = bool(raw[key])
    if prefs["area"] != "all":
        prefs["state"] = AREAS[prefs["area"]]["state"]
    return prefs


# ================================================================
# customers, kinds, screening
# ================================================================

def is_customer(p):
    """Any sign this person is (or has been) a Planet customer."""
    if p.get("lifecyclestage") == NONORDER:
        if yes(p.get("has_service")) or yes(p.get("hasaserviceatthisplace")) or yes(p.get("hs_current_customer")):
            return True
        if p.get("service_plan_id") or p.get("client_id"):
            return True
        return _num(p.get("recent_deal_amount")) > 0
    if yes(p.get("has_service")) or yes(p.get("hasaserviceatthisplace")) or yes(p.get("hs_current_customer")):
        return True
    if p.get("service_plan_id") or p.get("client_id"):
        return True
    if p.get("hs_v2_date_entered_customer"):
        return True
    if _num(p.get("recent_deal_amount")) > 0:
        return True
    return bool(re.search("customer", str(p.get("hs_lead_status") or ""), re.I))


def sql_only(p):
    # RULE (Reiss): only lifecycle stage "Sales Qualified Lead". No customers,
    # non-ordering, MQL, Lead or Other, from any source.
    return p.get("lifecyclestage") == "salesqualifiedlead"


def sweep_owner(p):
    m = p.get("__sweep") or {}
    if not p.get("hubspot_owner_id"):
        return {"state": "none", "name": ""}
    if m.get("owner_name") and m.get("owner_inactive"):
        return {"state": "inactive", "name": m["owner_name"]}
    if m.get("owner_name"):
        return {"state": "active", "name": m["owner_name"]}
    return {"state": "active", "name": "", "changed": True}    # owner assigned after the sweep: treat as active


def lead_kind(p, now):
    """"sql" open SQL nobody owns · "quiet" owned SQL untouched 30+ days ·
    "sweep" PAS sweep · "priority" call-first list · "missed"/"nonorder"
    exist in the page's code but can never pass sql_only() (the page
    switched those sources off for the SQL-only rule)."""
    k = _lead_kind0(p, now)
    return k or ("priority" if p.get("__prio") else None)


def _lead_kind0(p, now):
    if p.get("__sweep"):
        return "sweep"
    stage = p.get("lifecyclestage")
    if stage == "salesqualifiedlead":
        if not p.get("hubspot_owner_id"):
            return "sql"
        q = quiet_days(p, now)
        return "quiet" if (q is None or q >= QUIET_DAYS) else None
    if stage == NONORDER and not p.get("hubspot_owner_id"):
        return "nonorder"
    if (stage in MISSED_STAGES and not p.get("hubspot_owner_id")
            and (p.get("hs_v2_date_entered_salesqualifiedlead") or p.get("prequal_date")
                 or str(p.get("starting_avail_status") or "").strip() in PRE_SERVICE)):
        return "missed"
    return None


def _called_within_cooldown(p, now, cool_days):
    t = parse_ts(p.get("notes_last_contacted"))
    return bool(t and t >= cool_cutoff(now, cool_days))


def _bad_status(p):
    return (p.get("hs_lead_status") in BAD_STATUS
            and not (p.get("lifecyclestage") == NONORDER and p.get("hs_lead_status") == "Customer"))


def sweep_reason(p, now, prefs):
    """Why a PAS sweep lead is screened out (None = keep). Same checks as
    prescreen() minus availability = 1 (PAS confirmed Available Now) and
    minus "no owner" (the toggles decide)."""
    m = p.get("__sweep") or {}
    cool_days = prefs["cool_days"]
    if not prefs["sweep_on"]:
        return "PAS sweep leads turned off"
    if not sql_only(p):
        return "Not a Sales Qualified Lead in HubSpot"
    if m.get("batch") == "check manually" and not prefs["sweep_manual_on"]:
        return "Marked check manually"
    if junk_name(p.get("firstname"), p.get("lastname")):
        return "No name, or a junk or business name"
    if junk_phone(p.get("phone")):
        return "Junk or missing phone"
    if BIZ.search(p.get("address") or ""):
        return "Business address"
    if str(p.get("hs_email_optout")) == "true":
        return "Opted out of email"
    if re.search("appointment|install", p.get("sakari_message_sent_message") or "", re.I):
        return "Already has an appointment or install"
    if _called_within_cooldown(p, now, cool_days):
        return "Called today" if cool_days == 1 else f"Called in the last {cool_days} days"
    if yes(p.get("hs_current_customer")):
        return "HubSpot marks them as a current customer"
    if p.get("lifecyclestage") in SWEEP_BAD_STAGES or is_customer(p):
        return "Customer, cancelled, DNC or unusable in HubSpot"
    if not OK_STATES.match(str(p.get("state") or "").strip()):
        return "Not in NJ or VA"
    if not p.get("address") or not p.get("city"):
        return "No address"
    if _bad_status(p):
        return f"Lead status: {p.get('hs_lead_status')}"
    if p.get("hs_lead_status") == "OBJ Price":
        q = quiet_days(p, now)
        if q is not None and q < 60:
            return "Price objection in the last 60 days"
    if sweep_owner(p)["state"] == "active" and not prefs["sweep_active_on"]:
        return "Owned by an active rep"
    return None


def sweep_fill(p, m):
    """Fill blanks from the sweep row; live HubSpot values always win."""
    if not p.get("phone"):
        p["phone"] = p.get("mobilephone") or m.get("phone") or ""
    for prop, key in (("firstname", "first_name"), ("lastname", "last_name"), ("state", "state"),
                      ("city", "city"), ("address", "street_address")):
        if not p.get(prop) and m.get(key):
            p[prop] = m[key]
    p["__sweep"] = m
    return p


def prescreen(p, now, prefs):
    """The lead's kind if it's callable for these prefs, else False."""
    if not sql_only(p):
        return False
    if p.get("__sweep"):
        return False if sweep_reason(p, now, prefs) else "sweep"
    if junk_name(p.get("firstname"), p.get("lastname")) or junk_phone(p.get("phone")) or BIZ.search(p.get("address") or ""):
        return False
    if str(p.get("hs_email_optout")) == "true":
        return False
    if re.search("appointment|install", p.get("sakari_message_sent_message") or "", re.I):
        return False
    if _called_within_cooldown(p, now, prefs["cool_days"]):
        return False
    if is_customer(p):
        return False
    # must be serviceable now (Call-first leads: HubSpot is often stale, check PAS)
    if not p.get("__prio") and str(p.get("availability") or "").strip() != "1":
        return False
    if not OK_STATES.match(str(p.get("state") or "").strip()):
        return False
    if not p.get("address") or not p.get("city"):
        return False
    if _bad_status(p):
        return False
    if p.get("hs_lead_status") == "OBJ Price":
        q = quiet_days(p, now)
        if q is not None and q < 60:       # price objections come back after 60 days
            return False
    kind = lead_kind(p, now)
    if kind == "quiet" and not prefs["quiet_on"]:
        return False                       # Port: the page simply didn't query these when the toggle was off
    return kind or False


# ================================================================
# buying signals
# ================================================================

def signals(p):
    s = {}
    if (str(p.get("starting_avail_status") or "").strip() in PRE_SERVICE
            and str(p.get("availability") or "").strip() == "1"):
        s["newly"] = True
    pc = _num(p.get("prequal_count"), 0)
    if pc >= 2:
        s["repeat"] = int(pc)
    lts_raw = p.get("likely_to_switch")
    if lts_raw not in (None, ""):
        lts = _num(lts_raw, None)
        if lts is not None:
            s["lts"] = lts
    pq, lc = str(p.get("prequal_date") or "")[:10], str(p.get("notes_last_contacted") or "")[:10]
    if pq and lc and pq > lc:
        s["cameBack"] = True
    if p.get("hs_lead_status") == "Go Back":
        s["goBack"] = True
    sweep = p.get("__sweep")
    if sweep and re.fullmatch("yes", str(sweep.get("hot") or "").strip(), re.I):
        s["hot"] = True
    # Signed up, got marked "Customer - Non Ordering" without an order, and barely called
    if p.get("lifecyclestage") == NONORDER and calls_of(p) <= 2:
        s["stalled"] = True
    if p.get("hs_lead_status") == "OBJ Price":
        s["price"] = True
    return s


def sig_list(p):
    out = []
    for k, v in signals(p).items():
        if not v:
            continue
        out.append(f"lts{v:g}" if k == "lts" else f"repeat{v}" if k == "repeat" else k)
    return out


# ================================================================
# Priority scoring
# ================================================================

def bucket_of(n):
    for i, (lo, hi, _) in enumerate(CALL_BUCKETS):
        if lo <= n <= hi:
            return i
    return -1


def call_rates(cust, open_):
    """Per-bucket conversion rate (None under 30 contacts), from customer
    and still-open SQL counts per call bucket."""
    return [c / (c + o) if (c + o) >= 30 else None for c, o in zip(cust, open_)]


def still_ahead(age, conv):
    """Share of historical SQL->sale conversions that happen AFTER a lead
    of this age in days (1 at day 0)."""
    n, le = conv.get("n"), conv.get("le") or {}
    if not n:
        return None
    pts = [(0, 1.0)] + [(d, 1 - le[d] / n) for d in sorted(int(k) for k in le)]
    if age is None:
        return pts[-1][1]
    for (d0, v0), (d1, v1) in zip(pts, pts[1:]):
        if age <= d1:
            return v0 + (v1 - v0) * (age - d0) / (d1 - d0)
    return pts[-1][1]


def call_rate(calls, rates):
    """{"v", "rel"} for this many calls, or None without call data. A
    never-called lead is scored like a 1-call lead (0-call conversions
    mostly ordered on their own)."""
    if not rates:
        return None
    i = max(1, bucket_of(calls))
    v = rates[i]
    j = i
    while v is None and j > 0:
        v = rates[j - 1]
        j -= 1
    known = [r for r in rates if r is not None]
    top = max(known) if known else 0
    if v is None or not top:
        return None
    return {"v": v, "rel": v / top}


def sql_age(p, now):
    t = parse_ts(p.get("hs_v2_date_entered_salesqualifiedlead") or p.get("prequal_date"))
    return max(0.0, (now - t).total_seconds() / 86400) if t else None


def score_lead(p, now, intel, weights=None):
    """{score 0-100, tier 1-5, why [..], age, replied} -- the page's scoreLead()."""
    W = weights or DEFAULT_WEIGHTS
    conv = (intel or {}).get("conv") or FALLBACK_CONV
    rates = ((intel or {}).get("calls") or {}).get("rate")
    age, calls = sql_age(p, now), calls_of(p)
    f, c = still_ahead(age, conv), call_rate(calls, rates)
    sg = signals(p)
    boost = 1.0
    if sg.get("cameBack"):
        boost *= W["cameBack"]
    if sg.get("hot"):
        boost *= W["hot"]
    if sg.get("stalled"):
        boost *= W["stalled"]
    if sg.get("newly"):
        boost *= W["newly"]
    if sg.get("repeat"):
        boost *= 1 + min(W["repeatMax"], W["repeatStep"] * (sg["repeat"] - 1))
    if sg.get("lts") is not None:
        boost *= W["lts9"] if sg["lts"] >= 9 else (W["lts46"] if 4 <= sg["lts"] <= 6 else 1)
    q = quiet_days(p, now)
    if q is not None and q >= 90:
        boost *= W["rested"]                     # rested: not dialed in 3+ months
    score = min(100, _js_round(100 * (f if f is not None else 0.25) * (c["rel"] if c else 1) * boost))
    if age is not None and age <= 7:
        tier = 1                                 # ~half of all SQL->sale conversions happen in the first 7 days
    elif age is not None and age <= 30:
        tier = 2
    elif age is not None and age <= 90:
        tier = 3
    else:
        tier = 4 if (c and c["rel"] >= 0.6) else 5
    # Older leads with a real buying signal jump the line
    if sg.get("cameBack") or sg.get("hot") or sg.get("stalled"):
        tier = min(tier, 2)
    if sg.get("newly") or (sg.get("repeat") and sg["repeat"] >= 3):
        tier = min(tier, 3)
    if age is None:
        age_txt = "SQL date unknown"
    elif age < 1:
        age_txt = "Became an SQL today"
    else:
        days = _js_round(age)
        age_txt = f"SQL {days} day{'' if days == 1 else 's'} ago"
    why = [f"{age_txt}: {_js_round((f or 0) * 100)}% of past SQL sales closed after this point"]
    if c and calls == 0:
        why.append("Never called")
    elif c:
        why.append(f"SQLs with {CALL_BUCKETS[bucket_of(calls)][2]} call{'' if calls == 1 else 's'} "
                   f"converted {_js_round(c['v'] * 100)}% of the time")
    replied_at = parse_ts(p.get("hs_sales_email_last_replied"))
    replied = bool(replied_at and (now - replied_at) < 30 * DAY)
    if replied:
        why.append("Replied to a sales email in the last 30 days")
    if sg.get("cameBack"):
        why.append("Filled out a new prequal after their last call")
    if sg.get("hot"):
        why.append("Marked Hot in the PAS sweep")
    if sg.get("stalled"):
        why.append("Signed up but never ordered, and barely called")
    prio = p.get("__prio") or {}
    if prio.get("reason"):
        why.insert(0, prio["reason"])
    if sg.get("newly"):
        why.append("Signed up before their address was serviceable (these close 93% vs 88%)")
    if sg.get("repeat"):
        why.append(f"Filled out the prequal {sg['repeat']} times")
    if q is not None and q >= 90:
        why.append(f"Not dialed in {q} days")
    return {"score": score, "tier": tier, "why": why, "age": age, "replied": replied}


def _js_round(x):
    """JavaScript Math.round (halves round up), so scores match the page."""
    return int(math.floor(x + 0.5))


def rank_key(lead):
    """Sort key: call-first list on top in its own `rank` order, then tier,
    score (high first), fewest calls."""
    p, s = lead["p"], lead["s"]
    prio = p.get("__prio")
    prio_rank = _num(prio.get("rank"), 999) if prio else 0
    return (0 if prio else 1, prio_rank, s["tier"], -s["score"], calls_of(p))


def lead_meta(p, s, now):
    """The page's leadMeta(): what was known about a lead when it was taken."""
    local = now.astimezone(BUSINESS_TZ)
    return {"tier": s["tier"], "score": s["score"], "kind": lead_kind(p, now) or "", "sig": sig_list(p),
            "state": p.get("state") or "", "calls": calls_of(p),
            "sqlDays": None if s["age"] is None else _js_round(s["age"]),
            "hour": local.hour, "dow": (local.weekday() + 1) % 7}


def verify_activity(items, now, cool_days):
    """The page's last pre-dial check over a contact's 5 latest calls and
    notes ([{ts, text}], newest first): (ok, why)."""
    cutoff = cool_cutoff(now, cool_days)
    for it in items:
        ts = parse_ts(it.get("ts"))
        if ts and ts >= cutoff:
            return False, "Someone logged a call today" if cool_days == 1 else f"Someone called them in the last {cool_days} days"
        text = it.get("text") or ""
        if text and BAD_NOTE.search(text):
            return False, f'Note says: "{text[:60]}"'
    return True, None


def body_text(html):
    """Call/note body HTML -> plain text (the page's bodyText())."""
    text = re.sub(r"<[^>]+>", " ", str(html or ""))
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(entity, char)
    return re.sub(r"\s+", " ", text).strip()
