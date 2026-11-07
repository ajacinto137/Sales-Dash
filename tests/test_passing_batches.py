"""Tests for passing_batches.py -- weekly release batches and their sales.

Run with: pytest tests/test_passing_batches.py -v
"""

from datetime import date, datetime

import passing_batches as pb

TODAY = date(2026, 11, 5)


def passings(count, released, town="HOPATCONG", start=0):
    return [(start + i, released, town, None) for i in range(count)]


def test_a_batch_is_one_town_in_one_monday_to_sunday_week():
    rows = passings(60, date(2026, 10, 19)) + passings(60, date(2026, 10, 25), start=100)
    rows += passings(60, date(2026, 10, 26), start=200) + passings(60, date(2026, 10, 19), "HARDYSTON", 300)
    names = {b["name"]: b["passings"] for b in pb.build_batches(rows, {}, TODAY)}
    assert names == {
        "Hopatcong Batch Week Oct 19": 120,
        "Hopatcong Batch Week Oct 26": 60,
        "Hardyston Batch Week Oct 19": 60,
    }


def test_sales_weeks_are_monday_to_sunday_calendar_weeks():
    rows = [(1, datetime(2026, 10, 19, 9), "HOPATCONG", None), (2, datetime(2026, 10, 23), "HOPATCONG", None)]
    sales = {1: ["2026-10-25", "2026-10-26"], 2: ["2026-10-25", "2026-10-29", "2026-11-02"]}
    weeks = pb.build_batches(rows, sales, TODAY)[0]["weeks"]
    assert [w["sales"] for w in weeks] == [2, 2, 1]


def test_rate_is_week_sales_over_the_whole_batch():
    sales = {i: ["2026-10-20"] for i in range(5)}
    batch = pb.build_batches(passings(100, date(2026, 10, 19)), sales, TODAY)[0]
    assert batch["weeks"][0]["rate"] == 5.0


def test_a_week_is_final_once_its_sunday_has_passed_and_only_one_runs():
    rows = passings(60, date(2026, 10, 19)) + passings(60, date(2026, 10, 23), start=100)
    batch = pb.build_batches(rows, {}, date(2026, 10, 25))[0]
    assert [w["final"] for w in batch["weeks"]] == [False]
    batch = pb.build_batches(rows, {}, date(2026, 10, 26))[0]
    assert [w["final"] for w in batch["weeks"]] == [True, False]
    assert batch["running"]["number"] == 2


def test_status_compares_the_running_week_with_the_line():
    sales = {i: ["2026-11-03"] for i in range(3)}
    above = pb.build_batches(passings(100, date(2026, 10, 19)), sales, TODAY)[0]
    assert (above["status"], above["running"]["number"], above["running"]["rate"]) == ("above", 3, 3.0)
    sales = {0: ["2026-11-03"]}
    assert pb.build_batches(passings(100, date(2026, 10, 19)), sales, TODAY)[0]["status"] == "under"


def test_two_percent_exactly_is_not_under_the_line():
    sales = {i: ["2026-10-20"] for i in range(2)}
    batch = pb.build_batches(passings(100, date(2026, 10, 19)), sales, date(2026, 10, 21))[0]
    assert batch["status"] == "above"


def test_small_batches_get_no_status():
    small = pb.build_batches(passings(49, date(2026, 10, 19)), {}, TODAY)[0]
    assert small["status"] == "small"




def test_town_falls_back_to_city():
    rows = [(1, date(2026, 10, 19), " ", "NEW PALTZ")]
    assert pb.build_batches(rows, {}, TODAY)[0]["town"] == "New Paltz"


def test_summary_counts():
    rows = passings(100, date(2026, 10, 19)) + passings(10, date(2026, 10, 19), "HOPE", 200)
    summary = pb.summarize(pb.build_batches(rows, {}, TODAY))
    assert (summary["batches"], summary["passings"], summary["under"], summary["above"]) == (2, 110, 1, 0)


