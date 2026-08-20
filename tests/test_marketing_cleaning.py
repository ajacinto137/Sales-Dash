"""Tests for the trickiest/most bug-prone parts of marketing_cleaning.py
-- the data cleaning + 5-way channel attribution pipeline behind the
Marketing Channel Report (scripts/generate_marketing_report.py). Pure
functions, no database needed.

Run with: pytest tests/test_marketing_cleaning.py -v
"""

from datetime import datetime, timezone

import marketing_cleaning as mc


def base_row(**overrides):
    row = {
        "InsertDate": datetime(2026, 6, 1, 12, 0, 0),
        "AvailabilityID": 1,
        "Zipcode": "07461",
        "NJPR_municipalityName": "Wantage",
        "MarketingToken": None, "ReferralData": None, "email_id": "a" * 64, "uniqueURL": None,
        "utm_source": None, "utm_medium": None, "utm_campaign": None,
        "fbclid": None, "msclkid": None, "ndclid": None, "tw_source": None,
        "gad_source": None, "gbraid": None, "gclid": None,
    }
    row.update(overrides)
    return row


# ---------------- Zip / state cleaning ----------------

def test_clean_zipcode_strips_plus4_and_pads():
    assert mc.clean_zipcode("07461-1234") == "07461"
    assert mc.clean_zipcode("7461") == "07461"
    assert mc.clean_zipcode(7461) == "07461"
    assert mc.clean_zipcode("  07461  ") == "07461"
    assert mc.clean_zipcode(None) == ""
    assert mc.clean_zipcode("") == ""
    assert mc.clean_zipcode("abc") == ""


def test_derive_state_footprint_states():
    assert mc.derive_state("07461") == "NJ"
    assert mc.derive_state("12561") == "NY"
    assert mc.derive_state("18301") == "PA"
    assert mc.derive_state("22101") == "VA"


def test_derive_state_northern_virginia_201_maps_to_va_not_dc():
    # 200-205 is nominally "DC" in the textbook USPS table, but Planet
    # Networks has no DC footprint -- real rows in this range are
    # Northern Virginia, processed through the DC postal SCF despite
    # being physically in VA. Deliberately mapped to VA.
    assert mc.derive_state("20105") == "VA"
    assert mc.derive_state("20164") == "VA"


def test_derive_state_unresolvable_is_unknown():
    assert mc.derive_state("") == "Unknown"
    assert mc.derive_state("00501") == "Unknown"  # 000-005 reserved
    assert mc.derive_state("abc12") == "Unknown"


# ---------------- AvailabilityID validation ----------------

def test_availability_id_valid_range():
    for v in range(7):
        assert mc.clean_availability_id(v) == v
    assert mc.clean_availability_id(7) is None
    assert mc.clean_availability_id(-1) is None
    assert mc.clean_availability_id(None) is None
    assert mc.clean_availability_id("not a number") is None


def test_clean_row_drops_invalid_availability():
    assert mc.clean_row(base_row(AvailabilityID=99)) is None
    cleaned = mc.clean_row(base_row(AvailabilityID=1))
    assert cleaned is not None
    assert cleaned["AvailabilityID"] == 1
    assert cleaned["State"] == "NJ"


# ---------------- Paid tier: platform matching ----------------

def test_paid_google_adwords_and_click_ids():
    for row in [base_row(utm_source="adwords"), base_row(gclid="g1"), base_row(gad_source="1"),
                base_row(gbraid="b1"), base_row(tw_source="google")]:
        cleaned = mc.clean_row(row)
        tags = mc._assign_channel_pre_reuse(cleaned)
        assert tags["channel_group"] == "Paid"
        assert tags["channel_detail"] == "Google"


def test_paid_meta_prefix_match_handles_template_placeholder():
    # Real production data includes an unresolved templating placeholder
    # like "meta_{{site_source_name}}" -- prefix match must still catch it.
    row = base_row(utm_source="meta_%7B%7Bsite_source_name%7D%7D")
    cleaned = mc.clean_row(row)
    tags = mc._assign_channel_pre_reuse(cleaned)
    assert tags["channel_group"] == "Paid"
    assert tags["channel_detail"] == "Meta"


