"""Session-based authentication -- Flask's own signed-cookie `session`
(needs a real SECRET_KEY, see app.py) holds only a `user_id`; nothing
else is trusted from the client. Every request re-fetches the user's
current row from appdb (via user_store, cached on `flask.g` for the
duration of that one request only) rather than trusting anything cached
in the cookie itself, so a role change or a disable takes effect on the
user's very next request -- not next login.

No Flask-Login or other auth package: this app has zero framework
dependencies beyond Flask itself anywhere in its stack (no ORM, no
frontend framework), so this stays consistent with that -- a signed
session cookie plus two decorators is the whole auth system.

Password hashing lives in user_store.py (werkzeug.security, already a
Flask dependency). This module is purely about "who is making this
request and are they allowed to."""

from functools import wraps

from flask import abort, g, redirect, request, session, url_for

import user_store

SESSION_KEY = "user_id"


def login_user(user):
    session.clear()
    session[SESSION_KEY] = user["id"]
    session.permanent = True


def logout_user():
    session.clear()


def current_user():
    """The requesting user's dict (user_store row shape), or None if not
    logged in, the user no longer exists, or the account is disabled.
    Cached on flask.g so a single request only ever hits appdb once for
    this, regardless of how many places in that request ask."""
    if hasattr(g, "_current_user"):
        return g._current_user

    user_id = session.get(SESSION_KEY)
    user = user_store.get_user_by_id(user_id) if user_id else None
    if user is not None and user.get("status") != "active":
        # Disabled (or somehow still pending) -- treat as logged out
        # rather than granting a half-set-up session any access.
        user = None

    g._current_user = user
    return user


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            return redirect(url_for("login", next=request.path))
        if user.get("role") != "Admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped
