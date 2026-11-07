"""Live Call Coach (call_coach.py, 2026-11-07): playbook cards, the
Claude card parsing/guards, Deepgram token handling and the extension
download. No network: Anthropic and Deepgram are faked."""

import io
import json
import sys
import types
import urllib.error
import zipfile
from unittest import mock

import pytest

import call_coach as C


@pytest.fixture(autouse=True)
def no_saved_secrets(monkeypatch):
    monkeypatch.setattr(C.app_secrets, "get_secret", lambda name: {"value": None})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.delenv("COACH_MODEL", raising=False)


def playbook(**offer):
    data = json.loads(json.dumps(C.load_playbook()))
    data["offer"].update(offer)
    return data


# ---------------------------------------------------------- playbook

def test_shipped_playbook_is_valid():
    pb = C.load_playbook()
    ids = [o["id"] for o in pb["objections"]]
    assert len(ids) == len(set(ids))
    for o in pb["objections"]:
        assert o["listen_for"] and o["say"] and o["label"]
    assert pb["consent_line"]


def test_price_objection_matches_and_blank_offer_is_never_invented():
    cards = C.match("It looks great but honestly it's too expensive for us right now.")
    assert [c["id"] for c in cards] == ["price"]
    say = cards[0]["say"]
    assert "[fill in: value line]" in say
    assert "[fill in: your cheapest plan and price]" in say
    assert "{" not in say


def test_offer_values_are_filled_in():
    pb = playbook(value_line="Most families save $20 a month.", cheapest_plan="the 300 Mbps plan is $49.95 a month")
    say = C.match("that's too much money", pb)[0]["say"]
    assert "Most families save $20 a month." in say
    assert "the 300 Mbps plan is $49.95 a month" in say
    assert "fill in" not in say


def test_several_objections_come_back_in_the_order_they_were_said():
    cards = C.match("I'm locked in with Optimum, and it's too expensive anyway")
    assert [c["id"] for c in cards] == ["under_contract", "price"]


def test_phrases_match_whole_words_only():
    # "pricey" must not fire inside "pricing", "too much" not inside "too muchness"
    assert C.match("what does the pricing page say") == []


def test_buying_signal_card():
    cards = C.match("Okay, how soon could you get someone out here?")
    assert cards[-1]["kind"] == "buying_signal"


def test_curly_apostrophes_still_match():
    # Speech-to-text often returns ’ instead of '; the playbook uses '.
    assert [c["id"] for c in C.match("Thanks, but we’re good with what we have")] == ["happy_with_current"]
    assert [c["id"] for c in C.match("I can’t afford it")] == ["price"]


def test_empty_line_has_no_cards():
    assert C.match("   ") == []


# ---------------------------------------------------------- transcript

def test_clean_transcript_trims_and_caps():
    lines = [{"speaker": "rep", "text": "hi"}, {"speaker": "x", "text": "  hello \n there "}, {"speaker": "rep", "text": ""}, "junk"]
    assert C.clean_transcript(lines) == [("Rep", "hi"), ("Customer", "hello there")]
    many = [{"speaker": "customer", "text": str(i)} for i in range(100)]
    out = C.clean_transcript(many)
    assert len(out) == C.MAX_TRANSCRIPT_LINES and out[-1] == ("Customer", "99")


def test_system_prompt_is_stable_for_caching():
    pb = C.load_playbook()
    assert C.system_prompt(pb) == C.system_prompt(pb)
    assert "only facts you may state" in C.system_prompt(pb)


# ------------------------------------------------------------- Claude

def _response(text, stop_reason="end_turn"):
    return types.SimpleNamespace(stop_reason=stop_reason, content=[types.SimpleNamespace(type="text", text=text)])


def test_parse_card_accepts_a_valid_card():
    card = C.parse_card(_response(json.dumps({"kind": "objection", "label": "Price", "say": "What do you pay now?", "why": "anchor"})))
    assert card == {"kind": "objection", "id": "ai", "label": "Price", "say": "What do you pay now?", "why": "anchor", "source": "ai"}


@pytest.mark.parametrize("text,stop", [
    (json.dumps({"kind": "none", "label": "", "say": "", "why": ""}), "end_turn"),
    ("not json", "end_turn"),
    (json.dumps({"kind": "objection", "label": "x", "say": "  ", "why": ""}), "end_turn"),
    (json.dumps({"kind": "rant", "label": "x", "say": "y", "why": ""}), "end_turn"),
    (json.dumps({"kind": "objection", "label": "x", "say": "y", "why": ""}), "refusal"),
])
def test_parse_card_drops_nothing_cards_junk_and_refusals(text, stop):
    assert C.parse_card(_response(text, stop)) is None


