"""Tests for the Rep Profile's Monthly Status tab (installed / pending /
needs attention per scheduled install month) and its per-cell Bulk Account View
drill-down -- pure pandas, no database needed.

Run with: pytest tests/test_rep_monthly_status.py -v
"""

from unittest import mock

import pandas as pd

import sales_metrics as sm

DF = pd.DataFrame({
    "sales_rep": ["A", "A", "A", "A", "A", "A", "B"],
    "status": ["Installed", "Pending", "Needs Attention", "Installed", "Cancelled", "Installed", "Installed"],
    "sale_date": pd.to_datetime(["2026-07-03", "2026-07-20", "2026-07-25", "2026-09-01", "2026-09-02", None, "2026-07-02"]),
    "scheduled_date": ["2026-07-10", "2026-11-05", None, "2026-09-08", None, "2026-08-01", "2026-07-09"],
    "sales_channel": ["Outbound Call", "Inbound Call", "Door to Door", "Outbound Call", "Texting", "Outbound Call", "Outbound Call"],
})


def monthly(df, rep, today="2026-10-26"):
    with mock.patch.object(sm, "_today", return_value=pd.Timestamp(today).date()):
        return sm.calculate_rep_monthly_status(df, rep)


def test_buckets_by_install_month_falling_back_to_sale_month():
    months = {m["label"]: m for m in monthly(DF, "A")["months"]}
    assert list(months) == ["November 2026", "October 2026", "September 2026",
                            "August 2026", "July 2026"]
    # Sold in July, scheduled for November -> Pending in November.
    assert (months["November 2026"]["sales"], months["November 2026"]["pending"]) == (1, 1)
    # July: the July install + the undated Needs Attention (sale month).
    july = months["July 2026"]
    assert (july["sales"], july["installed"], july["pending"], july["needs_attention"]) == (2, 1, 0, 1)
    assert round(july["install_rate"]) == 50
    # August: the install with no sale date still lands by install month.
    assert months["August 2026"]["installed"] == 1
    # September: an install + an undated cancel (sale month), in no status column.
    sept = months["September 2026"]
    assert (sept["sales"], sept["installed"], sept["needs_attention"]) == (2, 1, 0)
    assert months["October 2026"]["sales"] == 0 and months["October 2026"]["install_rate"] is None


def test_totals_match_all_months_and_other_reps_are_excluded():
    totals = monthly(DF, "A")["totals"]
    assert (totals["sales"], totals["installed"], totals["pending"]) == (6, 3, 1)
    assert monthly(DF, "nobody") == {"months": [], "totals": None}


def test_drill_down_matches_cell_counts():
    with mock.patch.object(sm, "build_bulk_account_rows", side_effect=lambda df: list(df["status"])):
        assert sm.get_status_month_account_view(DF, "A", 2026, 7, "needs_attention") == (
            "July 2026 Needs Attention", ["Needs Attention"])
        title, rows = sm.get_status_month_account_view(DF, "A", 2026, 7, "all")
        assert title == "July 2026 Accounts" and rows == ["Installed", "Needs Attention"]
        assert sm.get_status_month_account_view(DF, "A", 2026, 11, "pending") == (
            "November 2026 Pending", ["Pending"])
    assert sm.get_status_month_account_view(DF, "A", 2026, 13, "all") == ("Invalid Month", [])
    assert sm.get_status_month_account_view(DF, "A", 2026, 7, "bogus") == ("Invalid View", [])


def test_rep_sales_by_month_runs_from_first_sale():
    from unittest import mock
    from datetime import date
    import pandas as pd
    import sales_metrics
    df = pd.DataFrame({
        "sales_rep": ["Ann", "Ann", "Ann", "Bob"],
        "sale_date": pd.to_datetime(["2025-11-03", "2026-01-10", "2026-01-12", "2026-01-05"]),
    })
    with mock.patch.object(sales_metrics, "_today", return_value=date(2026, 10, 26)):
        rows = sales_metrics.calculate_rep_sales_by_month(df, "Ann")
        assert rows[0]["month_label"] == "Nov 2025" and rows[-1]["month_label"] == "Oct 2026"
        assert [r["count"] for r in rows][:3] == [1, 0, 2] and sum(r["count"] for r in rows) == 3
        assert len(sales_metrics.calculate_rep_sales_by_month(df, "Nobody")) == 6
