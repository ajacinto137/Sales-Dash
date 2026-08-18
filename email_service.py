"""Transactional email -- first-time account setup, resend-setup, and
password reset. Plain smtplib (zero new dependency) rather than a third-
party email API, configured via the same `.env` pattern as every other
credential in this app (see db.py's PLANETWEB_*/KPI_*/APPDB_* vars).

Deliberately isolated behind three narrow functions
(send_setup_email/send_reset_email + the shared _send()) so swapping the
transport later (a provider API, a queue) never touches auth.py,
user_store.py, or any route -- they only ever call these three names.

Never sends a password -- every email is a link containing a single-use
token (see user_store.create_token()); the recipient sets their own
password after clicking through.

Graceful by design, not by accident: if SMTP isn't configured (no
SMTP_HOST in .env -- the state of a fresh deployment before anyone's
filled that in) or sending fails for any reason, every function here
returns (False, error) AND logs the actual setup/reset link to stdout
(`docker compose logs`) instead of raising. This is what makes the
avelino@planet.net bootstrap (see app.py) work before email is live --
the very first Admin can always get in by reading the container logs,
never by editing the database or `.env` by hand."""

import os
import smtplib
from email.message import EmailMessage

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM_ADDRESS = os.environ.get("SMTP_FROM_ADDRESS", "no-reply@planet.net")
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"

# Used to build absolute links in emails -- e.g. https://sales.planet.net
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:3005").rstrip("/")


def is_configured():
    return bool(SMTP_HOST)


def _send(to_address, subject, body, fallback_context):
    if not is_configured():
        print("=" * 60)
        print(f"EMAIL NOT SENT (SMTP not configured) -- {fallback_context}")
        print(f"To: {to_address}")
        print(f"Subject: {subject}")
        print(body)
        print("=" * 60)
        return False, "Email is not configured yet -- see the link above in the server logs."

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = SMTP_FROM_ADDRESS
    message["To"] = to_address
    message.set_content(body)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
            if SMTP_USE_TLS:
                smtp.starttls()
            if SMTP_USERNAME:
                smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(message)
        return True, None
    except Exception as exc:
        print("=" * 60)
        print(f"EMAIL SEND FAILED -- {fallback_context}")
        print(f"To: {to_address}")
        print(f"Error: {exc}")
        print(body)
        print("=" * 60)
        return False, "Could not send the email right now -- see the link above in the server logs."


def send_setup_email(user, raw_token):
    link = f"{APP_BASE_URL}/setup/{raw_token}"
    subject = "Set up your Planet Networks Sales Dashboard account"
    body = (
        f"Hi,\n\n"
        f"An Admin has set up a Sales Dashboard account for you "
        f"({user['email']}). Click the link below to create your password "
        f"and log in. This link expires in 72 hours and can only be used once.\n\n"
        f"{link}\n\n"
        f"If you weren't expecting this, you can ignore this email.\n"
    )
    return _send(user["email"], subject, body, f"first-time setup for user #{user['id']}")


def send_reset_email(user, raw_token):
    link = f"{APP_BASE_URL}/reset-password/{raw_token}"
    subject = "Reset your Planet Networks Sales Dashboard password"
    body = (
        f"Hi,\n\n"
        f"A password reset was requested for your Sales Dashboard account "
        f"({user['email']}). Click the link below to choose a new password. "
        f"This link expires in 24 hours and can only be used once.\n\n"
        f"{link}\n\n"
        f"If you didn't request this, you can ignore this email -- your "
        f"password will not be changed.\n"
    )
    return _send(user["email"], subject, body, f"password reset for user #{user['id']}")
