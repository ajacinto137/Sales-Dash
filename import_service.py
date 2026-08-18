"""Excel user import for the Admin Portal -- validate before upload is
even possible, preview with a proposed action per row, then commit with
per-row partial-failure reporting. See templates/admin/import.html and
static/js/admin_import.js for the select -> validate -> preview -> import
-> results flow this backs.

Two entry points:
- parse_and_validate(file_storage) -- pure read + validate + rep-match,
  never writes anything. Called from POST /admin/users/import/validate,
  safe to call repeatedly as the Admin swaps files.
- commit_import(rows) -- takes the row data the Admin is committing (Rep
  Name/Email/Group triples, NOT the client's own proposed_action/
  matched_rep -- see _validate_row()) and re-validates every row from
  scratch server-side before writing anything, exactly like every other
  write path in this app never trusts client-supplied state. Row-by-row:
  one bad row is reported and skipped, it never fails the rows around it.

Requires REQUIRED_COLUMNS to be present exactly as named -- "Rep Name",
"Email", "Group" -- and every Group value to be one of
user_store.IMPORTABLE_ROLES (Admin is deliberately not an Excel-
importable role, see user_store.py)."""

import re

import pandas as pd

import user_store

REQUIRED_COLUMNS = ["Rep Name", "Email", "Group"]
MAX_ROWS = 2000

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

ACTION_CREATE = "Create User"
ACTION_UPDATE = "Update Existing User"
ACTION_NEEDS_REVIEW = "Needs Review"
ACTION_CANNOT_IMPORT = "Cannot Import"


