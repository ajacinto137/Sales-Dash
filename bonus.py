"""Bonus Predictions (Tools > Bonus Predictions, added 2026-10-28).

Each outbound sale's bonus is its tier multiplier x its plan's monthly
price (FTTPRates.MRC) -- there is no base amount (rules given by request,
2026-10-28), e.g. GPON 250 = 1 x $59.95, 500 = 1.25 x $79.95,
1 Gig = 1.5 x $99.95:

    GPON / XGSPON   Tier 1 = 1x, Tier 2 = 1.25x, Tier 3 = 1.5x
    Single Tier     1x
    Door to Door    flat $200, whatever the plan

Market type and tier come from the order's plan (FTTPFormData.RateID ->
FTTPRates); the one plan shared by XGSPON and Single Tier markets
(1 Gig $49.95) is told apart by FTTPFormData.MarketID. Both pay 1x
either way.

    GPON     100 / 250 = Tier 1, 500 = Tier 2, 1 Gig = Tier 3
             (NJ, PA, VA three/two tier markets; 100 counts as Tier 1)
    XGSPON   1 Gig = Tier 1, 2.5 Gig = Tier 2, 5 Gig = Tier 3
             (Hopatcong, Butler, Stanhope, Netcong, NY Multi-Gig, PA XGSPON)
    Single   1 Gig $49.95 only (Jefferson, Vernon, Hardyston, Franklin,
             Hamburg, Ogdensburg, Kinnelon, NY State)

Bonuses are paid only on OUTBOUND sales (sales_metrics.OUTBOUND_CHANNELS)
that INSTALL (by request, 2026-10-28): Earned = installed outbound sales;
Predicted = Earned + outbound sales still Pending install. Needs
Attention and Cancelled sales, and every inbound/web sale, pay nothing.
Door to Door sales are kept out of the tier counts.

Residential and business plans tier the same way. Any other plan (old VA
Kinex rates, legacy promos) is "Other" and earns nothing until it's
given a tier here."""

import pandas as pd

from sales_metrics import OUTBOUND_CHANNELS, _rep_sales_with_month

GPON = "GPON"
XGSPON = "XGSPON"
SINGLE_TIER = "Single Tier"
OTHER = "Other"

MULTIPLIERS = {1: 1.0, 2: 1.25, 3: 1.5}
DOOR_TO_DOOR_CHANNEL = "Door to Door"
DOOR_TO_DOOR_BONUS = 200.0
SINGLE_TIER_MULTIPLIER = 1.0

# FTTPMarkets IDs whose 1 Gig $49.95 plan is XGSPON Tier 1, not Single Tier.
XGSPON_MARKET_IDS = {14, 15, 16, 18, 19, 45}

# FTTPRates.ID -> tier. Residential then business.
GPON_RATE_TIERS = {
    # Residential: 100, 100/100, 250, 500, 1 Gig (+ 1 Gig prepaid)
    57: 1, 90: 1, 24: 1, 84: 1,
    25: 2, 85: 2, 5: 2,
    26: 3, 86: 3, 6: 3, 12: 3, 14: 3, 18: 3,
    # Business: 100, 100/100, 250, 500, 1 Gig
    79: 1, 91: 1, 27: 1, 87: 1,
    28: 2, 88: 2, 10: 2,
    29: 3, 89: 3, 11: 3,
}
XGSPON_RATE_TIERS = {80: 2, 82: 2, 81: 3, 83: 3}
SHARED_1GIG_RATE_IDS = {58, 59}  # XGSPON Tier 1 or Single Tier, by market

# Prepaid 1 Gig plans have no monthly price (MRC 0); they pay on the
# GPON 1 Gig monthly price instead.
PRICE_OVERRIDES = {12: 99.95, 14: 99.95, 18: 99.95}

TIER_COLUMNS = [
    ("gpon_1", GPON, 1), ("gpon_2", GPON, 2), ("gpon_3", GPON, 3),
    ("xgs_1", XGSPON, 1), ("xgs_2", XGSPON, 2), ("xgs_3", XGSPON, 3),
    ("single", SINGLE_TIER, None),
]


def _int_or_none(value):
    try:
        if value is None or value != value:  # NaN
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def classify(rate_id, market_id):
    """(market type, tier or None, multiplier) for one sale."""
    rate_id = _int_or_none(rate_id)
    market_id = _int_or_none(market_id)
    if rate_id in GPON_RATE_TIERS:
        tier = GPON_RATE_TIERS[rate_id]
        return GPON, tier, MULTIPLIERS[tier]
    if rate_id in XGSPON_RATE_TIERS:
        tier = XGSPON_RATE_TIERS[rate_id]
        return XGSPON, tier, MULTIPLIERS[tier]
    if rate_id in SHARED_1GIG_RATE_IDS:
        if market_id in XGSPON_MARKET_IDS:
            return XGSPON, 1, MULTIPLIERS[1]
        return SINGLE_TIER, None, SINGLE_TIER_MULTIPLIER
    return OTHER, None, 0.0


