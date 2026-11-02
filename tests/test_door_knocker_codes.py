"""Door-knocker referral codes (2026-11-02): any first-initial + last-name
code is a knocker's, so those sales stay off the internal dashboard.

Run with: pytest tests/test_door_knocker_codes.py -v
"""

import pandas as pd

import sales_metrics as sm
from queries import WEB_SALES_SOURCE_TOKEN, internal_rep_codes, is_door_knocker_code


def test_listed_and_name_shaped_codes_are_knockers():
    assert is_door_knocker_code("mritchie")
    assert is_door_knocker_code("plee")  # listed, even though short
    assert is_door_knocker_code(" TharshBarger ")  # unlisted, name-shaped
    assert is_door_knocker_code("tharshbarger")
    assert is_door_knocker_code("joakley2")


def test_promo_and_random_codes_are_not_knockers():
    for code in ("welcometoplanet", "strausnews", "independence", "halloween26",
                 "njfair2025", "batjq", "e487", "mp-sep-26", "pc1-hopa", "me_jeff26", "", None):
        assert not is_door_knocker_code(code), code


def test_internal_rep_own_name_code_is_not_a_knocker():
    codes = internal_rep_codes(["PAS Employee: Dylan Zupkay", "ORDER_PLANET_X", None])
    assert codes == {"dzupkay"}
    assert not is_door_knocker_code("dzupkay", codes)
    assert is_door_knocker_code("dzupkay")


def _raw(rows):
    return pd.DataFrame({
        "ID": [r[0] for r in rows],
        "SourceToken": [r[1] for r in rows],
        "ReferralData": [r[2] for r in rows],
        "SalesChannel_ID": [2] * len(rows),
        "Date": ["2026-10-01"] * len(rows),
        "DateTime": ["2026-10-01 10:00"] * len(rows),
        "Install Status": ["Not Yet Installed"] * len(rows),
    })


def test_knocker_sales_leave_the_internal_dataset():
    main = _raw([
        (1, "PAS Employee: Dylan Zupkay", None),
        (2, "PAS Employee: Dylan Zupkay", "dzupkay"),  # own code -> stays
        (3, "PAS Employee: Jack Jacinto", "mfontana"),  # keyed in for a knocker -> dropped
    ])
    web = _raw([
        (10, WEB_SALES_SOURCE_TOKEN, "tharshbarger"),  # unlisted knocker -> dropped
        (11, WEB_SALES_SOURCE_TOKEN, "welcometoplanet"),
        (12, WEB_SALES_SOURCE_TOKEN, "dzupkay"),
    ])
    df = sm.build_sales_dataset(main, None, None, web)
    assert sorted(df["sale_id"]) == [1, 2, 11, 12]


def test_referral_code_reps_are_internal_reps():
    assert not is_door_knocker_code("avallorosi")
    assert not is_door_knocker_code("IPonce")
    main = _raw([(1, "PAS Employee: Ana Lee", None)])
    web = _raw([
        (10, WEB_SALES_SOURCE_TOKEN, "AVallorosi"),
        (11, WEB_SALES_SOURCE_TOKEN, "iponce"),
        (12, WEB_SALES_SOURCE_TOKEN, None),
    ])
    df = sm.build_sales_dataset(main, None, None, web).set_index("sale_id")
    assert df.loc[10, "sales_rep"] == "Aldo Vallorosi"
    assert df.loc[11, "sales_rep"] == "iponce"
    assert df.loc[12, "sales_rep"] == sm.WEB_REP
    assert not df.loc[10, "is_web"] and df.loc[12, "is_web"]
    assert df.loc[10, "sales_channel"] == sm.WEB_CHANNEL
    board = {r["sales_rep"]: r for r in sm.calculate_rep_metrics(df.reset_index())}
    assert board["Aldo Vallorosi"]["inbound"] == 1
    assert board["Aldo Vallorosi"]["outbound"] == 0