def test_passings_use_the_passings_definition():
    assert "p.IsPlanetImproved = 1" in pb._PASSINGS_QUERY


def test_a_batch_keeps_its_place_ids_and_organic_totals():
    sales = {0: ["2026-10-20"], 1: ["2026-10-27"], 2: ["2026-10-28"]}
    batch = pb.build_batches(passings(100, date(2026, 10, 19)), sales, TODAY)[0]
    assert batch["place_ids"] == list(range(100))
    assert batch["total_sales"] == 3
    assert batch["total_rate"] == 3.0


def test_find_batch_matches_town_and_release_monday():
    rows = passings(60, date(2026, 10, 19)) + passings(60, date(2026, 10, 26), start=100)
    batches = pb.build_batches(rows, {}, TODAY)
    assert pb.find_batch(batches, "Hopatcong", "2026-10-26")["place_ids"][0] == 100
    assert pb.find_batch(batches, "Hopatcong", "2026-10-27") is None
    assert pb.find_batch(batches, "Hardyston", "2026-10-26") is None


def test_sales_link_by_property_id():
    rows = [(1, date(2026, 10, 19), "A", None, " PROP-1 "), (2, date(2026, 10, 19), "A", None, "PROP-2")]
    sales = {"PROP-1": ["2026-10-20"], "OTHER": ["2026-10-20"]}
    assert pb.sales_by_passing(rows, sales) == {1: ["2026-10-20"]}


def test_a_shared_property_credits_only_its_first_released_passing():
    rows = [
        (5, date(2026, 10, 22), "A", None, "PROP-1"),
        (9, datetime(2026, 10, 19, 8), "A", None, "PROP-1"),
        (3, date(2026, 10, 19), "A", None, "PROP-1"),
    ]
    assert pb.sales_by_passing(rows, {"PROP-1": ["2026-10-20", "2026-10-21"]}) == {3: ["2026-10-20", "2026-10-21"]}


def test_a_passing_with_no_property_id_gets_no_sales():
    rows = [(1, date(2026, 10, 19), "A", None, None), (2, date(2026, 10, 19), "A", None, "  ")]
    assert pb.sales_by_passing(rows, {"": ["2026-10-20"]}) == {}


def test_old_batches_stay_and_carry_their_year():
    rows = passings(60, date(2021, 6, 8)) + passings(60, date(2026, 10, 19), start=100)
    assert [b["name"] for b in pb.build_batches(rows, {}, TODAY)] == [
        "Hopatcong Batch Week Oct 19",
        "Hopatcong Batch Week Jun 7, 2021",
    ]


def test_sales_before_release_do_not_count():
    batch = pb.build_batches(passings(60, date(2026, 8, 3)), {0: ["2026-08-01"]}, TODAY)[0]
    assert batch["total_sales"] == 0


def test_extra_towns_join_once_and_need_a_date():
    base = [(1, date(2026, 9, 24), "Vernon", None, "A")]
    extra = [(1, date(2026, 9, 24), "Vernon", None, "A"), (2, date(2026, 10, 9), "Matamoras", None, "B"), (3, None, "Westfall", None, "C")]
    assert [row[0] for row in pb.with_extra_towns(base, extra)] == [1, 2]


def test_extra_towns_have_no_batch_profile():
    rows = [(1, date(2026, 10, 5), "Matamoras", None), (2, date(2026, 10, 5), "Vernon", None)]
    profiles = {batch["town"]: batch["has_profile"] for batch in pb.build_batches(rows, {}, TODAY)}
    assert profiles == {"Matamoras": False, "Vernon": True}


def batch_of(rates, passings=1000):
    """A finished batch whose week N sells rates[N-1] percent of its passings."""
    weeks = [
        {"number": i + 1, "sales": rate * passings / 100, "rate": rate, "final": True}
        for i, rate in enumerate(rates)
    ]
    return {"passings": passings, "weeks": weeks}


