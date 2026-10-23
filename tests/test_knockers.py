"""Tests for the Door Knockers dashboard data (knockers.py) -- pure
pandas, no database needed.

Run with: pytest tests/test_knockers.py -v
"""

from datetime import date
from unittest import mock

import pandas as pd

import knockers

TODAY = date(2026, 10, 22)


def raw(rows):
    cols = ["order_id", "referral_code", "sale_date", "installation_date", "install_date",
            "order_cancellation_date", "service_cancellation_date", "state", "municipality"]
    df = pd.DataFrame(rows, columns=cols)
    for c in ["first_name", "last_name", "address", "apartment_suite", "city", "zipcode",
              "subscriber_uuid", "scheduled_status", "package_speed", "voice"]:
        df[c] = None
    df["payment_method"] = ["card", "Card ", "check", "ach", "skip", "card", None][:len(df)]
    return df


RAW = raw([
    (1, "AAA", "2026-10-01", "2026-10-05", "2026-10-05", None, None, "NJ", "Vernon"),          # installed
    (2, "aaa", "2026-10-02", "2026-10-30", None, None, None, "nj ", "Vernon"),                 # pending
    (3, "aaa", "2026-10-03", "2026-10-10", None, None, None, "NJ", "Sparta"),                  # past appt -> NA
    (4, "bbb", "2026-10-03", None, None, None, None, "NY", ""),                                # no appt -> NA
    (5, "bbb", "2026-10-04", "2026-10-20", None, "2026-10-08", None, "NY", "Warwick"),         # cancelled
    (6, "bbb", "2026-09-04", "2026-09-10", "2026-09-10", "2026-09-08", "2026-10-01", "VA", "X"),  # installed wins; service cancel
    (7, "ccc", "2026-10-22", "2026-10-22", None, None, None, "NJ", "Vernon"),                 # appt today -> pending
])


def norm():
    with mock.patch.object(knockers, "_today", return_value=TODAY):
        return knockers.normalize(RAW)


def test_status_definitions():
    df = norm().set_index("sale_id")
    assert df.loc[[1, 2, 3, 4, 5, 6, 7], "status"].tolist() == [
        "Installed", "Pending", "Needs Attention", "Needs Attention", "Cancelled", "Installed", "Pending"]
    assert df.loc[6, "service_cancelled"] and not df.loc[1, "service_cancelled"]


def test_codes_and_states_are_normalized():
    df = norm()
    assert set(df["sales_rep"]) == {"aaa", "bbb", "ccc"}
    assert set(df["state"]) == {"NJ", "NY", "VA"}


def test_knocker_rows():
    rows = {r["code"]: r for r in knockers.knocker_rows(norm())}
    a = rows["aaa"]
    assert (a["sales"], a["installed"], a["pending"], a["needs_attention"], a["cancelled"]) == (3, 1, 1, 1, 0)
    assert round(a["install_rate"], 1) == 33.3
    b = rows["bbb"]
    assert (b["sales"], b["installed"], b["cancelled"], b["service_cancelled"]) == (3, 1, 1, 1)
    assert [r["code"] for r in knockers.knocker_rows(norm())] == ["aaa", "bbb", "ccc"]


def test_sort_puts_missing_rates_last():
    rows = [{"code": "x", "install_rate": None}, {"code": "y", "install_rate": 50.0}, {"code": "z", "install_rate": 10.0}]
    assert [r["code"] for r in knockers.sort_knocker_rows(rows, "install_rate", "asc")] == ["z", "y", "x"]


def test_state_and_town_rows():
    states, total = knockers.state_rows(norm())
    assert [s["state"] for s in states] == ["NJ", "NY", "VA"]
    assert total["sales"] == 7
    towns = knockers.town_rows(norm())
    assert towns[0]["municipality"] == "Vernon" and towns[0]["sales"] == 3
    assert all(t["municipality"] for t in towns)  # blank towns excluded


