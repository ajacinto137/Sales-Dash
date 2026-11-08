"""Sales Video Coach (Tools menu, added 2026-11-07).

Anyone on the team pastes a YouTube link. The server pulls the video's
captions (youtube-transcript-api, no YouTube key), Claude turns them into
sales lessons written for how we sell (inbound calls, outbound calls, door
to door), and the lessons join one shared team playbook. "Ask the coach"
answers a rep's question from that playbook, citing the video moment.

Storage: appdb table sales_videos (db_migrations.py migration 23); one row
per video, the lessons live in its notes JSON. Learning runs in a
background thread; the page polls the row's status.

Keys: same Anthropic key as Tools > Live Call Coach (call_coach.py).
Truth rule: offer facts (plans, prices, install, contract) come only from
call_coach_playbook.json's "offer" block; anything missing is written as
"[fill in: ...]". Same rule as the live call coach.

Ported from the Claude Code skill in team-skills/sales-video-learner/."""

import json
import logging
import re
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import call_coach
import db
import db_migrations
import permissions

log = logging.getLogger("video_coach")

MODEL = call_coach.DEFAULT_MODEL
MAX_WORDS = 60000            # ~4 hours of speech
STALE_AFTER = timedelta(minutes=15)
PARAGRAPH_SECONDS = 30
MAX_QUESTION = 1000
MAX_PASTE_CHARS = 400000

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
CHANNELS = ("inbound", "outbound", "door_to_door")
CHANNEL_LABELS = {"inbound": "Inbound", "outbound": "Outbound", "door_to_door": "Door to door"}

ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


# ---------------------------------------------------------------- db

def _connect():
    conn = db.get_appdb_connection()
    db_migrations.ensure_schema(conn)
    return conn


_COLUMNS = """id, video_id, title, channel, length_seconds, captions, status, error,
    notes, added_by_user_id, added_by_name, added_at, learned_at"""


def _row(cur_row, cur):
    names = [d[0] for d in cur.description]
    row = dict(zip(names, cur_row))
    if row.get("status") == "processing" and row.get("added_at"):
        started = row.get("learned_at") or row["added_at"]
        if datetime.now(timezone.utc) - started > STALE_AFTER:
            row["status"], row["error"] = "failed", "Learning stopped before it finished. Try again."
    return row


def list_videos():
    """(ok, error, rows) newest first, without transcripts."""
    conn = None
    try:
        conn = _connect()
        with conn, conn.cursor() as cur:
            cur.execute("SELECT " + _COLUMNS + " FROM sales_videos ORDER BY added_at DESC")
            return True, None, [_row(r, cur) for r in cur.fetchall()]
    except Exception as exc:
        return False, db.sanitize_error(exc), []
    finally:
        if conn is not None:
            conn.close()


def get_video(row_id, with_transcript=False):
    conn = None
    try:
        conn = _connect()
        with conn, conn.cursor() as cur:
            cols = _COLUMNS + (", transcript" if with_transcript else "")
            cur.execute("SELECT " + cols + " FROM sales_videos WHERE id = %s", (row_id,))
            r = cur.fetchone()
            return _row(r, cur) if r else None
    finally:
        if conn is not None:
            conn.close()


def _update(row_id, **fields):
    sets = ", ".join(f"{k} = %s" for k in fields)
    values = [json.dumps(v) if k in ("notes", "transcript") and v is not None else v for k, v in fields.items()]
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(f"UPDATE sales_videos SET {sets} WHERE id = %s", values + [row_id])
    finally:
        conn.close()


def delete_video(row_id, user):
    """Whoever added it, or an Admin. Returns (ok, error)."""
    row = get_video(row_id)
    if row is None:
        return False, "That video isn't in the playbook."
    if not permissions.is_admin(user) and row.get("added_by_user_id") != user.get("id"):
        return False, "Only the person who added it, or an Admin, can remove it."
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM sales_videos WHERE id = %s", (row_id,))
        return True, None
    finally:
        conn.close()


# --------------------------------------------------------- transcripts

