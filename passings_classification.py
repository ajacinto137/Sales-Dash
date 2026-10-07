"""Centralized, pure business-rule logic for the Passings & Leads feature
(see README.md "Passings & Leads"). No I/O anywhere in this module -- both
passings_data.py (the live page) and app.py's Admin Data Tools route import
from here, so a Passing/Lead/Target is classified exactly the same way in
both places and can never drift apart the way "scattered hardcoded
conditions throughout components" (the thing the spec explicitly warned
against) would let happen.

Source field mapping this module is built against (from the stored
procedure `SP_CampaignEmailListBuilder_24`, PlanetWeb's own campaign-list
generator -- see passings_data.py's module docstring for the full query):

    EmailAddress -> most recent valid email for the PlaceID
    AuxVar1      -> BecameAvailable (COALESCE date, see passings_data.py)
    AuxVar2      -> AddressUnits
    AuxVar3      -> "Prequal as Business" flag (a business actually
                    submitted a prequal with contact info -- real lead
                    intent, an EXISTS check against FTTPFormData.IsBusiness)
    AuxVar4      -> ClassName / property type
    AuxVar5      -> PlaceID
"""

# ============================================================
# Lead: a Passing with a real, usable email address
# ============================================================

_INVALID_EMAIL_VALUES = {"", "none"}


def is_valid_email(value):
    """A Lead requires a real email. Rejects None, blank/whitespace-only
    strings, and the literal string "None" (how EmailServiceCampaignStaticListItems
    itself represents a missing address, per SP_CampaignEmailListBuilder_24's
    own `IsNull([EmailAddress],'None')`) -- never treated as a usable
    address here even though it's a non-empty string."""
    if value is None:
        return False
    text = str(value).strip()
    return text.lower() not in _INVALID_EMAIL_VALUES


# ============================================================
# Availability (As_AvailabilityID) -- this feature only ever sees 1/3
# (the SP's own WHERE p.As_AvailabilityID IN (1,3)). Labels reuse the
# exact wording already established for these IDs in
# planet_cleaning/pipeline.py's STATUSES/the Marketing Channel
# Report template's STATUS_FULL array, so this app never shows two
# different names for the same AvailabilityID.
# ============================================================

AVAILABILITY_LABELS = {
    1: "Available Now",
    3: "Pre-Order",
}

# Full wording, for the Admin Data Tools raw-data tab (matches
# templates/marketing_report_template.html's STATUS_FULL exactly).
AVAILABILITY_LABELS_FULL = {
    1: "Available Now",
    3: "Coming soon (preorder)",
}


def availability_label(availability_id):
    return AVAILABILITY_LABELS.get(availability_id, f"Unknown ({availability_id})" if availability_id is not None else "Unknown")


# ============================================================
# Commercial-relevant property types (AuxVar4 / ClassName) -- spec #5:
# "Create the property-type mapping in a centralized/configurable location
# rather than scattering hardcoded conditions throughout components."
#
# This app cannot query the live PlanetWeb server to see real ClassName
# values ahead of time, so this is a case-insensitive KEYWORD match against
# the category names the spec itself lists, not a hardcoded exact-string
# enum. Verify/tighten this against real ClassName values (with counts) via
# Admin -> Data Tools -> Passings & Leads Data once this is live, and edit
# the tuples below -- this is the one place that mapping lives.
#
# IMPORTANT (spec #5/#32): a commercial-relevant property type is a
# targeting signal only. It is NEVER equivalent to Prequal as Business
# (AuxVar3) -- see classify() below, which checks them independently.
# ============================================================

COMMERCIAL_PROPERTY_KEYWORDS = [
    ("Church / Religious", ("church", "religious", "worship", "synagogue", "mosque", "temple")),
    ("Industrial", ("industrial", "warehouse", "manufactur")),
    ("Commercial", ("commercial",)),
]


def keywords_for(label):
    """The keyword tuple for one commercial-relevant label (Commercial /
    Industrial / Church / Religious) -- lets passings_data.py build the SQL
    LIKE predicate for a single property-type filter from the SAME list
    used by commercial_property_label() below, instead of a second
    hand-maintained copy."""
    for candidate_label, keywords in COMMERCIAL_PROPERTY_KEYWORDS:
        if candidate_label == label:
            return keywords
    return ()


