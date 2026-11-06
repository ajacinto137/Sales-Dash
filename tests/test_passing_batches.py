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
    assert batch["organic_sales"] == 3
    assert batch["organic_rate"] == 3.0


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
    assert batch["organic_sales"] == 0


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
    assert result["decay_ends_week"] == 10


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