def video_id_from(url_or_id):
    s = (url_or_id or "").strip()
    if ID_RE.match(s):
        return s
    parsed = urllib.parse.urlparse(s if "://" in s else "https://" + s)
    host = (parsed.hostname or "").lower()
    cand = ""
    if host.endswith("youtu.be"):
        cand = parsed.path.lstrip("/").split("/")[0]
    elif host == "youtube.com" or host.endswith(".youtube.com"):
        qs = urllib.parse.parse_qs(parsed.query)
        if "v" in qs:
            cand = qs["v"][0]
        else:
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) >= 2 and parts[0] in ("shorts", "live", "embed", "v"):
                cand = parts[1]
    return cand if ID_RE.match(cand) else None


def oembed(video_id):
    url = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(
        f"https://www.youtube.com/watch?v={video_id}", safe="")
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return json.load(r)
    except Exception:
        return {}


def paragraphs(snippets, chunk=PARAGRAPH_SECONDS):
    """Caption snippets (objects or dicts with text/start) -> [[start, text], ...]."""
    out, cur, start = [], [], None
    for sn in snippets:
        text = (getattr(sn, "text", None) if not isinstance(sn, dict) else sn.get("text")) or ""
        at = getattr(sn, "start", None) if not isinstance(sn, dict) else sn.get("start")
        text = text.replace("\n", " ").strip()
        if not text or text in ("[Music]", "[Applause]"):
            continue
        if start is None:
            start = float(at or 0)
        cur.append(text)
        if float(at or 0) - start >= chunk:
            out.append([int(start), " ".join(cur)])
            cur, start = [], None
    if cur:
        out.append([int(start), " ".join(cur)])
    return out


def pasted_paragraphs(text):
    """A transcript pasted by hand -> paragraphs with no timestamps."""
    text = (text or "").strip()[:MAX_PASTE_CHARS]
    words = text.split()
    return [[None, " ".join(words[i:i + 120])] for i in range(0, len(words), 120)]


def fetch_transcript(video_id):
    """Returns (paragraphs, length_seconds, captions_label, error)."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return None, None, None, "The youtube-transcript-api package isn't installed on the server."
    try:
        tlist = YouTubeTranscriptApi().list(video_id)
        kind = "manual"
        try:
            t = tlist.find_manually_created_transcript(["en"])
        except Exception:
            try:
                t = tlist.find_generated_transcript(["en"])
                kind = "auto-generated"
            except Exception:
                t = next(iter(tlist))
                kind = ("auto-generated" if t.is_generated else "manual") + f" ({t.language_code})"
                if t.is_translatable and t.language_code != "en":
                    t = t.translate("en")
                    kind += " translated to en"
        snippets = list(t.fetch())
    except Exception as exc:
        name = type(exc).__name__
        if name in ("TranscriptsDisabled", "NoTranscriptFound"):
            return None, None, None, "This video has no captions. Paste the transcript instead."
        if name in ("RequestBlocked", "IpBlocked"):
            return None, None, None, "YouTube blocked the server from reading captions. Paste the transcript instead."
        if name == "VideoUnavailable":
            return None, None, None, "That video is private, removed or not available."
        return None, None, None, f"Couldn't read the captions ({name}). Paste the transcript instead."
    if not snippets:
        return None, None, None, "The captions came back empty."
    last = snippets[-1]
    return paragraphs(snippets), int(last.start + last.duration), kind, None


def ts(seconds):
    if seconds is None:
        return ""
    s = int(seconds)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def clip_url(video_id, seconds):
    base = f"https://www.youtube.com/watch?v={video_id}"
    return base + (f"&t={int(seconds)}s" if seconds is not None else "")


# ------------------------------------------------------------- Claude

LESSON_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string", "enum": [t for t, _ in TOPICS]},
        "title": {"type": "string"},
        "how": {"type": "string"},
        "say": {"type": "array", "items": {"type": "string"}},
        "channels": {"type": "array", "items": {"type": "string", "enum": list(CHANNELS)}},
        "start_seconds": {"type": ["integer", "null"]},
        "builds_on": {"type": "string"},
    },
    "required": ["topic", "title", "how", "say", "channels", "start_seconds", "builds_on"],
    "additionalProperties": False,
}

NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "top_takeaways": {"type": "array", "items": {"type": "string"}},
        "lessons": {"type": "array", "items": LESSON_SCHEMA},
        "objections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "objection": {"type": "string"},
                    "response": {"type": "string"},
                    "start_seconds": {"type": ["integer", "null"]},
                },
                "required": ["objection", "response", "start_seconds"],
                "additionalProperties": False,
            },
        },
        "doesnt_fit": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"point": {"type": "string"}, "why": {"type": "string"}},
                "required": ["point", "why"],
                "additionalProperties": False,
            },
        },
        "drill": {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "customer_script": {"type": "string"},
                "debrief": {"type": "string"},
            },
            "required": ["goal", "customer_script", "debrief"],
            "additionalProperties": False,
        },
        "not_sales_content": {"type": "boolean"},
    },
    "required": ["summary", "top_takeaways", "lessons", "objections", "doesnt_fit", "drill", "not_sales_content"],
    "additionalProperties": False,
}

TEAM_CONTEXT = """WHO WE ARE
- Planet Networks: residential fiber-to-the-home internet in New Jersey, New York, Pennsylvania and Virginia.
- Customers are homeowners and renters in towns where our fiber was just built; most already have cable or DSL from a big incumbent.

