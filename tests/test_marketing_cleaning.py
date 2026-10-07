"""Tests for marketing_cleaning.py -- the glue between PlanetWeb's
View_FormDataAnalytics rows, the vendored planet_cleaning pipeline
(attribution rules themselves are covered by
tests/test_planet_cleaning_pipeline.py, the handoff's own suite), and the
Marketing Channel Report's compact payload. No database needed.

Run with: pytest tests/test_marketing_cleaning.py -v
"""

from datetime import datetime

import pandas as pd

import marketing_cleaning as mc
from planet_cleaning import pipeline

NOW = pd.Timestamp("2026-09-11T16:00:00Z")


def base_row(**overrides):
    """Shaped like one fetch_raw_rows() row -- pyodbc hands InsertDate
    back as a naive (Eastern) datetime and AvailabilityID as an int."""
    row = {c: None for c in mc._FETCH_COLUMNS}
    row.update({
        "InsertDate": datetime(2026, 9, 7, 12, 0, 0),
        "AvailabilityID": 1,
        "Zipcode": 7461,
        "NJPR_municipalityName": "Wantage",
        "email_id": "a" * 64,
    })
    row.update(overrides)
    return row


def classify(*rows):
    output_rows, rejected = mc.classify_rows(list(rows), now=NOW)
    return output_rows, rejected


def test_mp_token_is_paid_even_without_utms():
    out, _ = classify(base_row(MarketingToken=" mp-oct-26 "))
    assert out[0]["channel_group"] == "Paid"
    assert out[0]["channel_detail"] == "Paid (other)"
    assert out[0]["match_tier"] == "confirmed_mp_prefix"


def test_mp_token_keeps_platform_from_independent_evidence():
    out, _ = classify(base_row(MarketingToken="MP-SEP-26", gclid="abc"))
    assert (out[0]["channel_group"], out[0]["channel_detail"]) == ("Paid", "Google")


def test_fbclid_alone_is_not_paid():
    out, _ = classify(base_row(fbclid="xyz"))
    assert out[0]["channel_group"] == "Organic-Direct"
    assert "fbclid_does_not_prove_paid" in out[0]["review_flags"]


def test_other_tokens_are_unconfirmed_not_guessed():
    out, _ = classify(base_row(MarketingToken="MRitchie"), base_row(MarketingToken="PC1", email_id="b" * 64))
    assert [(r["channel_group"], r["channel_detail"]) for r in out] == [("Offline-Referral", "Unconfirmed token")] * 2


def test_numeric_zip_and_naive_eastern_date_are_normalized():
    out, _ = classify(base_row(InsertDate=datetime(2026, 9, 7, 23, 30)))
    assert out[0]["Zipcode"] == "07461"
    assert out[0]["State"] == "NJ"
    # 23:30 Eastern stays on the Eastern calendar day, not the UTC one.
    assert out[0]["local_date"].isoformat() == "2026-09-07"


def test_invalid_rows_are_rejected_and_reported():
    out, rejected = classify(
        base_row(),
        base_row(AvailabilityID=9, email_id="b" * 64),
        base_row(email_id="not-a-hash"),
    )
    assert len(out) == 1
    assert len(rejected) == 2
    report = mc.build_guardrail_report([], rejected, out)
    assert any("2 row(s) failed validation" in w for w in report["warnings"])


def test_payload_has_no_hardcoded_spend_and_keeps_every_row():
    out, _ = classify(
        base_row(email_id="a" * 64, InsertDate=datetime(2026, 9, 1, 9)),
        base_row(email_id="a" * 64, InsertDate=datetime(2026, 9, 2, 9)),
        base_row(email_id="b" * 64, InsertDate=datetime(2026, 9, 2, 10), MarketingToken="MP-SEP-26"),
    )
    payload = mc.build_dashboard_payload(out)
    assert payload["spend_total"] is None
    assert payload["raw_rows"] == 3
    assert payload["d"] == [0, 1, 1]
    assert payload["e"] == [0, 0, 1]
    assert payload["a"] == [0, 0, 1]
    assert len(payload["r"]) == 3


def _browser_dedupe(payload, from_day, to_day):
    """Python port of the report's computeDedupedIndices(): first row per
    submitter, by day then array order, within [from_day, to_day]."""
    best = {}
    for i, day in enumerate(payload["d"]):
        if from_day <= day <= to_day:
            e = payload["e"][i]
            if e not in best or day < payload["d"][best[e]]:
                best[e] = i
    return sorted(best.values())


