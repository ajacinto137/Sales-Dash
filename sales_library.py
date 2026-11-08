"""Sales Library (Tools menu, added 2026-11-07) -- read-only page of the
sales lessons the team has learned from training videos.

The lessons are written OUTSIDE the dashboard, by the user's Claude Code
skills (sales-video-learner for YouTube, sales-instagram-learner for
Instagram). Each run drops one JSON file per video in sales_library/ and
pushes to main, which deploys it. No Claude/API calls happen here: this
module only reads and validates those files.

File format (sales_library/<id>.json): sales_library/README.md. Every
lesson carries a Planet Networks "example" -- a short realistic scene
(setting + Rep/Customer lines) showing the technique used selling our
fiber; the publishing script refuses lessons without one."""

import json
import os
import re
import threading

LIBRARY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sales_library")

TOPICS = [
    ("openers", "Openers & first 10 seconds"),
    ("rapport", "Rapport & tone"),
    ("discovery", "Discovery questions"),
    ("value", "Presenting the value"),
    ("objections", "Objection handling"),
    ("closing", "Closing & commitment"),
    ("cancellations", "Preventing cancellations"),
    ("follow_up", "Follow-up & call-backs"),
    ("mindset", "Mindset & habits"),
]
TOPIC_LABELS = dict(TOPICS)
CHANNELS = {"inbound": "Inbound", "outbound": "Outbound", "door_to_door": "Door to door"}
SOURCES = {"youtube": "YouTube", "instagram": "Instagram"}

ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,60}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_cache = {"key": None, "data": None}
_lock = threading.Lock()


def ts(seconds):
    if seconds is None:
        return ""
    s = int(seconds)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def clip_url(entry, seconds):
    """YouTube links jump to the moment; Instagram links can't."""
    url = entry["url"]
    if entry["source"] == "youtube" and seconds is not None:
        return url + ("&" if "?" in url else "?") + f"t={int(seconds)}s"
    return url


def _str(value, limit=4000):
    return value.strip()[:limit] if isinstance(value, str) else ""


def _strs(value, limit=2000):
    return [_str(v, limit) for v in value if _str(v, limit)] if isinstance(value, list) else []


def _secs(value):
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def validate(raw, filename=""):
    """Returns (entry, error). Unknown topics/channels are dropped, not fatal."""
    if not isinstance(raw, dict):
        return None, "not a JSON object"
    vid = raw.get("id")
    if not isinstance(vid, str) or not ID_RE.match(vid):
        return None, "missing or bad id"
    if filename and filename != f"{vid}.json":
        return None, f"file name must be {vid}.json"
    source = raw.get("source")
    if source not in SOURCES:
        return None, "source must be youtube or instagram"
    url = _str(raw.get("url"), 500)
    if not url.startswith("https://"):
        return None, "url must be an https link"
    learned = _str(raw.get("learned"), 10)
    if not DATE_RE.match(learned):
        return None, "learned must be YYYY-MM-DD"
    title = _str(raw.get("title"), 300)
    if not title:
        return None, "missing title"

    lessons = []
    for item in raw.get("lessons") or []:
        if not isinstance(item, dict) or not _str(item.get("title")):
            continue
        topic = item.get("topic") if item.get("topic") in TOPIC_LABELS else "mindset"
        example = item.get("example") if isinstance(item.get("example"), dict) else {}
        lines = [
            {"speaker": "Rep" if str(line.get("speaker", "")).lower() == "rep" else "Customer",
             "text": _str(line.get("text"), 600)}
            for line in example.get("lines") or [] if isinstance(line, dict) and _str(line.get("text"))
        ]
        lessons.append({
            "example": {"setting": _str(example.get("setting"), 600), "lines": lines} if lines else None,
            "topic": topic,
            "title": _str(item.get("title"), 300),
            "how": _str(item.get("how")),
            "say": _strs(item.get("say")),
            "channels": [c for c in _strs(item.get("channels"), 30) if c in CHANNELS],
            "start_seconds": _secs(item.get("start_seconds")),
            "builds_on": _str(item.get("builds_on"), 300),
        })
    objections = [
        {"objection": _str(o.get("objection"), 300), "response": _str(o.get("response")),
         "start_seconds": _secs(o.get("start_seconds"))}
        for o in raw.get("objections") or [] if isinstance(o, dict) and _str(o.get("objection"))
    ]
    doesnt_fit = [
        {"point": _str(d.get("point"), 300), "why": _str(d.get("why"))}
        for d in raw.get("doesnt_fit") or [] if isinstance(d, dict) and _str(d.get("point"))
    ]
    drill = raw.get("drill") if isinstance(raw.get("drill"), dict) else {}
    drill = {k: _str(drill.get(k)) for k in ("goal", "customer_script", "debrief")}
    applies = raw.get("applies") if isinstance(raw.get("applies"), dict) else {}
    applies = {k: _str(applies.get(k)) for k in CHANNELS if _str(applies.get(k))}

    return {
        "id": vid,
        "source": source,
        "source_label": SOURCES[source],
        "url": url,
        "title": title,
        "creator": _str(raw.get("creator"), 200),
        "length_seconds": _secs(raw.get("length_seconds")),
        "learned": learned,
        "added_by": _str(raw.get("added_by"), 100),
        "summary": _str(raw.get("summary")),
        "takeaways": _strs(raw.get("takeaways")),
        "lessons": lessons,
        "objections": objections,
        "doesnt_fit": doesnt_fit,
        "applies": applies,
        "drill": drill if any(drill.values()) else None,
    }, None


