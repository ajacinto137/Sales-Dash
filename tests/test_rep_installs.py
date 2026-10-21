"""Tests for the Rep Profile's Installs tab (installs per install month)
and its per-month Bulk Account View drill-down -- pure pandas, no
database needed.

Run with: pytest tests/test_rep_installs.py -v
"""

from unittest import mock

import pandas as pd

import sales_metrics as sm

DF = pd.DataFrame({
    "sales_rep": ["A", "A", "A", "A", "A", "B"],
    "status": ["Installed", "Installed", "Installed", "Pending", "Installed", "Installed"],
    "scheduled_date": ["2026-07-03", "2026-07-20", "2026-09-01", "2026-10-05", None, "2026-07-02"],
})


def monthly(df, rep, today="2026-10-20"):
    with mock.patch.object(sm, "_today", return_value=pd.Timestamp(today).date()):
        return sm.calculate_rep_monthly_installs(df, rep)


def test_counts_installs_by_install_month_newest_first_with_zero_months():
    result = monthly(DF, "A")
    assert [(m["label"], m["count"]) for m in result["months"]] == [
        ("October 2026", 0), ("September 2026", 1), ("August 2026", 0), ("July 2026", 2),
    ]
    assert result["total"] == 4  # includes the undated install
    assert result["undated"] == 1
    assert result["max_count"] == 2


def test_other_reps_and_non_installed_rows_are_excluded():
    assert sum(m["count"] for m in monthly(DF, "B")["months"]) == 1
    assert monthly(DF, "nobody") == {"months": [], "total": 0, "undated": 0, "max_count": 0}


def test_far_future_install_date_is_capped():
    df = pd.DataFrame({"sales_rep": ["A", "A"], "status": ["Installed"] * 2,
                       "scheduled_date": ["2026-10-01", "2099-01-01"]})
    assert len(monthly(df, "A")["months"]) == 13


def test_drill_down_matches_month_count():
    with mock.patch.object(sm, "build_bulk_account_rows", side_effect=lambda f: list(f["scheduled_date"])):
        title, rows = sm.get_install_month_account_view(DF, "A", 2026, 7)
        assert title == "July 2026 Installs"
        assert rows == ["2026-07-03", "2026-07-20"]
        assert sm.get_install_month_account_view(DF, "A", 2026, 13) == ("Invalid Month", [])
