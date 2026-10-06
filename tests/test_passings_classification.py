"""Tests for the ONE authoritative Passings & Leads classification rule
(passings_classification.classify()) -- pure functions, no database or
Flask app context needed. Covers is_valid_email()'s edge cases and every
branch of the spec's classification tree (spec #32): Prequal as Business
always wins regardless of property type; commercial-relevant property
type (independent of email) makes a Commercial Field Target; a valid
email on a non-commercial property makes a Residential Lead; neither
makes a plain Passing with no prospecting category. Also covers the
commercial-property keyword matching against the REAL ClassName values
observed in PlanetWeb's View_Places (Residential Property (1 - 4 Family),
Commercial, Public Property, Farm (House), Farm (Qualified), Other
Exempt, Church & Charitable Property, Industrial, Apartment, Public
School Property, Vacant Land, Other School Property, Cemeteries &
Graveyards) -- see passings_classification.py's module docstring."""

import passings_classification as pc


# ---------------- is_valid_email() ----------------

def test_valid_email_accepted():
    assert pc.is_valid_email("someone@example.com") is True


def test_none_email_rejected():
    assert pc.is_valid_email(None) is False


def test_blank_email_rejected():
    assert pc.is_valid_email("") is False


def test_whitespace_only_email_rejected():
    assert pc.is_valid_email("   ") is False


def test_literal_none_string_rejected():
    assert pc.is_valid_email("None") is False
    assert pc.is_valid_email("none") is False
    assert pc.is_valid_email(" NoNe ") is False


def test_email_with_surrounding_whitespace_accepted():
    assert pc.is_valid_email("  someone@example.com  ") is True


# ---------------- commercial_property_label() against real ClassName values ----------------

def test_residential_property_not_commercial():
    assert pc.commercial_property_label("Residential Property (1 - 4 Family)") is None


def test_commercial_classname_matches_commercial():
    assert pc.commercial_property_label("Commercial") == "Commercial"


def test_industrial_classname_matches_industrial():
    assert pc.commercial_property_label("Industrial") == "Industrial"


def test_church_and_charitable_matches_church_religious():
    assert pc.commercial_property_label("Church & Charitable Property") == "Church / Religious"


def test_public_property_not_matched():
    # Real ClassName value observed in production data -- deliberately
    # NOT one of the spec's three named commercial-relevant categories
    # (Commercial/Industrial/Church/Religious), so this must stay
    # unmatched rather than guessing it belongs in the mapping.
    assert pc.commercial_property_label("Public Property") is None


def test_farm_not_matched():
    assert pc.commercial_property_label("Farm (Qualified)") is None


def test_none_classname_not_matched():
    assert pc.commercial_property_label(None) is None


def test_blank_classname_not_matched():
    assert pc.commercial_property_label("") is None


def test_case_insensitive_match():
    assert pc.commercial_property_label("commercial") == "Commercial"
    assert pc.commercial_property_label("INDUSTRIAL PARK") == "Industrial"


# ---------------- classify(): the full spec #32 decision tree ----------------

def test_prequal_business_wins_regardless_of_property_type():
    """AuxVar3 = Prequal as Business is a HOT COMMERCIAL LEAD even on a
    residential-looking property -- never conflated with AuxVar4."""
    result = pc.classify({
        "email": None,
        "prequal_as_business": True,
        "class_name": "Residential Property (1 - 4 Family)",
    })
    assert result["category"] == pc.CATEGORY_COMMERCIAL_HOT_LEAD
    assert result["is_prequal_business"] is True
    assert result["is_commercial_property"] is False


def test_prequal_business_wins_over_commercial_property_too():
    """A commercial property that ALSO prequalified is a Hot Lead, not a
    Field Target -- Hot Lead takes priority."""
    result = pc.classify({
        "email": "biz@example.com",
        "prequal_as_business": True,
        "class_name": "Commercial",
    })
    assert result["category"] == pc.CATEGORY_COMMERCIAL_HOT_LEAD


def test_commercial_property_without_prequal_is_field_target():
    result = pc.classify({
        "email": None,
        "prequal_as_business": False,
        "class_name": "Industrial",
    })
    assert result["category"] == pc.CATEGORY_COMMERCIAL_FIELD_TARGET
    assert result["is_lead"] is False


def test_commercial_property_without_prequal_stays_field_target_even_with_email():
    """Commercial property type is a targeting signal, not automatically a
    lead -- having an email doesn't reclassify it as a Residential Lead."""
    result = pc.classify({
        "email": "someone@example.com",
        "prequal_as_business": False,
        "class_name": "Church & Charitable Property",
    })
    assert result["category"] == pc.CATEGORY_COMMERCIAL_FIELD_TARGET
    assert result["is_lead"] is True


def test_residential_with_valid_email_is_residential_lead():
    result = pc.classify({
        "email": "resident@example.com",
        "prequal_as_business": False,
        "class_name": "Residential Property (1 - 4 Family)",
    })
    assert result["category"] == pc.CATEGORY_RESIDENTIAL_LEAD
    assert result["primary_action"] == "Call"


def test_residential_without_email_is_none_category():
    result = pc.classify({
        "email": None,
        "prequal_as_business": False,
        "class_name": "Residential Property (1 - 4 Family)",
    })
    assert result["category"] == pc.CATEGORY_NONE
    assert result["is_lead"] is False
    assert result["category_label"] is None


def test_null_classname_with_email_is_residential_lead():
    """Missing/unknown ClassName (spec: "missing property classes" must be
    handled gracefully) is NOT commercial-relevant by default."""
    result = pc.classify({"email": "someone@example.com", "prequal_as_business": False, "class_name": None})
    assert result["category"] == pc.CATEGORY_RESIDENTIAL_LEAD


def test_categories_are_mutually_exclusive_and_exhaustive():
    combos = [
        {"email": e, "prequal_as_business": p, "class_name": c}
        for e in (None, "a@b.com")
        for p in (True, False)
        for c in (None, "Commercial", "Residential Property (1 - 4 Family)")
    ]
    valid_categories = {
        pc.CATEGORY_RESIDENTIAL_LEAD,
        pc.CATEGORY_COMMERCIAL_HOT_LEAD,
        pc.CATEGORY_COMMERCIAL_FIELD_TARGET,
        pc.CATEGORY_NONE,
    }
    for combo in combos:
        result = pc.classify(combo)
        assert result["category"] in valid_categories


# ---------------- availability_label() ----------------

def test_availability_label_1_is_available_now():
    assert pc.availability_label(1) == "Available Now"


def test_availability_label_3_is_pre_order():
    assert pc.availability_label(3) == "Pre-Order"
