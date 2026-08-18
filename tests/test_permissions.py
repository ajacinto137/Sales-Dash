"""Tests for the ONE authoritative Needs Attention permission rule
(permissions.can_work_account()) -- pure function, no database or Flask
app context needed, so these run instantly and cover every rule in the
spec directly: Day 1/14/15 boundaries, Customer-Success-owned accounts,
Admin bypass, and the Other role's conservative default.

Run with: pytest tests/test_permissions.py -v
(or just `pytest` from the repo root, alongside test_import_validation.py)
"""

from datetime import datetime, timedelta, timezone

import permissions

NOW = datetime.now(timezone.utc)


def days_ago(n):
    return NOW - timedelta(days=n, minutes=1)  # +1 min margin past the exact boundary


def make_user(role, sales_rep_name=None, status="active"):
    return {
        "id": 1,
        "email": "test@planet.net",
        "role": role,
        "sales_rep_name": sales_rep_name,
        "status": status,
    }


# ---- Sales Rep, own account ----

def test_sales_rep_can_work_own_account_immediately():
    user = make_user("Sales Rep", sales_rep_name="Jack Jacinto")
    allowed, reason = permissions.can_work_account(user, "Jack Jacinto", days_ago(0))
    assert allowed is True
    assert reason is None


# ---- Sales Rep, another rep's account: Day 14 vs Day 15 ----

def test_sales_rep_cannot_work_another_reps_account_at_14_days():
    user = make_user("Sales Rep", sales_rep_name="Jack Jacinto")
    allowed, reason = permissions.can_work_account(user, "Laryssa Dasilva", days_ago(14))
    assert allowed is False
    assert "15 days" in reason


def test_sales_rep_can_work_another_reps_account_at_15_full_days():
    user = make_user("Sales Rep", sales_rep_name="Jack Jacinto")
    allowed, reason = permissions.can_work_account(user, "Laryssa Dasilva", days_ago(15))
    assert allowed is True
    assert reason is None


def test_sales_rep_cannot_work_another_reps_account_at_day_0():
    user = make_user("Sales Rep", sales_rep_name="Jack Jacinto")
    allowed, reason = permissions.can_work_account(user, "Laryssa Dasilva", days_ago(0))
    assert allowed is False


# ---- Sales Rep, Customer-Success/unattributed-owned account ----

def test_sales_rep_can_work_account_with_no_owning_sales_rep_immediately():
    user = make_user("Sales Rep", sales_rep_name="Jack Jacinto")
    allowed, reason = permissions.can_work_account(user, None, None)
    assert allowed is True
    assert reason is None


# ---- Customer Success ----

def test_customer_success_can_work_any_account_immediately():
    user = make_user("Customer Success")
    allowed, reason = permissions.can_work_account(user, "Laryssa Dasilva", days_ago(0))
    assert allowed is True
    assert reason is None


# ---- Admin ----

def test_admin_can_work_any_account_immediately():
    user = make_user("Admin")
    allowed, reason = permissions.can_work_account(user, "Laryssa Dasilva", days_ago(0))
    assert allowed is True
    assert reason is None


def test_admin_bypasses_15_day_rule_even_at_day_0():
    user = make_user("Admin")
    allowed, _reason = permissions.can_work_account(user, "Anyone Else", days_ago(0))
    assert allowed is True


# ---- Other ----

def test_other_role_cannot_work_any_account():
    user = make_user("Other")
    allowed, reason = permissions.can_work_account(user, "Jack Jacinto", days_ago(0))
    assert allowed is False
    assert reason is not None


def test_other_role_cannot_work_unowned_account_either():
    """Other must never gain Sales Rep-style permissions, including the
    "no owning Sales Rep" carve-out that applies to Sales Reps."""
    user = make_user("Other")
    allowed, _reason = permissions.can_work_account(user, None, None)
    assert allowed is False


# ---- No user / disabled ----

def test_no_user_cannot_work_any_account():
    allowed, reason = permissions.can_work_account(None, "Jack Jacinto", days_ago(0))
    assert allowed is False
    assert reason is not None


def test_disabled_user_cannot_work_any_account():
    user = make_user("Admin", status="disabled")
    allowed, _reason = permissions.can_work_account(user, "Jack Jacinto", days_ago(0))
    assert allowed is False