def test_plateau_is_the_average_of_weeks_53_to_105_and_decay_ends_when_it_settles():
    result = pb.plateau([batch_of([5.0] * 9 + [0.2] * 100)])
    assert round(result["rate"], 6) == 0.2
    assert result["decay_ends"] == 10


def test_plateau_weights_big_batches_more():
    result = pb.plateau([batch_of([0.1] * 105, 3000), batch_of([0.5] * 105, 1000)])
    assert round(result["rate"], 6) == 0.2


def test_plateau_ignores_the_running_week():
    batch = batch_of([0.2] * 105)
    batch["weeks"].append({"number": 106, "sales": 0, "rate": 0.0, "final": False})
    batch["weeks"][-2].update(final=False, sales=900, rate=90.0)
    assert round(pb.plateau([batch])["rate"], 6) == 0.2


def test_no_plateau_without_enough_history():
    assert pb.plateau([batch_of([0.2] * 77)]) is None
    assert pb.plateau([]) is None


def test_all_sale_dates_keeps_every_order():
    rows = [("P1", "jsmith", date(2026, 10, 20)), ("P1", None, "2026-10-21")]
    assert pb.all_sale_dates(rows) == {"P1": ["2026-10-20", "2026-10-21"]}


def test_load_batches_counts_external_sales_only_when_asked(monkeypatch):
    organic, every_sale = {0: ["2026-10-20"]}, {0: ["2026-10-20"], 1: ["2026-10-20"]}
    monkeypatch.setattr(pb, "_fetch", lambda: (passings(100, date(2026, 10, 19)), organic, every_sale))
    monkeypatch.setattr(pb.passings_data, "_today", lambda: TODAY)
    monkeypatch.setattr(pb, "_state", {"data": None, "loaded_at": 0.0, "built": {}, "built_key": None})
    assert pb.load_batches()[0][0]["total_sales"] == 1
    assert pb.load_batches(monthly=True)[0][0]["name"] == "Hopatcong Batch Oct 2026"
    assert pb.load_batches(include_external=True)[0][0]["total_sales"] == 2
    assert pb.load_batches()[0][0]["total_sales"] == 1


def test_monthly_batches_are_a_town_in_a_calendar_month():
    rows = passings(60, date(2026, 10, 1)) + passings(60, date(2026, 10, 31), start=100)
    rows += passings(60, date(2026, 9, 30), start=200)
    names = {b["name"]: b["passings"] for b in pb.build_batches(rows, {}, TODAY, monthly=True)}
    assert names == {"Hopatcong Batch Oct 2026": 120, "Hopatcong Batch Sep 2026": 60}


def test_monthly_sales_land_in_calendar_months_and_a_month_is_final_once_it_ends():
    sales = {0: ["2026-10-31", "2026-11-01"], 1: ["2026-12-01"]}
    rows = passings(100, date(2026, 10, 15))
    batch = pb.build_batches(rows, sales, date(2026, 12, 2), monthly=True)[0]
    assert [w["sales"] for w in batch["weeks"]] == [1, 1, 1]
    assert [w["final"] for w in batch["weeks"]] == [True, True, False]
    assert batch["running"]["number"] == 3


def test_monthly_periods_cross_the_new_year():
    batch = pb.build_batches(passings(60, date(2025, 11, 20)), {0: ["2026-01-05"]}, date(2026, 2, 1), monthly=True)[0]
    assert [w["sales"] for w in batch["weeks"]] == [0, 0, 1, 0]
    assert [w["final"] for w in batch["weeks"]] == [True, True, True, False]


def test_monthly_plateau_uses_months_13_to_25():
    result = pb.plateau([batch_of([5.0] * 4 + [1.0] * 21)], monthly=True)
    assert round(result["rate"], 6) == 1.0
    assert (result["decay_ends"], result["first"], result["last"]) == (5, 13, 25)