def _clean(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _read_workbook(file_storage):
    """Returns (ok, error, dataframe). Isolates every way a spreadsheet
    can be malformed into one clear message instead of a raw traceback."""
    filename = (file_storage.filename or "").lower()
    if not filename.endswith(".xlsx"):
        return False, "Unsupported file type. Please upload a .xlsx spreadsheet.", None

    try:
        df = pd.read_excel(file_storage, engine="openpyxl", dtype=str)
    except Exception:
        return False, "This file could not be read as a spreadsheet. Save it as a standard .xlsx file and try again.", None

    if df.shape[1] == 0:
        return False, "This spreadsheet has no columns.", None

    df.columns = [str(c).strip() for c in df.columns]
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        return False, f"Missing required column(s): {', '.join(missing)}.", None

    df = df.dropna(how="all")
    if df.empty:
        return False, "This spreadsheet has no data rows.", None

    if len(df) > MAX_ROWS:
        return False, f"This spreadsheet has {len(df)} rows, which is more than the {MAX_ROWS}-row limit for a single import.", None

    return True, None, df


def _validate_row(row_number, rep_name, email, group, email_counts, existing_users_by_email):
    """One row's worth of validation -- shared by parse_and_validate()
    (preview) and commit_import() (re-validated fresh, never trusting
    whatever the client last saw). `email_counts` is {email: count in
    this file}, precomputed once per file/commit for duplicate
    detection."""
    errors = []
    rep_name = _clean(rep_name)
    email = _clean(email)
    group = _clean(group)

    if not rep_name:
        errors.append("Rep Name is required.")
    if not email:
        errors.append("Email is required.")
    elif not _EMAIL_RE.match(email):
        errors.append("Invalid email format.")
    elif email_counts.get(email.lower(), 0) > 1:
        errors.append("Duplicate email within this file.")
    if not group:
        errors.append("Group is required.")
    elif group not in user_store.IMPORTABLE_ROLES:
        errors.append(f'Group "{group}" is invalid. Must be one of: {", ".join(user_store.IMPORTABLE_ROLES)}.')

    row = {
        "row_number": row_number,
        "rep_name": rep_name,
        "email": email,
        "group": group,
        "errors": errors,
        "matched_rep": None,
        "proposed_action": ACTION_CANNOT_IMPORT,
    }

    if errors:
        return row

    # Only "Sales Rep" rows are ever matched against sales_reps -- a
    # Customer Success/Other row's Rep Name is just their name, not a
    # claim to a dashboard rep identity, so it's neither expected nor
    # flagged as needing review when it doesn't match one.
    matched_rep = user_store.find_sales_rep_by_name(rep_name) if group == "Sales Rep" else None
    row["matched_rep"] = matched_rep

    existing_user = existing_users_by_email.get(email.lower())
    if group == "Sales Rep" and matched_rep is None:
        row["proposed_action"] = ACTION_NEEDS_REVIEW
        row["review_reason"] = "No matching Sales Rep found in dashboard data -- this user will be created without a rep mapping."
    elif existing_user is not None:
        row["proposed_action"] = ACTION_UPDATE
    else:
        row["proposed_action"] = ACTION_CREATE

    return row


def _build_rows(df):
    email_counts = {}
    for _, record in df.iterrows():
        email = _clean(record.get("Email")).lower()
        if email:
            email_counts[email] = email_counts.get(email, 0) + 1

    existing_users = {u["email"].lower(): u for u in user_store.list_users()}

    rows = []
    for idx, record in df.iterrows():
        row_number = idx + 2  # header is row 1
        rows.append(_validate_row(
            row_number,
            record.get("Rep Name"),
            record.get("Email"),
            record.get("Group"),
            email_counts,
            existing_users,
        ))
    return rows


def _summarize(rows):
    summary = {"total": len(rows), ACTION_CREATE: 0, ACTION_UPDATE: 0, ACTION_NEEDS_REVIEW: 0, ACTION_CANNOT_IMPORT: 0}
    for row in rows:
        summary[row["proposed_action"]] += 1
    return summary


def parse_and_validate(file_storage):
    """Returns (ok, error, rows, summary). `ok` reflects structural
    validity of the FILE (right columns, non-empty, readable) -- a
    structurally valid file with some Cannot Import rows is still
    ok=True; those rows just won't be committed. Never writes anything."""
    ok, error, df = _read_workbook(file_storage)
    if not ok:
        return False, error, [], None

    rows = _build_rows(df)
    return True, None, rows, _summarize(rows)


def commit_import(rows):
    """Re-validates every row from scratch (rep_name/email/group only --
    any other field the client sent, like a claimed proposed_action, is
    ignored) and writes valid ones. One bad row never blocks the rows
    around it. Returns {added, updated, needs_review, failed: [...]}."""
    email_counts = {}
    for row in rows:
        email = _clean(row.get("email")).lower()
        if email:
            email_counts[email] = email_counts.get(email, 0) + 1

    existing_users = {u["email"].lower(): u for u in user_store.list_users()}

    added = 0
    updated = 0
    needs_review = 0
    failed = []

    for raw_row in rows:
        row_number = raw_row.get("row_number")
        validated = _validate_row(
            row_number,
            raw_row.get("rep_name"),
            raw_row.get("email"),
            raw_row.get("group"),
            email_counts,
            existing_users,
        )

        if validated["errors"]:
            failed.append({
                "row_number": row_number,
                "rep_name": validated["rep_name"],
                "email": validated["email"],
                "reason": " ".join(validated["errors"]),
            })
            continue

        matched_rep = validated["matched_rep"]
        sales_rep_id = matched_rep["id"] if matched_rep else None
        # "Needs review" only ever applies to a Sales Rep row that
        # couldn't be matched -- Customer Success/Other rows have no rep
        # to match in the first place (see _validate_row()), so their
        # matched_rep being None is normal, not something to flag.
        row_needs_review = validated["proposed_action"] == ACTION_NEEDS_REVIEW
        existing_user = existing_users.get(validated["email"].lower())

        if existing_user is not None:
            if matched_rep is not None:
                ok, error = user_store.update_user(
                    existing_user["id"], role=validated["group"], sales_rep_id=sales_rep_id,
                )
            else:
                ok, error = user_store.update_user(existing_user["id"], role=validated["group"])
            if ok:
                updated += 1
                if row_needs_review:
                    needs_review += 1
            else:
                failed.append({
                    "row_number": row_number, "rep_name": validated["rep_name"],
                    "email": validated["email"], "reason": error,
                })
        else:
            ok, error, _user = user_store.create_user(
                validated["email"], validated["group"], sales_rep_id=sales_rep_id,
            )
            if ok:
                added += 1
                if row_needs_review:
                    needs_review += 1
            else:
                failed.append({
                    "row_number": row_number, "rep_name": validated["rep_name"],
                    "email": validated["email"], "reason": error,
                })

    return {"added": added, "updated": updated, "needs_review": needs_review, "failed": failed}