class _FakeAnthropic:
    class APIStatusError(Exception):
        def __init__(self, status_code=500, message="boom"):
            self.status_code, self.message = status_code, message

    class AuthenticationError(APIStatusError):
        pass

    class RateLimitError(APIStatusError):
        pass

    class APIConnectionError(Exception):
        pass

    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []
        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                if outer.error:
                    raise outer.error
                return outer.reply

        self.beta = types.SimpleNamespace(messages=_Messages())


@pytest.fixture
def fake_anthropic(monkeypatch):
    fake = _FakeAnthropic(reply=_response(json.dumps({"kind": "objection", "label": "Price", "say": "What are you paying now?", "why": "anchor"})))
    module = types.ModuleType("anthropic")
    for name in ("APIStatusError", "AuthenticationError", "RateLimitError", "APIConnectionError"):
        setattr(module, name, getattr(_FakeAnthropic, name))
    monkeypatch.setitem(sys.modules, "anthropic", module)
    monkeypatch.setattr(C, "_client", lambda: fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-123456")
    return fake


def test_suggest_without_key_says_so():
    card, error = C.suggest([{"speaker": "customer", "text": "too expensive"}])
    assert card is None and "Anthropic API key" in error


def test_suggest_sends_playbook_and_transcript(fake_anthropic):
    card, error = C.suggest(
        [{"speaker": "rep", "text": "So that's the plan."}, {"speaker": "customer", "text": "It's too expensive."}],
        context="On Optimum, pays $90",
    )
    assert error is None and card["say"] == "What are you paying now?"
    call = fake_anthropic.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["effort"] == "low"
    assert call["output_config"]["format"]["schema"] == C.CARD_SCHEMA
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    user = call["messages"][0]["content"]
    assert "Customer: It's too expensive." in user and "On Optimum, pays $90" in user
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_suggest_model_can_be_overridden(fake_anthropic, monkeypatch):
    monkeypatch.setenv("COACH_MODEL", "claude-haiku-5-5")
    C.suggest([{"speaker": "customer", "text": "hmm"}])
    assert fake_anthropic.calls[0]["model"] == "claude-haiku-5-5"


def test_suggest_skips_when_the_rep_spoke_last(fake_anthropic):
    assert C.suggest([{"speaker": "customer", "text": "hi"}, {"speaker": "rep", "text": "hello"}]) == (None, None)
    assert fake_anthropic.calls == []


def test_suggest_reports_api_errors_without_the_key(fake_anthropic):
    fake_anthropic.error = _FakeAnthropic.APIStatusError(400, "bad key sk-ant-test-key-123456")
    card, error = C.suggest([{"speaker": "customer", "text": "too expensive"}])
    assert card is None and "sk-ant-test-key-123456" not in error and "400" in error


# ----------------------------------------------------------- Deepgram

def test_deepgram_token_needs_a_key():
    assert C.deepgram_token()[2].startswith("No Deepgram API key")


def test_deepgram_token_success(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-secret-key-abcdef")
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        seen["auth"] = req.get_header("Authorization")
        seen["body"] = json.loads(req.data)
        return Resp(json.dumps({"access_token": "jwt-abc", "expires_in": 60}).encode())

    monkeypatch.setattr(C.urllib.request, "urlopen", fake_urlopen)
    assert C.deepgram_token() == ("jwt-abc", 60, None)
    assert seen == {"auth": "Token dg-secret-key-abcdef", "body": {"ttl_seconds": C.DEEPGRAM_TOKEN_TTL}}


def test_deepgram_token_http_error(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-secret-key-abcdef")

    def boom(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(C.urllib.request, "urlopen", boom)
    token, _, error = C.deepgram_token()
    assert token is None and "401" in error


def test_admin_saved_key_wins_over_env(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "from-env-key-123456")
    monkeypatch.setattr(C.app_secrets, "get_secret", lambda name: {"value": "from-admin-key-123456" if name == C.DEEPGRAM_SECRET else None})
    assert C.deepgram_key() == "from-admin-key-123456"
    assert C.key_source(C.DEEPGRAM_SECRET, "DEEPGRAM_API_KEY") == "admin"
    assert C.key_hint("from-admin-key-123456") == "…3456"


# ---------------------------------------------------------- extension

def test_extension_zip_points_at_this_server():
    data = C.extension_zip("https://dash.example.com/")
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = set(zf.namelist())
    for required in ("manifest.json", "background.js", "offscreen.js", "sidepanel.js", "pcm-worklet.js", "mic.html", "icons/icon128.png"):
        assert "call-coach-extension/" + required in names
    manifest = json.loads(zf.read("call-coach-extension/manifest.json"))
    assert manifest["host_permissions"] == ["https://dash.example.com/*"]
    assert manifest["manifest_version"] == 3
    assert 'SERVER_URL = "https://dash.example.com"' in zf.read("call-coach-extension/config.js").decode()
    assert all(C.ORIGIN_PLACEHOLDER.encode() not in zf.read(n) for n in names)