def sale_bonus(rate_id, market_id, price, channel):
    """Dollar bonus one sale pays once installed: $200 flat for Door to
    Door, else multiplier x the plan's monthly price, 0 for any other
    channel or an untiered plan."""
    if channel == DOOR_TO_DOOR_CHANNEL:
        return DOOR_TO_DOOR_BONUS
    if channel not in OUTBOUND_CHANNELS:
        return 0.0
    multiplier = classify(rate_id, market_id)[2]
    price = PRICE_OVERRIDES.get(_int_or_none(rate_id), price)
    try:
        price = float(price)
    except (TypeError, ValueError):
        return 0.0
    if price != price:  # NaN
        return 0.0
    return round(multiplier * price, 2)


def _column_key(market_type, tier):
    for key, col_type, col_tier in TIER_COLUMNS:
        if col_type == market_type and col_tier == tier:
            return key
    return None


def _with_bonus(sales):
    """Outbound rows of `sales` with a `_bonus` dollar column."""
    outbound = sales[sales["sales_channel"].isin(OUTBOUND_CHANNELS)]
    return outbound.assign(_bonus=[
        sale_bonus(r, m, p, c) for r, m, p, c in zip(
            outbound["rate_id"], outbound["market_id"], outbound["rate_mrc"], outbound["sales_channel"])
    ])


def bonus_amounts(sales):
    """{bonus_installed, bonus_pending, bonus_predicted} in dollars for any
    set of sales (outbound only; predicted = installed + pending)."""
    if sales is None or sales.empty:
        return {"bonus_installed": 0.0, "bonus_pending": 0.0, "bonus_predicted": 0.0}
    rows = _with_bonus(sales)
    installed = float(rows.loc[rows["status"] == "Installed", "_bonus"].sum())
    pending = float(rows.loc[rows["status"] == "Pending", "_bonus"].sum())
    return {"bonus_installed": installed, "bonus_pending": pending, "bonus_predicted": installed + pending}


def calculate_rep_bonuses(df):
    """One row per rep with outbound sales in the given (already
    period-filtered) sales: outbound counts per market type/tier (Door to
    Door counted on its own), installs, pending, and dollars earned
    (installed) / pending / predicted (installed + pending). Sorted by
    predicted bonus."""
    if df is None or df.empty:
        return []
    outbound = _with_bonus(df)
    if outbound.empty:
        return []
    classified = [classify(r, m) for r, m in zip(outbound["rate_id"], outbound["market_id"])]
    outbound = outbound.assign(
        _d2d=outbound["sales_channel"] == DOOR_TO_DOOR_CHANNEL,
        _type=[c[0] for c in classified],
        _tier=[c[1] for c in classified],
    )

    rows = []
    for rep, group in outbound.groupby("sales_rep"):
        tiered = group[~group["_d2d"]]
        row = {
            "sales_rep": rep,
            "sales": len(group),
            "installs": int((group["status"] == "Installed").sum()),
            "pending": int((group["status"] == "Pending").sum()),
            "other": int((tiered["_type"] == OTHER).sum()),
            "d2d": int(group["_d2d"].sum()),
        }
        for key, _type, _tier in TIER_COLUMNS:
            row[key] = 0
        for (market_type, tier), sub in tiered.groupby(["_type", "_tier"], dropna=False):
            key = _column_key(market_type, None if pd.isna(tier) else int(tier))
            if key:
                row[key] = len(sub)
        row["bonus_installed"] = float(group.loc[group["status"] == "Installed", "_bonus"].sum())
        row["bonus_pending"] = float(group.loc[group["status"] == "Pending", "_bonus"].sum())
        row["bonus_predicted"] = row["bonus_installed"] + row["bonus_pending"]
        rows.append(row)

    rows.sort(key=lambda r: (-r["bonus_predicted"], -r["bonus_installed"], r["sales_rep"]))
    return rows


def summarize(rows):
    """Team totals over calculate_rep_bonuses() rows."""
    keys = ["sales", "installs", "pending", "other", "d2d"] + [k for k, _t, _n in TIER_COLUMNS] + [
        "bonus_installed", "bonus_pending", "bonus_predicted"]
    return {k: sum(r[k] for r in rows) for k in keys}


def calculate_rep_monthly_bonus(df, rep):
    """Bonus per Rep Profile Monthly Status month -- the same buckets
    (sales_metrics._rep_sales_with_month(), scheduled install month), so a
    sale's bonus lands in the month it installs / is scheduled to.
    {"months": {(year, month): bonus_amounts()}, "totals": bonus_amounts()}."""
    sales = _rep_sales_with_month(df, rep)
    if sales.empty:
        return {"months": {}, "totals": bonus_amounts(sales)}
    months = {
        (period.year, period.month): bonus_amounts(group)
        for period, group in sales.dropna(subset=["_status_month"]).groupby("_status_month")
    }
    return {"months": months, "totals": bonus_amounts(sales)}
