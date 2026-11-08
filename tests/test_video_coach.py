"""Sales Video Coach (video_coach.py, 2026-11-07): link parsing, transcript
paragraphs, the playbook view, Claude call guards and Ask the coach
source resolution. No network or database: Anthropic and appdb are faked."""

import json
import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import video_coach as V

USER = {"id": 7, "status": "active", "role": "Sales Rep", "email": "rep@example.com"}


@pytest.fixture(autouse=True)
def no_key(monkeypatch):
    monkeypatch.setattr(V.call_coach, "anthropic_key", lambda: "")


# --------------------------------------------------------------- links

@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=guZa7mQV1l0",
    "youtube.com/watch?v=guZa7mQV1l0&t=30s",
    "https://youtu.be/guZa7mQV1l0?si=abc",
    "https://www.youtube.com/shorts/guZa7mQV1l0",
    "https://m.youtube.com/live/guZa7mQV1l0?feature=share",
    "https://www.youtube.com/embed/guZa7mQV1l0",
    "guZa7mQV1l0",
])
def test_video_id_from_accepts_youtube_links(url):
    assert V.video_id_from(url) == "guZa7mQV1l0"


@pytest.mark.parametrize("url", [
    "", None, "https://example.com/watch?v=guZa7mQV1l0", "https://notyoutube.com/watch?v=guZa7mQV1l0",
    "https://www.youtube.com/@somechannel", "https://www.youtube.com/watch?v=short",
])
def test_video_id_from_rejects_other_links(url):
    assert V.video_id_from(url) is None


# --------------------------------------------------------- transcripts

def test_paragraphs_group_by_time_and_skip_noise():
    snippets = [
        SimpleNamespace(text="hello", start=0.0),
        SimpleNamespace(text="[Music]", start=5.0),
        SimpleNamespace(text="there\nfriend", start=12.0),
        SimpleNamespace(text="new idea", start=31.0),
        SimpleNamespace(text="more", start=40.0),
    ]
    assert V.paragraphs(snippets) == [[0, "hello there friend new idea"], [40, "more"]]


def test_pasted_paragraphs_have_no_timestamps():
    paras = V.pasted_paragraphs(" ".join(["word"] * 250))
    assert [p[0] for p in paras] == [None, None, None]
    assert sum(len(p[1].split()) for p in paras) == 250
    assert V.transcript_text(paras).startswith("word word")


def test_ts_and_clip_url():
    assert V.ts(None) == "" and V.ts(75) == "1:15" and V.ts(3723) == "1:02:03"
    assert V.clip_url("abc", 90) == "https://www.youtube.com/watch?v=abc&t=90s"
    assert V.clip_url("abc", None) == "https://www.youtube.com/watch?v=abc"


# ------------------------------------------------------------ playbook

def row(row_id, status="ready", lessons=(), video_id="vid00000001"):
    return {"id": row_id, "video_id": video_id, "title": f"Video {row_id}", "status": status,
            "notes": {"lessons": list(lessons)} if status == "ready" else None}


def lesson(topic, title, start=10, channels=("outbound",)):
    return {"topic": topic, "title": title, "how": "how", "say": ["line"], "channels": list(channels),
            "start_seconds": start, "builds_on": ""}


def test_playbook_view_groups_in_topic_order_and_skips_unready():
    rows = [  # newest first, like list_videos()
        row(2, lessons=[lesson("closing", "Close B"), lesson("openers", "Open B")]),
        row(3, status="processing"),
        row(1, lessons=[lesson("openers", "Open A", start=None, channels=("door_to_door", "bogus"))]),
    ]
    view = V.playbook_view(rows)
    assert [t for t, _label, _items in view] == ["openers", "closing"]
    openers = view[0][2]
    assert [l["title"] for l in openers] == ["Open A", "Open B"]  # oldest video first
    assert openers[0]["clip"].endswith("v=vid00000001") and openers[0]["at"] == ""
    assert openers[0]["channel_labels"] == ["Door to door"]
    assert openers[1]["at"] == "0:10"


def test_stale_processing_rows_read_as_failed():
    class Cur:
        description = [("id",), ("status",), ("error",), ("added_at",), ("learned_at",)]
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    r = V._row((1, "processing", None, old, None), Cur())
    assert r["status"] == "failed" and "Try again" in r["error"]
    fresh = V._row((1, "processing", None, datetime.now(timezone.utc), None), Cur())
    assert fresh["status"] == "processing"


# -------------------------------------------------------------- Claude

def test_call_without_key_explains_how_to_add_it():
    result, error = V._call("s", "u", V.ASK_SCHEMA, "low", 100)
    assert result is None and "Live Call Coach" in error


