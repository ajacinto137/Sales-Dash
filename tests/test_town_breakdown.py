"""Tests for the Team Leaderboard's Sales by Town section and its
per-count Bulk Account View drill-down -- pure pandas, no database.

Run with: pytest tests/test_town_breakdown.py -v
"""

from unittest import mock

import pandas as pd

import sales_metrics as sm

DF = pd.DataFrame({
    "sales_rep": ["A", "A", "B", "B", "A", "B"],
    "city": ["Newton", " Newton ", "Sparta", "Newton", "", None],
    "state": ["NJ", "nj", "NJ", "PA", "NJ", "NY"],
    "status": ["Installed", "Needs Attention", "Pending", "Installed", "Installed", "Installed"],
    "sale_date": pd.to_datetime(["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06"]),
    "scheduled_date": ["2026-10-09", None, "2026-11-01", "2026-10-10", "2026-10-11", "2026-10-12"],
    "sales_channel": ["Outbound Call", "Inbound Call", "Door to Door", "Outbound Call", "Outbound Call", "Inbound Call"],
})


def test_one_row_per_town_and_state_most_sales_first():
    tb = sm.calculate_town_breakdown(DF)
    rows = {(r["town"], r["state"]): r for r in tb["rows"]}
    assert set(rows) == {("Newton", "NJ"), ("Sparta", "NJ"), ("Newton", "PA"), ("Unknown", "NJ"), ("Unknown", "NY")}
    assert tb["rows"][0]["town"] == "Newton" and tb["rows"][0]["state"] == "NJ"
    newton = rows[("Newton", "NJ")]
    assert (newton["sales"], newton["installs"], newton["needs_attention"], newton["outbound"], newton["inbound"]) == (2, 1, 1, 1, 1)
    assert newton["install_rate"] == 50.0 and round(newton["share"], 1) == 33.3
    assert rows[("Sparta", "NJ")]["pending"] == 1
    total = tb["total"]
    assert (total["sales"], total["installs"], total["needs_attention"]) == (6, 4, 1)
    assert tb["states"] == ["NJ", "NY", "PA"]


def test_empty():
    assert sm.calculate_town_breakdown(None)["rows"] == []
    assert sm.calculate_town_breakdown(DF.iloc[0:0])["total"]["sales"] == 0


def test_drill_down_matches_the_count():
    # build_bulk_account_rows needs the full normalized columns; the rows
    # it's handed are what matter here.
    with mock.patch.object(sm, "_today", return_value=pd.Timestamp("2026-10-27").date()), \
            mock.patch.object(sm, "build_bulk_account_rows", side_effect=lambda df: df.to_dict("records")):
        title, accounts = sm.get_town_account_view(DF, "Newton", "NJ", "sales", "this_month", None, None)
        assert title == "Sales Accounts — Newton, NJ" and len(accounts) == 2
        assert len(sm.get_town_account_view(DF, "Newton", "NJ", "installs", "this_month", None, None)[1]) == 1
        assert len(sm.get_town_account_view(DF, "Unknown", "NY", "sales", "this_month", None, None)[1]) == 1
        assert sm.get_town_account_view(DF, "Newton", "NJ", "bogus", "this_month", None, None)[1] == []
        assert sm.get_town_account_view(DF, "Newton", "NJ", "sales", "last_month", None, None)[1] == []