def test_paid_landing_page_hosts_are_paid_lp_not_google_or_meta():
    for source in ("unbounce", "leadpages"):
        cleaned = mc.clean_row(base_row(utm_source=source))
        tags = mc._assign_channel_pre_reuse(cleaned)
        assert tags["channel_group"] == "Paid"
        assert tags["channel_detail"] == "Paid (LP)"


def test_paid_medium_alone_qualifies():
    for medium in ("paid-social", "ppc", "paid"):
        cleaned = mc.clean_row(base_row(utm_medium=medium))
        tags = mc._assign_channel_pre_reuse(cleaned)
        assert tags["channel_group"] == "Paid"
        assert tags["channel_detail"] == "Other Paid"


def test_medium_cpc_alone_does_not_qualify():
    # "cpc" is a REAL medium value in production data but is NOT in the
    # paid-medium-alone list {paid-social, ppc, paid} -- must not be paid
    # without a qualifying source/click id.
    cleaned = mc.clean_row(base_row(utm_medium="cpc", utm_source="chatgpt"))
    tags = mc._assign_channel_pre_reuse(cleaned)
    assert tags["channel_group"] != "Paid"


# ---------------- Exclusion: utm_medium=social ----------------

def test_social_medium_excludes_paid_even_with_fbclid():
    # Real production data: every utm_source="ig" row pairs with
    # utm_medium="social" and often carries a real fbclid (IG bio-link
    # click) -- this must NOT be classified paid.
    cleaned = mc.clean_row(base_row(utm_source="ig", utm_medium="social", fbclid="f1"))
    tags = mc._assign_channel_pre_reuse(cleaned)
    assert tags["channel_group"] != "Paid"


def test_paid_social_medium_is_not_excluded():
    # paid-social must still qualify -- only the exact "social" (organic)
    # medium is excluded, not the "paid-social" one.
    cleaned = mc.clean_row(base_row(utm_medium="paid-social"))
    tags = mc._assign_channel_pre_reuse(cleaned)
    assert tags["channel_group"] == "Paid"


# ---------------- Tier precedence: EMAIL / AI / Offline ----------------

def test_email_tier_sources_and_token():
    for source in ("sfmc", "hs_email", "hs_automation", "newsletter", "Community Newsletter"):
        cleaned = mc.clean_row(base_row(utm_source=source))
        tags = mc._assign_channel_pre_reuse(cleaned)
        assert tags["channel_group"] == "Email"
    cleaned = mc.clean_row(base_row(MarketingToken="EM1208B"))
    tags = mc._assign_channel_pre_reuse(cleaned)
    assert tags["channel_group"] == "Email"


def test_ai_referral_sources():
    for source in ("chatgpt.com", "chatgpt", "copilot.com", "perplexity"):
        cleaned = mc.clean_row(base_row(utm_source=source))
        tags = mc._assign_channel_pre_reuse(cleaned)
        assert tags["channel_group"] == "AI-Referral"


def test_organic_direct_catch_all():
    cleaned = mc.clean_row(base_row())
    tags = mc._assign_channel_pre_reuse(cleaned)
    assert tags["channel_group"] == "Organic-Direct"


# ---------------- MarketingToken sub-classification ----------------

def test_offline_referral_pc_pattern():
    assert mc.classify_offline_referral("PC1") == "Direct mail — confirm"
    assert mc.classify_offline_referral("PC2") == "Direct mail — confirm"


def test_offline_referral_mp_pattern():
    assert mc.classify_offline_referral("MP-AUG-26") == "Agency campaign"


def test_offline_referral_keywords():
    for token in ("facebook", "instagram", "gaming", "strausnews"):
        assert mc.classify_offline_referral(token) == "Organic social / press"


def test_offline_referral_dated_codes():
    assert mc.classify_offline_referral("NJFAIR2025") == "Event / one-off"
    assert mc.classify_offline_referral("LMM20260803") == "Event / one-off"


