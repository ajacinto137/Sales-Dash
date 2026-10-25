"""Rep Leaderboard's All Time Install Rate (2026-10-23): the Install Rate
formula over each rep's whole history, regardless of the period shown."""

import pandas as pd

import sales_metrics as sm


def df(rows):
    return pd.DataFrame([
        {"sales_rep": rep, "status": status, "sale_date": pd.Timestamp(day), "sales_channel": "Inbound Call"}
        for rep, status, day in rows
    ])


def test_all_time_install_rate_ignores_the_period():
    everything = df([
        ("amy", "Installed", "2026-09-01"),
        ("amy", "Needs Attention", "2026-09-02"),
        ("amy", "Installed", "2026-10-01"),
        ("amy", "Installed", "2026-10-02"),
    ])
    october = everything[everything["sale_date"] >= "2026-10-01"]
    rows = sm.add_all_time_install_rates(sm.calculate_rep_metrics(october), everything)
    assert rows[0]["install_rate"] == 100.0           # October only
    assert rows[0]["all_time_install_rate"] == 75.0   # 1 - 1/4
    assert [r["sales_rep"] for r in sm.sort_rep_rows(rows, "all_time_install_rate", "desc")] == ["amy"]