HOW WE SELL
- inbound: the customer calls or orders on the website; qualify, answer questions, close and book the install on the call.
- outbound: reps call leads from HubSpot in newly released towns; cold or lukewarm, short attention; get the order or a booked call-back.
- door_to_door: knockers work neighborhoods, often in team blitzes; a few seconds to earn attention; get the order at the door.

WHAT A WIN IS
- An order with an install booked. A sale only counts once it is INSTALLED, so cancellations before install hurt: real commitment and honest expectations matter as much as the close."""

LEARN_SYSTEM = """You turn sales training videos into a shared playbook for a sales team. You read a video's timestamped transcript and pull out what the team can actually use.

{context}

OFFER FACTS (the only plan/price/install/contract facts you may state; anything not here must be written as "[fill in: what's needed]"):
{offer}

How to write the notes:
- summary: 3-5 sentences on the video's big idea.
- top_takeaways: the 3 most useful things for our reps, one sentence each.
- lessons: each distinct technique. "how" says how it works and when to use it (2-4 sentences). "say" holds 1-3 lines a rep could say out loud, REWRITTEN for selling residential fiber on the phone or at the door, never the speaker's B2B or other-industry wording. Quote the speaker only in short phrases (under 15 words). "channels" lists only the channels where it really fits. "start_seconds" is where the video teaches it (the [seconds] marker of that paragraph), or null if the transcript has no timestamps.
- Dedup: the EXISTING PLAYBOOK below lists lessons the team already has. Do not repeat one. If this video adds a genuinely new angle or example to an existing lesson, include it and put that existing lesson's exact title in "builds_on"; otherwise "builds_on" is "".
- objections: objections the video handles, with the response adapted to us.
- doesnt_fit: advice that is B2B-only, high-ticket, manipulative, or conflicts with how we sell, and why.
- drill: one 5-minute pairs role-play built on the best lesson: the rep's goal, what the "customer" says and does, and what to debrief.
- Never invent urgency, discounts, deadlines or results. Never teach lying or pressure that would cause cancellations.
- If the video has no usable sales or persuasion content, set not_sales_content true and keep the lists empty."""

ASK_SYSTEM = """You are the sales coach for the Planet Networks sales team. Reps ask you what to do or say; you answer from the team playbook, which was built from training videos the team chose.

{context}

OFFER FACTS (the only plan/price/install/contract facts you may state; anything missing is "[fill in: what's needed]"):
{offer}

