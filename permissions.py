"""The ONE authoritative place that decides whether a user may work a
Needs Attention account. Both the write routes in app.py (server-side
enforcement -- the only kind that counts) and the template/JS (to
proactively disable a Classify/Add Note button, purely for UX) call
can_work_account() -- there is exactly one implementation of the 15-day
rule in this codebase, never a second copy that could drift out of sync.

Pure functions, no I/O -- `needs_attention_since` is looked up by the
caller (needs_attention_service.get_needs_attention_since()) and passed
in, which is also what makes this module trivially unit-testable without
a database or Flask app context (see tests/test_permissions.py)."""

from needs_attention_service import AGE_THRESHOLD_DAYS, days_since

ROLE_ADMIN = "Admin"
ROLE_SALES_REP = "Sales Rep"
ROLE_CUSTOMER_SUCCESS = "Customer Success"
ROLE_OTHER = "Other"


def is_admin(user):
    return bool(user) and user.get("role") == ROLE_ADMIN


def can_access_admin_portal(user):
    return is_admin(user)


def can_work_account(user, owner_rep_name, needs_attention_since):
    """Returns (allowed: bool, reason: str | None). `reason` is always a
    polished, specific explanation when allowed=False (spec #25) -- never
    a bare "Forbidden".

    Rules, in order:
    1. No user / disabled user -> never.
    2. Admin -> always.
    3. Customer Success -> always (any account, any age).
    4. Sales Rep:
       a. account has no owning Sales Rep (owner is Customer Success/
          Other/unattributed) -> always.
       b. account's owner IS this user's own mapped rep -> always.
       c. a DIFFERENT Sales Rep owns it -> only once
          needs_attention_since is >= AGE_THRESHOLD_DAYS full days old.
    5. Other -> never (conservative default, spec #14)."""
    if not user or user.get("status") != "active":
        return False, "You must be logged in with an active account to do this."

    role = user.get("role")

    if role == ROLE_ADMIN:
        return True, None

    if role == ROLE_CUSTOMER_SUCCESS:
        return True, None

    if role == ROLE_SALES_REP:
        user_rep_name = user.get("sales_rep_name")

        if not owner_rep_name:
            # No owning Sales Rep on record for this account (e.g. sold
            # under a Customer Success/Other-mapped name) -- open to any
            # Sales Rep immediately.
            return True, None

        if owner_rep_name == user_rep_name:
            return True, None

        age_days = days_since(needs_attention_since)
        if age_days >= AGE_THRESHOLD_DAYS:
            return True, None

        remaining = max(0, AGE_THRESHOLD_DAYS - int(age_days))
        return False, (
            f"This account belongs to another Sales Rep and has only been in Needs "
            f"Attention for {int(age_days)} day{'s' if int(age_days) != 1 else ''}. "
            f"It becomes available to other Sales Reps after {AGE_THRESHOLD_DAYS} days "
            f"({remaining} day{'s' if remaining != 1 else ''} from now)."
        )

    # Other, or any unrecognized role -- conservative default.
    return False, "Your account does not have permission to work Needs Attention accounts."