def test_submit_rejects_bad_link_and_missing_key():
    assert V.submit("https://example.com", "", USER)[1] == "That doesn't look like a YouTube video link."
    ok, error, _ = V.submit("https://youtu.be/guZa7mQV1l0", "", USER)
    assert not ok and "Anthropic API key" in error
    assert V.submit("https://youtu.be/guZa7mQV1l0", "", {"status": "inactive"})[0] is False


class FakeClient:
    def __init__(self, payload, stop_reason="end_turn"):
        self.payload, self.stop_reason, self.calls = payload, stop_reason, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="text", text=json.dumps(self.payload))])


def with_client(monkeypatch, client):
    """Fake anthropic module (exception classes only) + a fake client."""
    module = types.ModuleType("anthropic")
    for name in ("APIStatusError", "AuthenticationError", "RateLimitError", "APITimeoutError", "APIConnectionError"):
        setattr(module, name, type(name, (Exception,), {}))
    monkeypatch.setitem(sys.modules, "anthropic", module)
    monkeypatch.setattr(V.call_coach, "anthropic_key", lambda: "sk-test-key-123456")
    monkeypatch.setattr(V, "_client", lambda: client)
    monkeypatch.setattr(V, "_offer_json", lambda: "{}")


def test_call_sends_schema_effort_and_fallbacks(monkeypatch):
    client = FakeClient({"ok": 1})
    with_client(monkeypatch, client)
    result, error = V._call("sys", "user", {"type": "object"}, "medium", 16000)
    assert error is None and result == {"ok": 1}
    kw = client.calls[0]
    assert kw["model"] == V.MODEL and kw["fallbacks"] == "default"
    assert kw["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": {"type": "object"}}}


@pytest.mark.parametrize("stop, message", [("refusal", "declined"), ("max_tokens", "cut off")])
def test_call_reports_refusal_and_truncation(monkeypatch, stop, message):
    with_client(monkeypatch, FakeClient({}, stop_reason=stop))
    result, error = V._call("s", "u", {}, "low", 10)
    assert result is None and message in error


def test_ask_resolves_lesson_ids_to_clips(monkeypatch):
    rows = [row(1, lessons=[lesson("openers", "Bad time opener", start=310)], video_id="guZa7mQV1l0")]
    monkeypatch.setattr(V, "list_videos", lambda: (True, None, rows))
    client = FakeClient({"answer": "Use it.", "say_this": ["Is now a bad time?"], "sources": ["L1", "L99"],
                         "from_playbook": True, "next_video_idea": ""})
    with_client(monkeypatch, client)
    result, error = V.ask("how do I open a cold call?", "outbound", USER)
    assert error is None
    assert result["sources"] == [{"title": "Bad time opener", "video_title": "Video 1", "at": "5:10",
                                  "clip": "https://www.youtube.com/watch?v=guZa7mQV1l0&t=310s"}]
    prompt = client.calls[0]["messages"][0]["content"]
    assert "L1 [openers] Bad time opener" in prompt and "REP'S CHANNEL: Outbound" in prompt


def test_ask_needs_a_question():
    assert V.ask("  ", "", USER) == (None, "Type a question first.")


def test_learn_saves_notes_and_drops_unknown_channels(monkeypatch):
    saved = {}
    monkeypatch.setattr(V, "get_video", lambda rid, with_transcript=False: {
        "id": rid, "video_id": "guZa7mQV1l0", "title": "T", "channel": "C", "transcript": [[0, "hello"]]})
    monkeypatch.setattr(V, "list_videos", lambda: (True, None, [row(9, lessons=[lesson("rapport", "Smile")])]))
    monkeypatch.setattr(V, "_update", lambda rid, **f: saved.update(f))
    notes = {"summary": "s", "top_takeaways": [], "objections": [], "doesnt_fit": [], "not_sales_content": False,
             "drill": {"goal": "g", "customer_script": "c", "debrief": "d"},
             "lessons": [lesson("openers", "New", channels=("inbound", "radio"))]}
    client = FakeClient(notes)
    with_client(monkeypatch, client)
    V.learn(1)
    assert saved["status"] == "ready" and saved["notes"]["lessons"][0]["channels"] == ["inbound"]
    prompt = client.calls[0]["messages"][0]["content"]
    assert "- [rapport] Smile" in prompt and "[0] hello" in prompt


def test_learn_marks_failure(monkeypatch):
    saved = {}
    monkeypatch.setattr(V, "get_video", lambda rid, with_transcript=False: {"id": rid, "video_id": "x", "transcript": []})
    monkeypatch.setattr(V, "list_videos", lambda: (True, None, []))
    monkeypatch.setattr(V, "_update", lambda rid, **f: saved.update(f))
    V.learn(1)  # no key -> error from _call
    assert saved["status"] == "failed" and "Anthropic API key" in saved["error"]
