"""Tests for the Team Leaderboard's Monthly Sales by Status chart and its
per-segment Bulk Account View drill-down -- pure pandas, no database.

Run with: pytest tests/test_monthly_sales_by_status.py -v
"""

from unittest import mock

import pandas as pd

import sales_metrics as sm

DF = pd.DataFrame({
    "status": ["Installed", "Pending", "Needs Attention", "Cancelled", "Installed", None],
    "sale_date": pd.to_datetime(["2026-07-02", "2026-07-30", "2026-07-15", "2026-09-01", "2026-09-20", None]),
    "scheduled_date": [None] * 6,
})
TODAY = pd.Timestamp("2026-10-27").date()


def test_every_month_from_first_sale_and_segments_add_up():
    with mock.patch.object(sm, "_today", return_value=TODAY):
        rows = sm.calculate_monthly_sales_by_status(DF)
    assert [r["label"] for r in rows] == ["Jul 2026", "Aug 2026", "Sep 2026", "Oct 2026"]
    jul, aug, sep, _ = rows
    assert (jul["sales"], jul["installed"], jul["pending"], jul["needs_attention"], jul["cancels"]) == (3, 1, 1, 1, 0)
    assert aug["sales"] == 0
    assert (sep["sales"], sep["installed"], sep["cancels"]) == (2, 1, 1)
    for r in rows:
        assert r["installed"] + r["pending"] + r["needs_attention"] + r["cancels"] == r["sales"]


def test_segment_drill_down():
    with mock.patch.object(sm, "build_bulk_account_rows", side_effect=lambda df: df.to_dict("records")):
        title, rows = sm.get_month_status_account_view(DF, "2026", "7", "pending")
        assert title == "Pending — July 2026 Sales" and len(rows) == 1
        assert len(sm.get_month_status_account_view(DF, 2026, 7, "all")[1]) == 3
        assert len(sm.get_month_status_account_view(DF, 2026, 9, "cancels")[1]) == 1
    assert sm.get_month_status_account_view(DF, 2026, 13, "all") == ("Invalid View", [])
    assert sm.get_month_status_account_view(DF, 2026, 7, "bogus") == ("Invalid View", [])


def test_empty():
    assert sm.calculate_monthly_sales_by_status(None) == []
    assert sm.calculate_monthly_sales_by_status(DF.iloc[0:0]) == []