def commercial_property_label(class_name):
    """The commercial-relevant category label for a ClassName, or None if
    this property type isn't one of the commercial-relevant categories
    (e.g. residential/unknown -- the common case, so returning None rather
    than raising keeps every caller a plain truthiness check)."""
    if not class_name:
        return None
    text = str(class_name).strip().lower()
    if not text:
        return None
    for label, keywords in COMMERCIAL_PROPERTY_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return label
    return None


# ============================================================
# The three prospecting categories (spec #7/#32) -- the source of truth
# for how a Passing is bucketed. AuxVar3 (Prequal as Business) and AuxVar4
# (property type) are checked independently and NEVER conflated:
#
#   Prequal as Business (AuxVar3)          -> Commercial Hot Lead, always,
#                                              regardless of property type.
#   Commercial-relevant AuxVar4,
#     NOT Prequal as Business              -> Commercial Field Target.
#   Not commercial-relevant, has email     -> Residential Lead.
#   Not commercial-relevant, no email      -> not a lead or target ("none")
#                                              -- still a Passing, just not
#                                              one of the three prospecting
#                                              categories.
# ============================================================

CATEGORY_RESIDENTIAL_LEAD = "residential_lead"
CATEGORY_COMMERCIAL_HOT_LEAD = "commercial_hot_lead"
CATEGORY_COMMERCIAL_FIELD_TARGET = "commercial_field_target"
CATEGORY_NONE = "none"

CATEGORY_LABELS = {
    CATEGORY_RESIDENTIAL_LEAD: "Residential Lead",
    CATEGORY_COMMERCIAL_HOT_LEAD: "Commercial Hot Lead",
    CATEGORY_COMMERCIAL_FIELD_TARGET: "Commercial Field Target",
    CATEGORY_NONE: None,
}

# Primary action shown alongside the category badge/filter (spec #7).
CATEGORY_PRIMARY_ACTION = {
    CATEGORY_RESIDENTIAL_LEAD: "Call",
    CATEGORY_COMMERCIAL_HOT_LEAD: "Call / Email / Visit",
    CATEGORY_COMMERCIAL_FIELD_TARGET: "Field Canvas / Visit",
    CATEGORY_NONE: None,
}


def classify(record):
    """record: dict with `email`, `prequal_as_business` (bool-ish), and
    `class_name`. Returns a dict every caller (passings_data.py's row
    builder, the Admin Data Tools raw-row view) uses identically:

        is_lead                 -- has a valid email (spec #3)
        is_prequal_business     -- AuxVar3 flag, independent of category
        is_commercial_property  -- AuxVar4 maps to a commercial-relevant
                                    type, independent of AuxVar3
        property_type_label     -- the matched label, or None
        category                -- one of CATEGORY_* above
        primary_action          -- CATEGORY_PRIMARY_ACTION[category]
    """
    is_lead = is_valid_email(record.get("email"))
    is_prequal_business = bool(record.get("prequal_as_business"))
    property_type_label = commercial_property_label(record.get("class_name"))
    is_commercial_property = property_type_label is not None

    if is_prequal_business:
        category = CATEGORY_COMMERCIAL_HOT_LEAD
    elif is_commercial_property:
        category = CATEGORY_COMMERCIAL_FIELD_TARGET
    elif is_lead:
        category = CATEGORY_RESIDENTIAL_LEAD
    else:
        category = CATEGORY_NONE

    return {
        "is_lead": is_lead,
        "is_prequal_business": is_prequal_business,
        "is_commercial_property": is_commercial_property,
        "property_type_label": property_type_label,
        "category": category,
        "category_label": CATEGORY_LABELS[category],
        "primary_action": CATEGORY_PRIMARY_ACTION[category],
    }


# ============================================================
# Permissions extension point (spec #28). There is no "Commercial" role
# or sales_reps.team value defined anywhere in this codebase today
# (user_store.ROLES = Admin/Sales Rep/Customer Success/Other;
# user_store.SALES_REP_TEAMS has no Commercial entry) -- inventing one
# here would be exactly the "destructive permission rule" the spec warned
# against. /passings is login_required only; every role sees all three
# categories with manual tab switching (see templates/passings.html).
#
# When a real Commercial role/team is introduced, wire it here (e.g. add
# the team's exact sales_reps.team value(s) to this set) so app.py's
# passings_page() can default the initial category tab for those users --
# nothing else in this feature needs to change.
# ============================================================

COMMERCIAL_REP_TEAMS = set()
