"""Tests for the ONE authoritative Marketing Attribution rule
(marketing_attribution.attribute_record()) -- pure function, no database
or Flask app context needed. Covers Tier 1 (UTM), Tier 2 (click IDs),
Tier 3 (_gcl_aw recovery), precedence between tiers, and the
case-insensitive/whitespace-safe/no-double-count rules the spec calls out
explicitly (see marketing_attribution.py's module docstring).

Run with: pytest tests/test_marketing_attribution.py -v
(or just `pytest` from the repo root, alongside the other test files)
"""

import base64
from datetime import datetime, timedelta, timezone

import marketing_attribution as attr


def base_row(**overrides):
    row = {
        "utm_source": None, "utm_medium": None, "utm_campaign": None,
        "gclid": None, "gad_source": None, "gbraid": None, "fbclid": None,
        "msclkid": None, "ndclid": None, "tw_source": None,
        "InsertDate": datetime(2026, 6, 1, 12, 0, 0),
        "uniqueURL": None, "ReferralData": None, "MarketingToken": None,
    }
    row.update(overrides)
    return row


def encode_gcl_aw(gclid, click_dt):
    """Mirrors Google's REAL encoding (confirmed against captured
    production uniqueURL values): standard base64 padding characters
    ("=") are replaced with literal "." rather than stripped -- Google's
    own values consistently end in one or more dots, not "=" or nothing."""
    raw = f"GCL.{int(click_dt.timestamp())}.{gclid}"
    encoded = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
    return encoded.replace("=", ".")


def gcl_aw_url(token):
    """Google's real linker-parameter format: _gcl_aw lives INSIDE a
    single _gl= query parameter, asterisk-delimited alongside other
    sub-values (typically _gcl_au right after it) -- never a standalone
    "_gcl_aw=<value>" parameter. Confirmed against real captured
    uniqueURL values during development; an earlier implementation
    assumed the "=" form and matched zero real rows as a result."""
    return f"https://order.planet.net/?_gl=1*abc123*_gcl_aw*{token}*_gcl_au*999.888"


def eastern_insert_date_as_utc(insert_date):
    """InsertDate is naive and interpreted as America/New_York local time
    (see marketing_attribution.py's EASTERN_TZ comment) -- converts it to
    a UTC instant so tests can build a click timestamp a known distance
    from it, in the same terms _tier3() actually compares against."""
    return insert_date.replace(tzinfo=attr.EASTERN_TZ).astimezone(timezone.utc)


# ---------------- Tier 1: UTM ----------------

def test_tier1_meta_sources():
    for source in ("meta_fb", "meta_ig", "meta_an", "meta_th", "ig"):
        result = attr.attribute_record(base_row(utm_source=source))
        assert result == {"isPaid": True, "paidPlatform": "Meta", "attributionTier": 1, "attributionMethod": "utm_source"}


def test_tier1_google_bing_nextdoor_reddit():
    assert attr.attribute_record(base_row(utm_source="adwords"))["paidPlatform"] == "Google Ads"
    assert attr.attribute_record(base_row(utm_source="bing"))["paidPlatform"] == "Bing"
    assert attr.attribute_record(base_row(utm_source="next-door"))["paidPlatform"] == "Nextdoor"
    assert attr.attribute_record(base_row(utm_source="reddit"))["paidPlatform"] == "Reddit"


def test_tier1_unbounce_leadpages_are_other_paid_not_google_or_meta():
    for source in ("unbounce", "leadpages"):
        result = attr.attribute_record(base_row(utm_source=source))
        assert result["isPaid"] is True
        assert result["paidPlatform"] == "Other Paid"
        assert result["attributionTier"] == 1


def test_tier1_case_insensitive_and_trims_whitespace():
    result = attr.attribute_record(base_row(utm_source="  META_FB  "))
    assert result["isPaid"] is True
    assert result["paidPlatform"] == "Meta"


