"""Tests for the "Web" pseudo-rep (website / GetPlanetFiber.com map-pin
orders without a door-knocker referral code) -- pure pandas, no database
needed.

Run with: pytest tests/test_web_sales.py -v
"""

import pandas as pd

import sales_metrics as sm
from queries import WEB_SALES_SOURCE_TOKEN

DOOR_KNOCKER = next(iter(sm.DOOR_KNOCKER_CODES))


def raw(rows):
    """Minimal PlanetWeb-shaped frame: (id, source_token, referral, channel_id)."""
    return pd.DataFrame({
        "ID": [r[0] for r in rows],
        "SourceToken": [r[1] for r in rows],
        "ReferralData": [r[2] for r in rows],
        "SalesChannel_ID": [r[3] for r in rows],
        "Date": ["2026-10-01"] * len(rows),
        "DateTime": ["2026-10-01 10:00"] * len(rows),
        "Install Status": ["Not Yet Installed"] * len(rows),
    })


MAIN = raw([(1, "PAS Employee: Ana Lee", None, 2), (2, "PAS Employee: Ana Lee", None, 1)])
WEB = raw([
    (10, WEB_SALES_SOURCE_TOKEN, None, None),
    (11, WEB_SALES_SOURCE_TOKEN, "welcometoplanet", None),
    (12, WEB_SALES_SOURCE_TOKEN, DOOR_KNOCKER.upper() + " ", None),  # door knocker -> dropped
])


def build():
    return sm.build_sales_dataset(MAIN, None, None, WEB)


def test_web_rows_added_and_door_knocker_dropped():
    df = build()
    assert sorted(df["sale_id"]) == [1, 2, 10, 11]
    web = df[df["is_web"]]
    assert set(web["sale_id"]) == {10, 11}
    assert set(web["sales_rep"]) == {sm.WEB_REP}
    assert set(web["sales_channel"]) == {sm.WEB_CHANNEL}


def test_without_web_sales_dataset_is_unchanged():
    df = sm.build_sales_dataset(MAIN, None, None)
    assert list(df["sale_id"]) == [1, 2]
    assert not df["is_web"].any()


def test_exclude_web():
    assert sorted(sm.exclude_web(build())["sale_id"]) == [1, 2]


def test_leaderboard_web_is_all_inbound_with_na_outbound():
    rows = {r["sales_rep"]: r for r in sm.calculate_rep_metrics(build())}
    web = rows[sm.WEB_REP]
    assert web["sales"] == 2 and web["inbound"] == 2
    assert web["outbound"] is None and web["outbound_installs"] is None
    assert rows["Ana Lee"]["outbound"] == 1 and rows["Ana Lee"]["inbound"] == 1
    # N/A rows sort to the bottom of an Outbound sort either way.
    for direction in ("asc", "desc"):
        assert sm.sort_rep_rows(list(rows.values()), "outbound", direction)[-1]["sales_rep"] == sm.WEB_REP


def test_inbound_drill_down_matches_web_count():
    df = build()
    web_df = df[df["sales_rep"] == sm.WEB_REP]
    assert len(sm.LEADERBOARD_METRIC_FILTERS["inbound"](web_df)) == 2


def test_channel_breakdown_web_bar_only_when_present():
    with_web = {r["channel"]: r for r in sm.calculate_channel_breakdown(build())}
    assert with_web[sm.WEB_CHANNEL] == {"channel": "Web", "count": 2, "direction": "inbound"}
    reps_only = [r["channel"] for r in sm.calculate_channel_breakdown(sm.exclude_web(build()))]
    assert sm.WEB_CHANNEL not in reps_only


def test_channel_state_breakdown_has_web_column_counted_inbound():
    df = build()
    df["state"] = "NJ"
    result = sm.calculate_channel_state_breakdown(df)
    assert result["channels"][-1] == sm.WEB_CHANNEL
    nj = result["rows"][0]
    assert nj["channels"][sm.WEB_CHANNEL] == 2
    assert nj["inbound"] == 3 and nj["outbound"] == 1 and nj["total"] == 4
