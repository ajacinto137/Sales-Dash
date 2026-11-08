"""Sales Library (sales_library.py, 2026-11-07): file validation, loading,
the lessons-by-topic view, and the shipped library files themselves."""

import json

import pytest

import sales_library as L


def entry(**over):
    data = {
        "id": "abcdefghijk", "source": "youtube", "url": "https://www.youtube.com/watch?v=abcdefghijk",
        "title": "Video", "creator": "Chan", "length_seconds": 90, "learned": "2026-11-07",
        "summary": "s", "takeaways": ["t"],
        "lessons": [{
            "topic": "openers", "title": "Open", "how": "h", "say": ["hi"], "channels": ["outbound", "radio"],
            "start_seconds": 30, "builds_on": "",
            "example": {"setting": "Outbound call", "lines": [{"speaker": "rep", "text": "Hi"}, {"speaker": "x", "text": "Hey"}]},
        }],
    }
    data.update(over)
    return data


def test_validate_cleans_lessons_and_examples():
    e, err = L.validate(entry(), "abcdefghijk.json")
    assert err is None
    lesson = e["lessons"][0]
    assert lesson["channels"] == ["outbound"]
    assert lesson["example"] == {"setting": "Outbound call", "lines": [
        {"speaker": "Rep", "text": "Hi"}, {"speaker": "Customer", "text": "Hey"}]}
    assert e["drill"] is None and e["source_label"] == "YouTube"


@pytest.mark.parametrize("over, filename, message", [
    ({"id": "bad id!"}, "", "bad id"),
    ({}, "other.json", "file name"),
    ({"source": "tiktok"}, "", "source"),
    ({"url": "http://x"}, "", "https"),
    ({"learned": "Nov 7"}, "", "YYYY-MM-DD"),
    ({"title": ""}, "", "title"),
])
def test_validate_rejects_bad_files(over, filename, message):
    e, err = L.validate(entry(**over), filename)
    assert e is None and message in err


def test_unknown_topic_falls_back_and_empty_example_is_none():
    e, _ = L.validate(entry(lessons=[{"topic": "nope", "title": "T", "example": {"lines": []}}]))
    assert e["lessons"][0]["topic"] == "mindset" and e["lessons"][0]["example"] is None


def test_clip_urls():
    yt = {"source": "youtube", "url": "https://www.youtube.com/watch?v=x"}
    ig = {"source": "instagram", "url": "https://www.instagram.com/reel/X/"}
    assert L.clip_url(yt, 75) == "https://www.youtube.com/watch?v=x&t=75s"
    assert L.clip_url(yt, None) == yt["url"] and L.clip_url(ig, 10) == ig["url"]
    assert L.ts(None) == "" and L.ts(3723) == "1:02:03"


def test_load_sorts_newest_first_and_reports_bad_files(tmp_path):
    (tmp_path / "abcdefghijk.json").write_text(json.dumps(entry()))
    (tmp_path / "ig-NEW12345.json").write_text(json.dumps(entry(
        id="ig-NEW12345", source="instagram", url="https://www.instagram.com/reel/NEW12345/", learned="2026-11-08")))
    (tmp_path / "broken.json").write_text("{nope")
    (tmp_path / "README.md").write_text("ignored")
    entries, errors = L.load(str(tmp_path))
    assert [e["id"] for e in entries] == ["ig-NEW12345", "abcdefghijk"]
    assert len(errors) == 1 and errors[0].startswith("broken.json")
    assert L.summary(entries) == {"videos": 2, "lessons": 2, "youtube": 1, "instagram": 1}


def test_lessons_by_topic_filters():
    a, _ = L.validate(entry())
    b, _ = L.validate(entry(id="ig-B1234567", source="instagram", url="https://www.instagram.com/reel/B1234567/",
                            lessons=[{"topic": "closing", "title": "Close", "channels": ["door_to_door"]}]))
    groups = L.lessons_by_topic([b, a])
    assert [g[0] for g in groups] == ["openers", "closing"]
    assert groups[0][2][0]["clip"].endswith("&t=30s") and groups[0][2][0]["at"] == "0:30"
    assert [g[0] for g in L.lessons_by_topic([b, a], channel="door_to_door")] == ["closing"]
    assert [g[0] for g in L.lessons_by_topic([b, a], source="youtube")] == ["openers"]
    assert L.lessons_by_topic([b, a], topic="value") == []


def test_shipped_library_files_are_valid_and_every_lesson_has_an_example():
    entries, errors = L.load()
    assert errors == []
    assert entries, "the library ships with at least one lesson file"
    for e in entries:
        for lesson in e["lessons"]:
            assert lesson["example"] and lesson["example"]["lines"], f"{e['id']}: {lesson['title']} has no example"