How to answer:
- Answer from the PLAYBOOK first and list the lesson ids you used in "sources". If the playbook doesn't cover it, give sound general advice and set "from_playbook" false.
- "answer" is 2-5 short sentences of coaching. "say_this" holds 1-3 lines the rep can say out loud, fit to their channel.
- Plain words, no jargon. Never invent urgency, discounts, deadlines or results.
- "next_video_idea" is a short suggestion of what kind of training video would fill a gap the question exposed, or "" if none."""

ASK_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "say_this": {"type": "array", "items": {"type": "string"}},
        "sources": {"type": "array", "items": {"type": "string"}},
        "from_playbook": {"type": "boolean"},
        "next_video_idea": {"type": "string"},
    },
    "required": ["answer", "say_this", "sources", "from_playbook", "next_video_idea"],
    "additionalProperties": False,
}

_client_lock = threading.Lock()
_client_cache = {"key": None, "client": None}


def _client():
    import anthropic

    key = call_coach.anthropic_key()
    with _client_lock:
        if _client_cache["key"] != key:
            _client_cache.update(key=key, client=anthropic.Anthropic(api_key=key, timeout=420.0, max_retries=1))
        return _client_cache["client"]


def _offer_json():
    try:
        offer = call_coach.load_playbook().get("offer", {})
    except (OSError, ValueError):
        offer = {}
    return json.dumps(offer, indent=2, sort_keys=True)


def _call(system, user, schema, effort, max_tokens):
    """Returns (parsed_json, error)."""
    if not call_coach.anthropic_key():
        return None, "No Anthropic API key is set (an Admin can add it on Tools > Live Call Coach)."
    import anthropic

    try:
        response = _client().beta.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.AuthenticationError:
        return None, "Anthropic rejected the API key."
    except anthropic.RateLimitError:
        return None, "Anthropic rate limit hit. Try again in a minute."
    except anthropic.APITimeoutError:
        return None, "Claude took too long. Try again."
    except anthropic.APIStatusError as exc:
        return None, call_coach.sanitize("Anthropic error %s: %s" % (exc.status_code, exc.message))
    except anthropic.APIConnectionError:
        return None, "Couldn't reach Anthropic."
    if response.stop_reason == "refusal":
        return None, "Claude declined to process this."
    if response.stop_reason == "max_tokens":
        return None, "The answer was cut off. Try again."
    text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
    try:
        return json.loads(text), None
    except ValueError:
        return None, "Claude returned something unreadable. Try again."


def transcript_text(paras):
    return "\n".join((f"[{p[0]}] " if p[0] is not None else "") + p[1] for p in paras)


def existing_lessons(exclude_row_id=None):
    """[(row, lesson)] for every ready video, oldest first."""
    ok, _err, rows = list_videos()
    out = []
    for row in reversed(rows):
        if row["status"] != "ready" or row["id"] == exclude_row_id:
            continue
        for lesson in (row.get("notes") or {}).get("lessons", []):
            out.append((row, lesson))
    return out


def learn(row_id):
    """Background job: transcript in the row -> notes. Never raises."""
    try:
        row = get_video(row_id, with_transcript=True)
        if row is None:
            return
        paras = row.get("transcript") or []
        known = existing_lessons(exclude_row_id=row_id)
        playbook = "\n".join(f"- [{l['topic']}] {l['title']}" for _r, l in known) or "(empty: this is the first video)"
        user = (
            f"VIDEO: {row.get('title') or row['video_id']} (channel: {row.get('channel') or 'unknown'})\n\n"
            f"EXISTING PLAYBOOK:\n{playbook}\n\n"
            f"TRANSCRIPT ([n] = seconds into the video):\n{transcript_text(paras)}"
        )
        system = LEARN_SYSTEM.format(context=TEAM_CONTEXT, offer=_offer_json())
        notes, error = _call(system, user, NOTES_SCHEMA, "medium", 16000)
        if error:
            _update(row_id, status="failed", error=error)
            return
        for lesson in notes.get("lessons", []):
            lesson["channels"] = [c for c in lesson.get("channels", []) if c in CHANNELS]
        _update(row_id, status="ready", error=None, notes=notes, learned_at=datetime.now(timezone.utc))
    except Exception as exc:  # keep the row honest even on surprises
        log.exception("video coach: learning failed")
        try:
            _update(row_id, status="failed", error=db.sanitize_error(exc))
        except Exception:
            pass


def _start(row_id):
    threading.Thread(target=learn, args=(row_id,), daemon=True, name=f"video-coach-{row_id}").start()


def submit(url, pasted, user):
    """Add a video and start learning it. Returns (ok, error, row_id)."""
    if not user or user.get("status") != "active":
        return False, "You must be logged in with an active account.", None
    vid = video_id_from(url)
    if not vid:
        return False, "That doesn't look like a YouTube video link.", None
    if not call_coach.anthropic_key():
        return False, "No Anthropic API key is set yet (an Admin can add it on Tools > Live Call Coach).", None

    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT id, status FROM sales_videos WHERE video_id = %s", (vid,))
            found = cur.fetchone()
    finally:
        conn.close()
    if found and found[1] in ("ready", "processing"):
        return False, "That video is already in the playbook.", found[0]

    meta = oembed(vid)
    if pasted and pasted.strip():
        paras, length, kind = pasted_paragraphs(pasted), None, "pasted by hand"
    else:
        paras, length, kind, error = fetch_transcript(vid)
        if error:
            return False, error, None
    words = sum(len(p[1].split()) for p in paras)
    if words < 50:
        return False, "The transcript is too short to learn from.", None
    if words > MAX_WORDS:
        return False, f"That video is too long ({words:,} words). Try one under about 4 hours.", None

    name = user.get("sales_rep_name") or user.get("email")
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sales_videos (video_id, title, channel, length_seconds, captions, transcript,
                                          status, error, notes, added_by_user_id, added_by_name, added_at, learned_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'processing', NULL, NULL, %s, %s, now(), NULL)
                ON CONFLICT (video_id) DO UPDATE SET
                    title = EXCLUDED.title, channel = EXCLUDED.channel, length_seconds = EXCLUDED.length_seconds,
                    captions = EXCLUDED.captions, transcript = EXCLUDED.transcript, status = 'processing',
                    error = NULL, notes = NULL, added_by_user_id = EXCLUDED.added_by_user_id,
                    added_by_name = EXCLUDED.added_by_name, added_at = now(), learned_at = NULL
                RETURNING id
                """,
                (vid, meta.get("title") or vid, meta.get("author_name"), length, kind, json.dumps(paras),
                 user.get("id"), name),
            )
            row_id = cur.fetchone()[0]
    finally:
        conn.close()
    _start(row_id)
    return True, None, row_id


