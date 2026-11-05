"""login_required must send a logged-out visitor back to the full link,
query string included.

Run with: pytest tests/test_login_return_path.py -v
"""

from unittest import mock
from urllib.parse import parse_qs, urlparse

from flask import Flask

import auth


def _client():
    flask_app = Flask(__name__)
    flask_app.add_url_rule("/login", "login", lambda: "login")
    flask_app.add_url_rule("/page", "page", auth.login_required(lambda: "page"))
    return flask_app.test_client()


def _next_for(url):
    with mock.patch.object(auth, "current_user", return_value=None):
        response = _client().get(url)
    assert response.status_code == 302
    return parse_qs(urlparse(response.location).query)["next"][0]


def test_query_string_survives_login():
    assert _next_for("/page?town=O%27Brien&state=NJ") == "/page?town=O%27Brien&state=NJ"


def test_plain_path_has_no_trailing_question_mark():
    assert _next_for("/page") == "/page"