def test_browser_dedupe_matches_pipeline_select_period():
    """The report's per-range counts must equal the handoff's own
    select_period() -- filter dates first, earliest submission per email
    within the range, THEN count channel and availability."""
    rows = [
        # Person a: organic+unavailable on day 0, paid+available on day 2.
        base_row(email_id="a" * 64, InsertDate=datetime(2026, 9, 1, 9), AvailabilityID=0),
        base_row(email_id="a" * 64, InsertDate=datetime(2026, 9, 3, 9), MarketingToken="MP-SEP-26"),
        # Person b: two same-day rows -- the earlier time must win.
        base_row(email_id="b" * 64, InsertDate=datetime(2026, 9, 2, 8), gclid="g1"),
        base_row(email_id="b" * 64, InsertDate=datetime(2026, 9, 2, 7), AvailabilityID=0),
        base_row(email_id="c" * 64, InsertDate=datetime(2026, 9, 3, 12), utm_medium="email"),
    ]
    out, _ = classify(*rows)
    payload = mc.build_dashboard_payload(out)

    df = pd.DataFrame(rows, columns=mc._FETCH_COLUMNS, dtype=object)
    df["input_row"] = range(1, len(df) + 1)
    p_rows, _ = pipeline.normalize_rows(df, mc.REPORT_TZ, NOW, {})
    p_rows = pipeline.classify(p_rows, {})

    for from_day, to_day in [(0, 2), (1, 2), (2, 2), (0, 0)]:
        start = pd.Timestamp("2026-09-01").tz_localize(mc.REPORT_TZ) + pd.DateOffset(days=from_day)
        end = pd.Timestamp("2026-09-01").tz_localize(mc.REPORT_TZ) + pd.DateOffset(days=to_day + 1)
        _, selected = pipeline.select_period(p_rows, start, end)
        idx = _browser_dedupe(payload, from_day, to_day)
        browser = sorted((payload["chans"][payload["c"][i]]["g"], payload["s"][i]) for i in idx)
        expected = sorted((r["channel_group"], r["AvailabilityID"]) for r in selected)
        assert browser == expected, (from_day, to_day)


# ---------------- Hourly cache ----------------

def _reset_cache():
    mc._cache = {"output_rows": None, "guardrail_report": None, "computed_at": None}


def test_run_cleaning_cached_reuses_result_within_ttl():
    _reset_cache()
    calls = {"n": 0}
    real_run_cleaning = mc.run_cleaning
    real_monotonic = mc.time.monotonic
    clock = [1000.0]
    try:
        mc.run_cleaning = lambda: (calls.__setitem__("n", calls["n"] + 1), ([{"row": calls["n"]}], {"warnings": [], "info": []}))[1]
        mc.time.monotonic = lambda: clock[0]

        rows1, _ = mc.run_cleaning_cached()
        assert calls["n"] == 1
        assert rows1 == [{"row": 1}]

        clock[0] += 1800  # 30 minutes later, still within the 1-hour TTL
        rows2, _ = mc.run_cleaning_cached()
        assert calls["n"] == 1, "should have served the cached result, not re-run the pipeline"
        assert rows2 == [{"row": 1}]
    finally:
        mc.run_cleaning = real_run_cleaning
        mc.time.monotonic = real_monotonic
        _reset_cache()


def test_run_cleaning_cached_refreshes_after_ttl_expires():
    _reset_cache()
    calls = {"n": 0}
    real_run_cleaning = mc.run_cleaning
    real_monotonic = mc.time.monotonic
    clock = [1000.0]
    try:
        mc.run_cleaning = lambda: (calls.__setitem__("n", calls["n"] + 1), ([{"row": calls["n"]}], {"warnings": [], "info": []}))[1]
        mc.time.monotonic = lambda: clock[0]

        mc.run_cleaning_cached()
        clock[0] += mc.CACHE_TTL_SECONDS + 1
        rows, _ = mc.run_cleaning_cached()
        assert calls["n"] == 2, "cache should have expired and triggered a real re-run"
        assert rows == [{"row": 2}]
    finally:
        mc.run_cleaning = real_run_cleaning
        mc.time.monotonic = real_monotonic
        _reset_cache()


def test_run_cleaning_cached_does_not_cache_a_failed_pull():
    _reset_cache()
    calls = {"n": 0}
    real_run_cleaning = mc.run_cleaning
    try:
        mc.run_cleaning = lambda: (calls.__setitem__("n", calls["n"] + 1), ([], {"warnings": ["down"], "info": []}))[1]

        rows1, guardrail1 = mc.run_cleaning_cached()
        assert rows1 == []
        assert calls["n"] == 1

        rows2, _ = mc.run_cleaning_cached()
        assert calls["n"] == 2, "a failed pull must not be cached -- the next call should retry immediately"
    finally:
        mc.run_cleaning = real_run_cleaning
        _reset_cache()