def retry(row_id, user):
    row = get_video(row_id)
    if row is None:
        return False, "That video isn't in the playbook."
    if row["status"] == "processing":
        return False, "It's still learning."
    if not call_coach.anthropic_key():
        return False, "No Anthropic API key is set yet."
    _update(row_id, status="processing", error=None, learned_at=datetime.now(timezone.utc))
    _start(row_id)
    return True, None


# ------------------------------------------------------------ playbook

def playbook_view(rows):
    """Lessons from ready videos grouped by topic, in TOPICS order."""
    by_topic = {t: [] for t, _ in TOPICS}
    for row in reversed(rows):
        if row["status"] != "ready":
            continue
        for lesson in (row.get("notes") or {}).get("lessons", []):
            item = dict(lesson)
            item["video_title"] = row.get("title") or row["video_id"]
            item["video_row_id"] = row["id"]
            item["clip"] = clip_url(row["video_id"], lesson.get("start_seconds"))
            item["at"] = ts(lesson.get("start_seconds"))
            item["channel_labels"] = [CHANNEL_LABELS[c] for c in lesson.get("channels", []) if c in CHANNEL_LABELS]
            by_topic.setdefault(lesson.get("topic"), []).append(item)
    return [(t, TOPIC_LABELS[t], by_topic[t]) for t, _ in TOPICS if by_topic.get(t)]


def ask(question, channel, user):
    """Returns (result, error). result has answer/say_this/sources (resolved)."""
    if not user or user.get("status") != "active":
        return None, "You must be logged in with an active account."
    question = (question or "").strip()[:MAX_QUESTION]
    if len(question) < 3:
        return None, "Type a question first."
    known = existing_lessons()
    refs = {}
    lines = []
    for i, (row, lesson) in enumerate(known, 1):
        ref = f"L{i}"
        refs[ref] = (row, lesson)
        say = " / ".join(lesson.get("say", []))
        lines.append(
            f"{ref} [{lesson.get('topic')}] {lesson.get('title')} (channels: {', '.join(lesson.get('channels', []))}): "
            f"{lesson.get('how')} Say: {say}"
        )
    playbook = "\n".join(lines) or "(the playbook is empty: no videos learned yet)"
    system = ASK_SYSTEM.format(context=TEAM_CONTEXT, offer=_offer_json())
    channel_note = CHANNEL_LABELS.get(channel, "any channel")
    user_msg = f"PLAYBOOK:\n{playbook}\n\nREP'S CHANNEL: {channel_note}\n\nQUESTION: {question}"
    result, error = _call(system, user_msg, ASK_SCHEMA, "low", 4000)
    if error:
        return None, error
    sources = []
    for ref in result.get("sources", []):
        if ref in refs:
            row, lesson = refs[ref]
            sources.append({
                "title": lesson.get("title"),
                "video_title": row.get("title") or row["video_id"],
                "at": ts(lesson.get("start_seconds")),
                "clip": clip_url(row["video_id"], lesson.get("start_seconds")),
            })
    result["sources"] = sources
    return result, None
