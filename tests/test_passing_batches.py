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


def test_window_keeps_whole_batches_and_sales_before_release_do_not_count():
    # 90 days before 2026-11-05 is Thursday 2026-08-07's week, starting 2026-08-03.
    rows = passings(60, date(2026, 8, 3)) + passings(60, date(2026, 8, 2), start=100)
    batches = pb.build_batches(rows, {0: ["2026-08-01"]}, TODAY)
    assert [b["name"] for b in batches] == ["Hopatcong Batch Week Aug 3"]
    assert sum(w["sales"] for w in batches[0]["weeks"]) == 0


def test_town_falls_back_to_city():
    rows = [(1, date(2026, 10, 19), " ", "NEW PALTZ")]
    assert pb.build_batches(rows, {}, TODAY)[0]["town"] == "New Paltz"


def test_summary_counts():
    rows = passings(100, date(2026, 10, 19)) + passings(10, date(2026, 10, 19), "HOPE", 200)
    summary = pb.summarize(pb.build_batches(rows, {}, TODAY))
    assert (summary["batches"], summary["passings"], summary["under"], summary["above"]) == (2, 110, 1, 0)
    assert summary["week_columns"] == 3


def test_passings_use_the_passings_definition():
    assert "p.IsPlanetImproved = 1" in pb._PASSINGS_QUERY