def test_tier1_medium_only_match_is_other_paid():
    for medium in ("paid-social", "ppc", "paid"):
        result = attr.attribute_record(base_row(utm_medium=medium))
        assert result == {"isPaid": True, "paidPlatform": "Other Paid", "attributionTier": 1, "attributionMethod": "utm_medium"}


def test_tier1_source_takes_precedence_over_medium():
    result = attr.attribute_record(base_row(utm_source="reddit", utm_medium="organic"))
    assert result["paidPlatform"] == "Reddit"
    assert result["attributionMethod"] == "utm_source"


def test_tier1_unpaid_source_and_medium_falls_through():
    result = attr.attribute_record(base_row(utm_source="google-organic", utm_medium="organic"))
    assert result["isPaid"] is False
    assert result["attributionTier"] is None


# ---------------- Tier 2: Click IDs ----------------

def test_tier2_google_click_ids():
    assert attr.attribute_record(base_row(gclid="abc123"))["attributionMethod"] == "gclid"
    assert attr.attribute_record(base_row(gad_source="1"))["attributionMethod"] == "gad_source"
    assert attr.attribute_record(base_row(gbraid="xyz"))["attributionMethod"] == "gbraid"
    for field in ("gclid", "gad_source", "gbraid"):
        result = attr.attribute_record(base_row(**{field: "present"}))
        assert result["isPaid"] is True
        assert result["paidPlatform"] == "Google Ads"
        assert result["attributionTier"] == 2


def test_tier2_meta_bing_nextdoor_click_ids():
    assert attr.attribute_record(base_row(fbclid="f1"))["paidPlatform"] == "Meta"
    assert attr.attribute_record(base_row(msclkid="m1"))["paidPlatform"] == "Bing"
    assert attr.attribute_record(base_row(ndclid="n1"))["paidPlatform"] == "Nextdoor"


def test_tier2_tw_source_google():
    result = attr.attribute_record(base_row(tw_source="google"))
    assert result["isPaid"] is True
    assert result["paidPlatform"] == "Google Ads"
    assert result["attributionMethod"] == "tw_source"


def test_tier2_whitespace_only_click_id_is_not_populated():
    result = attr.attribute_record(base_row(gclid="   "))
    assert result["isPaid"] is False


def test_tier2_multiple_populated_fields_do_not_double_count():
    # gclid AND fbclid both populated -- must still be exactly ONE lead,
    # attributed via the first rule in the precedence order (gclid).
    result = attr.attribute_record(base_row(gclid="g1", fbclid="f1", msclkid="m1"))
    assert result["paidPlatform"] == "Google Ads"
    assert result["attributionMethod"] == "gclid"


def test_tier1_takes_precedence_over_tier2():
    result = attr.attribute_record(base_row(utm_source="reddit", gclid="g1"))
    assert result["paidPlatform"] == "Reddit"
    assert result["attributionTier"] == 1


# ---------------- Tier 3: _gcl_aw recovery ----------------

def test_tier3_valid_gcl_aw_within_the_hour():
    insert_date = datetime(2026, 6, 1, 12, 0, 0)
    click_dt = eastern_insert_date_as_utc(insert_date) - timedelta(minutes=30)
    token = encode_gcl_aw("Cj0KCQjw123", click_dt)
    row = base_row(InsertDate=insert_date, uniqueURL=gcl_aw_url(token))
    result = attr.attribute_record(row)
    assert result == {"isPaid": True, "paidPlatform": "Google Ads", "attributionTier": 3, "attributionMethod": "_gcl_aw"}