def test_offline_referral_short_codes_are_unknown_not_names():
    # Real production MarketingToken values -- short, mostly-uppercase
    # agency/campaign codes that must NOT be classified as a rep name.
    for token in ("PVA1", "LOB7", "PMG1", "MG1", "NSW", "PN25", "PNVA", "SFEDa", "SFEDb"):
        assert mc.classify_offline_referral(token) == "Unknown — confirm"


def test_offline_referral_names_not_swallowed_by_short_code_pattern():
    # "Jberg" (Capital + lowercase surname) must NOT be caught by the
    # short-code pattern just because it's <= 5 chars -- this was a real
    # bug found against production data during development (the loose
    # short-code regex originally matched it before the name check ran).
    for token in ("MRitchie", "jmortensen", "Jberg", "dthompson", "mastle2"):
        assert mc.classify_offline_referral(token) == "Sales rep / referral — confirm"


def test_offline_referral_never_returns_none():
    for token in ("welcometoplanet-em-butler", "constructionsqsp", "xyz123abc", ""):
        result = mc.classify_offline_referral(token)
        assert result is not None
        assert "confirm" in result.lower() or result in ("Organic social / press", "Event / one-off", "Agency campaign", "Direct mail — confirm")


# ---------------- _gcl_aw cookie fallback ----------------
# Google's real linker-parameter format packs _gcl_aw INSIDE a single
# _gl= query parameter, asterisk-delimited (typically alongside _gcl_au
# right after it): ?_gl=1*<id>*_gcl_aw*<VALUE>*_gcl_au*<other-value> --
# NOT a standalone "_gcl_aw=<value>" parameter, and the value itself is
# terminated with literal "." characters instead of "=" padding. Both
# were wrong in an earlier version of this module and meant Tier 3 never
# matched a single real production row; confirmed against real captured
# uniqueURL values before fixing -- see marketing_cleaning.py's
# _decode_gcl_aw()/_GCL_AW_PATTERN comments.

def test_decode_gcl_aw_url_safe_alphabet_never_raises():
    assert mc._decode_gcl_aw("") == (None, None)
    assert mc._decode_gcl_aw(None) == (None, None)
    assert mc._decode_gcl_aw("not-valid!!!") == (None, None)


def test_gcl_aw_real_captured_value_decodes_and_recovers_google():
    # A real _gcl_aw value captured from this app's own production
    # uniqueURL data (an opaque Google click-tracking artifact, not
    # personal data) -- the direct regression guard for both bugs found
    # during development. Decodes to GCL.1772624733.<gclid>; InsertDate
    # is set to exactly that click instant so the one-hour freshness
    # check passes.
    real_value = (
        "R0NMLjE3NzI2MjQ3MzMuRUFJYUlRb2JDaE1Ja3VDUDE1V0drd01WTFdCSEFSMV9jd3pwRUFB"
        "WUFTQUNFZ0prY3ZEX0J3RQ.."
    )
    url = f"https://order.planet.net/?_gl=1*d9fhja*_gcl_aw*{real_value}*_gcl_au*NDU2NzQ4NzY3LjE3NzI2MjQ3MzI."
    click_dt_utc = datetime.fromtimestamp(1772624733, tz=timezone.utc)
    insert_date = click_dt_utc.astimezone(mc.EASTERN_TZ).replace(tzinfo=None)
    assert mc._gcl_aw_fallback(url, insert_date) == "Google"


def test_gcl_aw_never_used_for_gcl_au():
    # Regression guard: the extraction regex must only ever match
    # "_gcl_aw*", never "_gcl_au*" (spec: "set on nearly all visits, not
    # an ad signal").
    url = "https://order.planet.net/?_gl=1*abc*_gcl_au*1.2.345.678"
    assert mc._gcl_aw_fallback(url, datetime(2026, 6, 1, 12, 0, 0)) is None


# ---------------- Click-ID reuse dedup ----------------

