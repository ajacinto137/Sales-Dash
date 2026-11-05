"""Tests for the Team Leaderboard's Sales by Town section and its
per-count Bulk Account View drill-down -- pure pandas, no database.

Run with: pytest tests/test_town_breakdown.py -v
"""

from datetime import date, timedelta
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


def test_town_sources_combined_counts_shared_orders_once():
    internal = DF.assign(sale_id=range(1, 7))
    knockers = DF.iloc[:2].assign(sale_id=[2, 99], sales_channel="Door to Door")
    assert sm.town_source_df("internal", internal, None) is internal
    assert sm.town_source_df("knockers", internal, None) is None
    assert sm.town_source_df("knockers", internal, knockers) is knockers
    combined = sm.town_source_df("combined", internal, knockers)
    assert sorted(combined["sale_id"]) == [1, 2, 3, 4, 5, 6, 99]
    # The internal copy of a shared order wins.
    assert combined[combined["sale_id"] == 2]["sales_channel"].iloc[0] == "Inbound Call"


# ---------------- Town Profile ----------------

TODAY = date(2026, 11, 5)  # a Thursday


def _town_df(rows):
    """rows: (city, state, days_ago, status, channel, rep)."""
    return pd.DataFrame({
        "city": [r[0] for r in rows],
        "state": [r[1] for r in rows],
        "sale_date": pd.to_datetime([TODAY - timedelta(days=r[2]) for r in rows]),
        "status": [r[3] for r in rows],
        "sales_channel": [r[4] for r in rows],
        "sales_rep": [r[5] for r in rows],
        "scheduled_date": [None] * len(rows),
    })


PROFILE_DF = _town_df([
    ("Newton", "NJ", 0, "Installed", "Outbound Call", "A"),
    ("Newton", "NJ", 3, "Pending", "Inbound Call", "A"),   # Mon Nov 2, this week
    ("Newton", "NJ", 4, "Installed", "Outbound Call", "B"),  # Sun Nov 1, last week
    ("Newton", "NJ", 89, "Needs Attention", "Outbound Call", "B"),  # first day of the window
    ("Newton", "NJ", 90, "Installed", "Outbound Call", "B"),  # just outside it
    ("Newton", "NJ", 179, "Installed", "Outbound Call", "C"),  # last day of the prior window
    ("Newton", "NJ", 180, "Installed", "Outbound Call", "C"),  # older than both
    ("Sparta", "NJ", 1, "Installed", "Outbound Call", "A"),
    ("Newton", "PA", 1, "Installed", "Outbound Call", "A"),
])


def _profile(df=PROFILE_DF, town="Newton", state="NJ"):
    with mock.patch.object(sm, "_today", return_value=TODAY):
        return sm.calculate_town_profile(df, town, state)


def test_profile_averages_use_the_last_90_days_including_today():
    recent = _profile()["recent"]
    assert recent["start"] == "2026-08-08" and recent["end"] == "2026-11-05"
    assert recent["sales"] == 4
    assert recent["avg_per_day"] == round(4 / 90, 2)
    assert recent["avg_per_week"] == round(4 / 90 * 7, 1)


def test_profile_compares_to_the_prior_90_days():
    recent = _profile()["recent"]
    assert recent["prior_sales"] == 2 and recent["change_pct"] == 100.0
    no_prior = _town_df([("Newton", "NJ", 1, "Installed", "Outbound Call", "A")])
    assert _profile(no_prior)["recent"]["change_pct"] is None


def test_weeks_are_monday_to_sunday_clipped_to_the_window_and_add_up():
    profile = _profile()
    weeks = profile["weeks"]
    assert sum(w["sales"] for w in weeks) == profile["recent"]["sales"]
    assert sum(w["days"] for w in weeks) == 90
    # Aug 8 2026 is a Saturday, so the first week is that weekend only.
    assert (weeks[0]["start"], weeks[0]["end"], weeks[0]["days"], weeks[0]["partial"]) == ("2026-08-08", "2026-08-09", 2, True)
    assert weeks[0]["sales"] == 1 and weeks[0]["needs_attention"] == 1 and weeks[0]["avg_per_day"] == 0.5
    assert (weeks[-1]["start"], weeks[-1]["end"], weeks[-1]["days"]) == ("2026-11-02", "2026-11-05", 4)
    assert (weeks[-1]["sales"], weeks[-1]["installs"], weeks[-1]["pending"]) == (2, 1, 1)
    assert (weeks[-2]["start"], weeks[-2]["end"], weeks[-2]["sales"], weeks[-2]["partial"]) == ("2026-10-26", "2026-11-01", 1, False)
    assert all(w["sales"] == 0 for w in weeks[1:-2])  # empty weeks still get a row


def test_week_rows_carry_every_key_the_status_chart_stacks():
    # charts.js MONTHLY_STATUSES reads these; a missing one drops its segment.
    week = _profile()["weeks"][-1]
    assert (week["installed"], week["pending"], week["needs_attention"], week["cancels"]) == (1, 1, 0, 0)
    assert (week["name"], week["label"]) == ("Nov 2", "Nov 2 – 5")
    assert _profile()["weeks"][-2]["label"] == "Oct 26 – Nov 1"


def test_best_week_prefers_the_latest_on_a_tie():
    assert _profile()["recent"]["best_week"]["start"] == "2026-11-02"
    tied = _town_df([("Newton", "NJ", 0, "Installed", "Outbound Call", "A"),
                     ("Newton", "NJ", 20, "Installed", "Outbound Call", "A")])
    assert _profile(tied)["recent"]["best_week"]["start"] == "2026-11-02"
    old_only = _town_df([("Newton", "NJ", 200, "Installed", "Outbound Call", "A")])
    assert _profile(old_only)["recent"]["best_week"] is None


def test_profile_all_time_matches_the_sales_by_town_row():
    profile = _profile()
    row = next(r for r in sm.calculate_town_breakdown(PROFILE_DF)["rows"] if (r["town"], r["state"]) == ("Newton", "NJ"))
    assert profile["all_time"] == row
    assert (profile["rank"], profile["town_count"], profile["place"]) == (1, 3, "Newton, NJ")
    assert profile["days_since_last_sale"] == 0


def test_profile_reps_and_channels_rank_by_recent_sales():
    profile = _profile()
    assert [(r["rep"], r["recent_sales"], r["sales"]) for r in profile["reps"]] == [("A", 2, 2), ("B", 2, 3)][::-1] + [("C", 0, 2)]
    assert [(c["channel"], c["recent_sales"], c["sales"]) for c in profile["channels"]] == [("Outbound Call", 3, 6), ("Inbound Call", 1, 1)]


def test_profile_of_unknown_town_or_empty_data_is_none():
    assert _profile(town="Nowhere") is None
    assert _profile(state="NY") is None
    assert _profile(df=None) is None
    assert _profile(df=PROFILE_DF.iloc[0:0]) is None


def test_blank_city_profile_uses_the_unknown_town():
    df = _town_df([("", "NJ", 1, "Installed", "Outbound Call", "A")])
    assert _profile(df, town=sm.UNKNOWN_TOWN)["recent"]["sales"] == 1
