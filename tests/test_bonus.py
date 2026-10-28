"""Tests for Bonus Predictions (bonus.py) -- pure pandas, no database
needed.

Run with: pytest tests/test_bonus.py -v
"""

import pandas as pd
import pytest

import bonus

# FTTPRates.MRC of each plan used below.
PRICES = {24: 59.95, 25: 79.95, 26: 99.95, 57: 49.95, 58: 49.95, 80: 74.95, 81: 99.95, 12: 0.0}


def sales(rows):
    """(rep, channel, status, rate_id, market_id) rows."""
    return pd.DataFrame({
        "sales_rep": [r[0] for r in rows],
        "sales_channel": [r[1] for r in rows],
        "status": [r[2] for r in rows],
        "rate_id": [r[3] for r in rows],
        "market_id": [r[4] for r in rows],
        "rate_mrc": [PRICES[r[3]] for r in rows],
    })


def test_classify_tiers():
    assert bonus.classify(24, 1) == (bonus.GPON, 1, 1.0)       # 250
    assert bonus.classify(25, 1) == (bonus.GPON, 2, 1.25)      # 500
    assert bonus.classify(26, 1) == (bonus.GPON, 3, 1.5)       # 1 Gig
    assert bonus.classify(80, 19) == (bonus.XGSPON, 2, 1.25)   # 2.5 Gig
    assert bonus.classify(81, 19) == (bonus.XGSPON, 3, 1.5)    # 5 Gig


def test_shared_1gig_plan_split_by_market():
    assert bonus.classify(58, 19) == (bonus.XGSPON, 1, 1.0)            # Hopatcong
    assert bonus.classify(58, 11) == (bonus.SINGLE_TIER, None, 1.0)    # Jefferson
    assert bonus.classify(58, None) == (bonus.SINGLE_TIER, None, 1.0)


def test_unknown_plan_pays_nothing():
    assert bonus.classify(67, 4) == (bonus.OTHER, None, 0.0)
    assert bonus.sale_bonus(67, 4, 69.95, "Outbound Call") == 0.0


def test_sale_bonus_is_multiplier_times_plan_price():
    assert bonus.sale_bonus(24, 1, 59.95, "Outbound Call") == 59.95     # 1 x 59.95
    assert bonus.sale_bonus(25, 1, 79.95, "Outbound Call") == 99.94     # 1.25 x 79.95
    assert bonus.sale_bonus(26, 1, 99.95, "Outbound Call") == 149.93    # 1.5 x 99.95
    assert bonus.sale_bonus(81, 19, 99.95, "Texting") == 149.93
    assert bonus.sale_bonus(58, 11, 49.95, "Outbound Call") == 49.95    # Single Tier
    assert bonus.sale_bonus(12, 1, 0.0, "Outbound Call") == 149.93      # prepaid 1 Gig
    assert bonus.sale_bonus(26, 1, 99.95, "Door to Door") == 200.0
    assert bonus.sale_bonus(26, 1, 99.95, "Inbound Call") == 0.0
    assert bonus.sale_bonus(26, 1, None, "Outbound Call") == 0.0


def test_only_installed_outbound_sales_earn():
    df = sales([
        ("Ann", "Outbound Call", "Installed", 26, 1),       # $149.93 earned
        ("Ann", "Outbound Email", "Pending", 25, 1),        # $99.94 pending
        ("Ann", "Outbound Call", "Needs Attention", 26, 1), # nothing
        ("Ann", "Inbound Call", "Installed", 26, 1),        # inbound: nothing
        ("Bob", "Inbound Call", "Installed", 81, 19),       # inbound only: no row
    ])
    rows = bonus.calculate_rep_bonuses(df)
    assert [r["sales_rep"] for r in rows] == ["Ann"]
    ann = rows[0]
    assert ann["sales"] == 3
    assert ann["gpon_3"] == 2 and ann["gpon_2"] == 1
    assert ann["bonus_installed"] == pytest.approx(149.93)
    assert ann["bonus_pending"] == pytest.approx(99.94)
    assert ann["bonus_predicted"] == pytest.approx(249.87)
    assert bonus.summarize(rows)["bonus_predicted"] == pytest.approx(249.87)


def test_door_to_door_is_flat_200_installed_only():
    df = sales([
        ("Ann", "Door to Door", "Installed", 26, 1),        # $200, not 1.5x
        ("Ann", "Door to Door", "Pending", 81, 19),         # $200 predicted
        ("Ann", "Door to Door", "Needs Attention", 24, 1),  # nothing
        ("Ann", "Outbound Call", "Installed", 24, 1),       # $59.95
    ])
    ann = bonus.calculate_rep_bonuses(df)[0]
    assert ann["d2d"] == 3
    assert ann["gpon_1"] == 1 and ann["gpon_3"] == 0 and ann["xgs_3"] == 0
    assert ann["bonus_installed"] == pytest.approx(259.95)
    assert ann["bonus_predicted"] == pytest.approx(459.95)


def test_monthly_bonus_by_install_month():
    df = sales([
        ("Ann", "Outbound Call", "Installed", 26, 1),   # Oct: $149.93
        ("Ann", "Door to Door", "Pending", 24, 1),      # Nov: $200 predicted
        ("Ann", "Inbound Call", "Installed", 26, 1),    # Oct: nothing
        ("Bob", "Outbound Call", "Installed", 26, 1),   # not Ann
    ])
    df["sale_date"] = pd.to_datetime(["2026-10-01"] * 4)
    df["scheduled_date"] = pd.to_datetime(["2026-10-10", "2026-11-05", "2026-10-12", "2026-10-10"])
    result = bonus.calculate_rep_monthly_bonus(df, "Ann")
    assert result["months"][(2026, 10)]["bonus_installed"] == pytest.approx(149.93)
    assert result["months"][(2026, 11)]["bonus_installed"] == 0.0
    assert result["months"][(2026, 11)]["bonus_predicted"] == 200.0
    assert result["totals"]["bonus_predicted"] == pytest.approx(349.93)
    assert bonus.calculate_rep_monthly_bonus(df, "Nobody")["months"] == {}