def test_reused_gclid_credits_first_and_within_window_demotes_rest():
    t0 = datetime(2026, 6, 1, 12, 0, 0)
    rows = [
        mc.clean_row(base_row(InsertDate=t0, gclid="shared", email_id="e1" * 32)),
        mc.clean_row(base_row(InsertDate=datetime(2026, 6, 1, 12, 30, 0), gclid="shared", email_id="e2" * 32)),  # within 60 min -- still credited
        mc.clean_row(base_row(InsertDate=datetime(2026, 6, 1, 15, 0, 0), gclid="shared", email_id="e3" * 32)),  # 3 hrs later, no independent UTM -- demoted
        mc.clean_row(base_row(InsertDate=datetime(2026, 6, 1, 16, 0, 0), gclid="shared", utm_source="adwords", email_id="e4" * 32)),  # later but has independent UTM evidence
    ]
    output_rows, notes = mc.tag_channels(rows)
    assert output_rows[0]["channel_group"] == "Paid" and output_rows[0]["channel_detail"] == "Google"
    assert output_rows[1]["channel_group"] == "Paid" and output_rows[1]["channel_detail"] == "Google"
    assert output_rows[2]["channel_group"] != "Paid"  # demoted -- no gclid, no other signal -> falls to Organic-Direct
    assert output_rows[3]["channel_group"] == "Paid" and output_rows[3]["channel_detail"] == "Google"
    assert notes["reused_click_ids_demoted"] == 1


def test_gbraid_reuse_is_never_demoted():
    # gbraid is shared by design (privacy-preserving attribution) --
    # must NOT be subject to the reuse-dedup pass at all.
    t0 = datetime(2026, 6, 1, 12, 0, 0)
    rows = [
        mc.clean_row(base_row(InsertDate=t0, gbraid="shared", email_id="e1" * 32)),
        mc.clean_row(base_row(InsertDate=datetime(2026, 6, 2, 12, 0, 0), gbraid="shared", email_id="e2" * 32)),  # 24 hrs later
    ]
    output_rows, notes = mc.tag_channels(rows)
    assert all(r["channel_group"] == "Paid" and r["channel_detail"] == "Google" for r in output_rows)
    assert notes["reused_click_ids_demoted"] == 0


# ---------------- Paid attribution never requires utm_campaign ----------------
# Click-ID-only paid leads (gclid/fbclid with no campaign param) are
# common and legitimate -- a blank utm_campaign must never keep a row out
# of Paid.

def test_paid_via_utm_source_with_blank_campaign():
    rows = [mc.clean_row(base_row(utm_source="adwords", utm_campaign=None))]
    output_rows, _notes = mc.tag_channels(rows)
    assert output_rows[0]["channel_group"] == "Paid"


def test_paid_via_click_id_with_blank_campaign():
    rows = [mc.clean_row(base_row(gclid="g1", utm_campaign=None))]
    output_rows, _notes = mc.tag_channels(rows)
    assert output_rows[0]["channel_group"] == "Paid"


# ---------------- Dashboard payload shaping ----------------

def test_build_dashboard_payload_shape_and_no_dedup():
    rows = [
        mc.clean_row(base_row(InsertDate=datetime(2026, 3, 1, 9, 0, 0), email_id="e1" * 32)),
        mc.clean_row(base_row(InsertDate=datetime(2026, 3, 1, 10, 0, 0), email_id="e1" * 32)),  # same submitter, same day -- NOT deduped here
        mc.clean_row(base_row(InsertDate=datetime(2026, 3, 3, 9, 0, 0), email_id="e2" * 32)),
    ]
    output_rows, _ = mc.tag_channels(rows)
    payload = mc.build_dashboard_payload(output_rows)
    assert payload["day0"] == "2026-03-01"
    assert payload["maxday"] == 2
    assert payload["raw_rows"] == 3
    assert len(payload["d"]) == 3  # not deduplicated -- dedup is a client-side, date-range-scoped concern
    assert payload["d"] == [0, 0, 2]


# ---------------- run_cleaning_cached() -- hourly cache (added 2026-08-20) ----------------
# mc.run_cleaning and mc.time.monotonic are monkeypatched directly (not via
# the pytest fixture) and always restored in a finally block, since these
# are module-level globals shared with every other test in this file/
# session -- a leaked patch here would silently break unrelated tests.

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