def test_account_view_filters_match_counts():
    df = norm()
    with mock.patch.object(knockers, "build_bulk_account_rows", side_effect=lambda f: list(f["sale_id"])), \
         mock.patch("sales_metrics._today", return_value=TODAY):
        assert knockers.account_view(df, code="aaa", status="needs_attention", period="all_time") == ("Needs Attention", [3])
        assert knockers.account_view(df, status="service_cancelled", period="all_time") == ("Service Cancellations", [6])
        title, ids = knockers.account_view(df, municipality="Vernon", status="all", period="this_month")
        assert sorted(ids) == [1, 2, 7]
        title, ids = knockers.account_view(df, status="all", day=date(2026, 10, 3))
        assert sorted(ids) == [3, 4] and title.startswith("October 3, 2026")


def test_card_on_file():
    df = norm().set_index("sale_id")
    assert df["card_on_file"].tolist() == [True, True, False, True, False, True, False]  # card + ach
    rows = {r["code"]: r for r in knockers.knocker_rows(norm())}
    assert rows["aaa"]["card_on_file"] == 2 and round(rows["aaa"]["card_on_file_rate"], 1) == 66.7
    assert rows["ccc"]["card_on_file_rate"] == 0
    assert knockers.status_summary(norm())["card_on_file"] == 4
    with mock.patch.object(knockers, "build_bulk_account_rows", side_effect=lambda f: list(f["sale_id"])):
        assert knockers.account_view(norm(), code="bbb", status="no_card_on_file", period="all_time")[1] == [5]


def test_week_to_date_comparison():
    import sales_metrics as sm
    # Thursday 2026-10-22: this week = Mon 10/19..Thu 10/22.
    dates = ["2026-10-19", "2026-10-19", "2026-10-21", "2026-10-22",   # this week: 2,0,1,1
             "2026-10-12", "2026-10-18",                                 # last week: Mon 1, Sun 1
             "2026-09-07"]                                               # outside 2-week window
    df = pd.DataFrame({"sale_date": pd.to_datetime(dates)})
    with mock.patch.object(sm, "_today", return_value=TODAY):
        w = sm.calculate_week_to_date_comparison(df, weeks=2)
    assert w["days"][0] == "Mon 10/19" and len(w["days"]) == 7
    assert w["current"] == [2, 2, 3, 4, None, None, None]
    assert w["current_daily"][:4] == [2, 0, 1, 1]
    # weeks of 10/5 (0 sales) and 10/12 (Mon 1 ... Sun 2) averaged
    assert w["average"] == [0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 1.0]
    assert w["total"] == 4 and w["week_label"] == "Week of Oct 19"


def test_blitz_attribution_and_summary():
    # NY blitz 2026-10-02..10-12; a NY sale outside the window and an NJ sale inside it don't count.
    df = knockers.normalize(raw([
        (1, "a", "2026-10-02", "2026-10-05", "2026-10-05", None, None, "NY", "Saugerties"),
        (2, "b", "2026-10-02", None, None, None, None, "NY", "Saugerties"),
        (3, "a", "2026-10-12", "2026-10-30", None, None, None, "NY", "New Paltz"),
        (4, "a", "2026-10-13", None, None, None, None, "NY", "Saugerties"),
        (5, "a", "2026-10-05", None, None, None, None, "NJ", "Vernon"),
    ]))
    blitz = knockers.BLITZ_BY_SLUG["ny-saugerties-fall-2026"]
    with mock.patch.object(knockers, "_today", return_value=TODAY):
        assert sorted(knockers.blitz_sales(df, blitz)["sale_id"]) == [1, 2, 3]
        s = knockers.blitz_summary(df, blitz)
    assert s["stats"]["sales"] == 3 and s["knockers"] == 2 and s["selling_days"] == 2
    assert len(s["daily"]) == 11 and s["daily"][0]["count"] == 2 and s["daily"][-1]["count"] == 1
    assert s["per_day"] == 1.5 and not s["ongoing"]


def test_ongoing_blitz_runs_through_today():
    blitz = knockers.BLITZ_BY_SLUG["va-farmville-october-2026"]
    assert knockers.blitz_window(blitz, today=TODAY) == (date(2026, 10, 15), TODAY)


