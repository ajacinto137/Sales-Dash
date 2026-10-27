"""order_search -- the global search's every-order index (pure pandas)."""

import pandas as pd

import order_search
from queries import WEB_SALES_SOURCE_TOKEN

RAW = pd.DataFrame([
    (1, "James", "King", "127 Squaw Trail", None, "Milford", "pa", "18337", "2026-10-19 15:17", "MRitchie", None, float("nan")),
    (2, "Ann", "Kingsley", "5 Main St", "Apt 2", "Vernon", "NJ", "07462", "2024-03-01 10:00", None, "PAS Employee: Dylan Zupkay", 12),
    (3, "Bob", "Smith", "9 King Rd", None, "Sparta", "NJ", "", "2025-06-01 09:00", None, WEB_SALES_SOURCE_TOKEN, None),
    (4, "Cy", "Old", "1 Elm", None, "", "", "", "2021-01-01 09:00", float("nan"), None, None),
], columns=["ID", "FirstName", "LastName", "Address", "ApartmentSuite", "City", "State", "Zipcode",
            "InsertDate", "ReferralData", "SourceToken", "InsertByEmployeeID"])


def test_index_attributes_seller_and_formats_address():
    idx = order_search.build_index(RAW).set_index("sale_id")
    assert idx.loc[1, "sold_by"] == "mritchie" and idx.loc[1, "sold_by_kind"] == "knocker"
    assert (idx.loc[2, "sold_by"], idx.loc[2, "sold_by_kind"]) == ("Dylan Zupkay", "rep")
    assert (idx.loc[3, "sold_by"], idx.loc[3, "sold_by_kind"]) == ("Web", "web")
    assert idx.loc[4, "sold_by"] is None
    assert idx.loc[2, "address"] == "5 Main St Apt 2" and idx.loc[2, "city_state_zip"] == "Vernon, NJ 07462"
    assert idx.loc[1, "city_state_zip"] == "Milford, PA 18337" and idx.loc[4, "city_state_zip"] == ""


def test_search_matches_any_year_name_prefix_first():
    idx = order_search.build_index(RAW)
    ids = lambda q: order_search.search(idx, q, 8)[0]["sale_id"].tolist()
    assert ids("james king") == [1]
    # "king": name-prefix hits (none start with it) then newest first.
    assert ids("king") == [1, 3, 2]
    assert ids("ann") == [2]
    assert order_search.search(idx, "  ", 8)[0].empty
    assert order_search.find(idx, 4)["last_name"] == "Old" and order_search.find(idx, 99) is None


def test_address_search_ignores_format_and_offers_closest_match():
    idx = order_search.build_index(RAW)
    ids = lambda q: order_search.search(idx, q, 8)[0]["sale_id"].tolist()
    closest = lambda q: order_search.search(idx, q, 8)[1]
    # Full address with city/state/ZIP, punctuation, spacing, abbreviations.
    for q in ("127 Squaw Trail, Milford, PA 18337", "127 squaw trl milford pa", "127  Squaw   Trail",
              "5 Main Street Apt 2", "5 main st, vernon nj", "9 King Road"):
        assert len(ids(q)) == 1 and not closest(q), q
    assert ids("5 Main Street Apt 2") == [2] and ids("9 King Road") == [3]
    # Misspelled / partly wrong: no exact match, closest one offered.
    assert ids("127 Squaw Trial") == [1] and closest("127 Squaw Trial")
    assert ids("5 Mian St Vernon") == [2] and closest("5 Mian St Vernon")
    # House number decides between otherwise equal matches.
    assert ids("9 Kingg Rd")[0] == 3
    # Too little in common: nothing.
    assert ids("42 Wallaby Way Sydney") == []