def test_tier3_real_captured_value_decodes_correctly():
    # A real _gcl_aw value captured from this app's own production
    # uniqueURL data (an opaque Google click-tracking artifact, not
    # personal data) -- a direct regression guard against the "=" vs "."
    # padding bug and the "=" vs "*" delimiter bug found during
    # development. Decodes to GCL.1772624733.<gclid>; InsertDate is set
    # to exactly that click instant (converted to Eastern) so the
    # one-hour freshness check passes.
    real_value = (
        "R0NMLjE3NzI2MjQ3MzMuRUFJYUlRb2JDaE1Ja3VDUDE1V0drd01WTFdCSEFSMV9jd3pwRUFB"
        "WUFTQUNFZ0prY3ZEX0J3RQ.."
    )
    click_dt_utc = datetime.fromtimestamp(1772624733, tz=timezone.utc)
    insert_date = click_dt_utc.astimezone(attr.EASTERN_TZ).replace(tzinfo=None)
    row = base_row(
        InsertDate=insert_date,
        uniqueURL=f"https://order.planet.net/?_gl=1*d9fhja*_gcl_aw*{real_value}*_gcl_au*NDU2NzQ4NzY3LjE3NzI2MjQ3MzI.",
    )
    result = attr.attribute_record(row)
    assert result["isPaid"] is True
    assert result["paidPlatform"] == "Google Ads"
    assert result["attributionTier"] == 3


def test_tier3_stale_gcl_aw_outside_the_hour_is_rejected():
    insert_date = datetime(2026, 6, 1, 12, 0, 0)
    click_dt = eastern_insert_date_as_utc(insert_date) - timedelta(hours=5)
    token = encode_gcl_aw("Cj0KCQjw123", click_dt)
    row = base_row(InsertDate=insert_date, ReferralData=gcl_aw_url(token))
    result = attr.attribute_record(row)
    assert result["isPaid"] is False


def test_tier3_malformed_base64_never_raises():
    row = base_row(uniqueURL=gcl_aw_url("not-valid-base64!!!"))
    result = attr.attribute_record(row)
    assert result["isPaid"] is False


def test_tier3_missing_gcl_aw_falls_through_cleanly():
    row = base_row(uniqueURL="https://example.com/?utm_source=organic")
    result = attr.attribute_record(row)
    assert result["isPaid"] is False


def test_tier3_never_matches_gcl_au():
    # _gcl_au is set on nearly every visit and carries no ad signal --
    # must never be read as _gcl_aw, even though the two share a prefix
    # and appear right next to each other in Google's real _gl= format.
    row = base_row(uniqueURL="https://order.planet.net/?_gl=1*abc*_gcl_au*ignoreme.")
    result = attr.attribute_record(row)
    assert result["isPaid"] is False


def test_tier3_checks_multiple_fields_in_order():
    insert_date = datetime(2026, 6, 1, 12, 0, 0)
    click_dt = eastern_insert_date_as_utc(insert_date) - timedelta(minutes=10)
    token = encode_gcl_aw("Cj0KCQjw999", click_dt)
    row = base_row(InsertDate=insert_date, MarketingToken=gcl_aw_url(token))
    result = attr.attribute_record(row)
    assert result["isPaid"] is True
    assert result["attributionMethod"] == "_gcl_aw"


def test_tier1_and_tier2_take_precedence_over_tier3():
    insert_date = datetime(2026, 6, 1, 12, 0, 0)
    click_dt = eastern_insert_date_as_utc(insert_date) - timedelta(minutes=5)
    token = encode_gcl_aw("Cj0KCQjw999", click_dt)
    row = base_row(InsertDate=insert_date, fbclid="f1", uniqueURL=gcl_aw_url(token))
    result = attr.attribute_record(row)
    assert result["paidPlatform"] == "Meta"
    assert result["attributionTier"] == 2


def test_decode_gcl_aw_never_raises_on_garbage():
    assert attr.decode_gcl_aw("") == (None, None)
    assert attr.decode_gcl_aw(None) == (None, None)
    assert attr.decode_gcl_aw("!!!not base64!!!") == (None, None)
    assert attr.decode_gcl_aw(base64.urlsafe_b64encode(b"too.few").decode()) == (None, None)


# ---------------- No attribution at all ----------------

def test_completely_organic_record_is_not_paid():
    result = attr.attribute_record(base_row())
    assert result == {"isPaid": False, "paidPlatform": None, "attributionTier": None, "attributionMethod": None}