def test_blitz_slugs_unique_and_windows_ordered():
    assert len(knockers.BLITZ_BY_SLUG) == len(knockers.BLITZES)
    for b in knockers.BLITZES:
        assert b["end"] is None or b["start"] <= b["end"]


def test_group_summary_is_union_of_blitzes():
    df = knockers.normalize(raw([
        (1, "a", "2026-10-02", None, "2026-10-05", None, None, "NY", "Saugerties"),
        (2, "b", "2026-10-03", None, None, None, None, "NY", "Saugerties"),
        (3, "c", "2026-10-16", None, None, None, None, "VA", "District 601"),
        (4, "c", "2026-10-16", None, None, None, None, "NJ", "Vernon"),
    ]))
    blitzes = [knockers.BLITZ_BY_SLUG["ny-saugerties-fall-2026"], knockers.BLITZ_BY_SLUG["va-farmville-october-2026"]]
    with mock.patch.object(knockers, "_today", return_value=TODAY):
        assert sorted(knockers.group_sales(df, blitzes)["sale_id"]) == [1, 2, 3]
        g = knockers.group_summary(df, blitzes)
        states = {s["state"]: s for s in knockers.state_group_summaries(df)}
    assert g["stats"]["sales"] == 3 and g["stats"]["installed"] == 1 and g["knockers"] == 3
    assert g["selling_days"] == 3 and len(g["members"]) == 2 and g["state"] == "NY, VA"
    assert g["ongoing"] and g["name"] == "2 Blitzes Combined"
    assert list(states) == ["NJ", "NY", "VA"] and states["NY"]["name"] == "All NY Blitzes"
    assert states["NY"]["stats"]["sales"] == 2 and states["NJ"]["stats"]["sales"] == 0


def test_monthly_rates():
    knockers_min = knockers.MIN_RATE_BASE
    knockers.MIN_RATE_BASE = 1  # tiny fixture; the threshold has its own test
    df = knockers.normalize(raw([
        (1, "a", "2026-03-05", None, None, "2026-03-20", None, "NJ", "X"),            # Mar: cancelled (pre-reliable)
        (2, "a", "2026-04-02", "2026-04-09", "2026-04-09", None, "2026-06-01", "NJ", "X"),  # Apr: installed + service cancel
        (3, "a", "2026-04-03", "2026-04-09", "2026-04-09", None, None, "NJ", "X"),     # Apr: installed
        (4, "a", "2026-04-04", None, None, "2026-04-10", None, "NJ", "X"),             # Apr: cancelled
        (5, "a", "2026-04-05", None, None, None, None, "NJ", "X"),                     # Apr: needs attention
        (6, "a", "2026-10-05", "2026-10-30", None, None, None, "NJ", "X"),             # Oct: pending
    ]))
    with mock.patch.object(knockers, "_today", return_value=TODAY):
        rows = {r["short"]: r for r in knockers.monthly_rates(df, today=TODAY)}
    assert list(rows)[0] == "Mar" and list(rows)[-1] == "Oct"
    assert rows["Mar"]["order_cancel_rate"] is None and rows["Mar"]["cancelled"] == 1
    apr = rows["Apr"]
    assert (apr["order_cancel_rate"], apr["needs_attention_rate"], apr["service_cancel_rate"]) == (25.0, 25.0, 50.0)
    assert rows["May"]["sales"] == 0 and rows["May"]["needs_attention_rate"] is None
    assert not apr["maturing"] and rows["Sep"]["maturing"] and rows["Oct"]["maturing"]
    knockers.MIN_RATE_BASE = knockers_min


def test_monthly_rates_skip_tiny_months():
    df = knockers.normalize(raw([(1, "a", "2026-08-03", None, None, "2026-08-05", None, "VA", "X")]))
    with mock.patch.object(knockers, "_today", return_value=TODAY):
        aug = {r["short"]: r for r in knockers.monthly_rates(df, today=TODAY)}["Aug"]
    assert aug["sales"] == 1 and aug["cancelled"] == 1 and aug["order_cancel_rate"] is None