def load(directory=LIBRARY_DIR):
    """(entries newest first, errors). Re-read whenever a file changes."""
    try:
        names = sorted(n for n in os.listdir(directory) if n.endswith(".json"))
    except FileNotFoundError:
        return [], []
    key = (directory, tuple((n, os.path.getmtime(os.path.join(directory, n))) for n in names))
    with _lock:
        if _cache["key"] == key:
            return _cache["data"]
        entries, errors = [], []
        for name in names:
            try:
                with open(os.path.join(directory, name), encoding="utf-8") as fh:
                    raw = json.load(fh)
            except (OSError, ValueError) as exc:
                errors.append(f"{name}: {exc}")
                continue
            entry, error = validate(raw, name)
            if error:
                errors.append(f"{name}: {error}")
            else:
                entries.append(entry)
        entries.sort(key=lambda e: (e["learned"], e["id"]), reverse=True)
        _cache.update(key=key, data=(entries, errors))
        return entries, errors


def get(entry_id, directory=LIBRARY_DIR):
    entries, _errors = load(directory)
    return next((e for e in entries if e["id"] == entry_id), None)


def lessons_by_topic(entries, topic=None, channel=None, source=None):
    """[(topic, label, [lesson + source fields])] in TOPICS order, oldest video first."""
    grouped = {t: [] for t, _ in TOPICS}
    for entry in reversed(entries):
        if source and entry["source"] != source:
            continue
        for lesson in entry["lessons"]:
            if topic and lesson["topic"] != topic:
                continue
            if channel and channel not in lesson["channels"]:
                continue
            item = dict(lesson)
            item.update(
                entry_id=entry["id"],
                entry_title=entry["title"],
                creator=entry["creator"],
                source_label=entry["source_label"],
                clip=clip_url(entry, lesson["start_seconds"]),
                at=ts(lesson["start_seconds"]),
                channel_labels=[CHANNELS[c] for c in lesson["channels"]],
            )
            grouped[lesson["topic"]].append(item)
    return [(t, TOPIC_LABELS[t], grouped[t]) for t, _ in TOPICS if grouped[t]]


def summary(entries):
    return {
        "videos": len(entries),
        "lessons": sum(len(e["lessons"]) for e in entries),
        "youtube": sum(1 for e in entries if e["source"] == "youtube"),
        "instagram": sum(1 for e in entries if e["source"] == "instagram"),
    }
