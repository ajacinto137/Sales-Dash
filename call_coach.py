"""Live Call Coach (added 2026-11-07) -- the server half of the Chrome
extension in call_coach_extension/.

The extension records a HubSpot browser call (the HubSpot tab = the
customer, the rep's microphone = the rep), streams both channels to
Deepgram for live speech-to-text, and posts each finished line here:

- match()   instant tier: keyword cards from call_coach_playbook.json,
            no network, a few milliseconds.
- suggest() smart tier: the last stretch of transcript plus the playbook
            goes to Claude, which returns one short "say this next" card
            (or nothing, when no card is needed).
- deepgram_token() a short-lived Deepgram token, so the real Deepgram key
            never leaves this server.

Keys: Tools > Live Call Coach, Admin only (encrypted in app_secrets, same as the HubSpot
key), else ANTHROPIC_API_KEY / DEEPGRAM_API_KEY in .env.

Truth rule: the coach only quotes prices, plans and facts that are in the
playbook's "offer" block. A blank offer field renders as
"[fill in: ...]" rather than something made up."""

import json
import logging
import os
import re
import threading
import io
import zipfile
import urllib.error
import urllib.request

import app_secrets

log = logging.getLogger("call_coach")

EXTENSION_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "call_coach_extension")
PLAYBOOK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "call_coach_playbook.json")

ANTHROPIC_SECRET = "anthropic_api_key"
DEEPGRAM_SECRET = "deepgram_api_key"

# Opus 5.5 at low effort by default; COACH_MODEL can point it at a faster
# model (e.g. claude-haiku-5-5) if cards arrive too slowly on live calls.
DEFAULT_MODEL = "claude-opus-5-5"
MAX_TRANSCRIPT_LINES = 40
MAX_LINE_CHARS = 600

DEEPGRAM_GRANT_URL = "https://api.deepgram.com/v1/auth/grant"
DEEPGRAM_TOKEN_TTL = 60

KINDS = ("objection", "question", "buying_signal", "none")

_playbook_cache = {"mtime": None, "data": None}
_playbook_lock = threading.Lock()
_client_lock = threading.Lock()
_client_cache = {"key": None, "client": None}


# ---------------------------------------------------------------- keys

def _key(secret_name, env_name):
    saved = (app_secrets.get_secret(secret_name)["value"] or "").strip()
    return saved or os.environ.get(env_name, "").strip()


def anthropic_key():
    return _key(ANTHROPIC_SECRET, "ANTHROPIC_API_KEY")


def deepgram_key():
    return _key(DEEPGRAM_SECRET, "DEEPGRAM_API_KEY")


def key_source(secret_name, env_name):
    """'admin', 'env' or None -- for the setup page."""
    if (app_secrets.get_secret(secret_name)["value"] or "").strip():
        return "admin"
    return "env" if os.environ.get(env_name, "").strip() else None


def key_hint(value):
    return f"…{value[-4:]}" if len(value) >= 12 else ("set" if value else None)


def sanitize(message):
    text = str(message)
    for key in (anthropic_key(), deepgram_key()):
        if key:
            text = text.replace(key, "***")
    return text


def model():
    return os.environ.get("COACH_MODEL", "").strip() or DEFAULT_MODEL


# ------------------------------------------------------------ playbook

def load_playbook(path=PLAYBOOK_PATH):
    """Parsed playbook, re-read whenever the file changes on disk."""
    mtime = os.path.getmtime(path)
    with _playbook_lock:
        if path == PLAYBOOK_PATH and _playbook_cache["mtime"] == mtime:
            return _playbook_cache["data"]
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if path == PLAYBOOK_PATH:
            _playbook_cache.update(mtime=mtime, data=data)
        return data


def _offer_text(offer, field):
    value = offer.get(field)
    if field == "cheapest_plan" and not value:
        return "[fill in: your cheapest plan and price]"
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "[fill in: %s]" % field.replace("_", " ")


def fill(template, offer):
    """Swap {field} placeholders for the playbook's offer values."""
    return re.sub(r"\{(\w+)\}", lambda m: _offer_text(offer, m.group(1)), template)


def _normalize(text):
    text = (text or "").lower().replace("’", "'")
    return re.sub(r"\s+", " ", text)


def _phrase_in(phrase, text):
    return re.search(r"(?<![a-z])" + re.escape(phrase.lower()) + r"(?![a-z])", text) is not None


def match(text, playbook=None):
    """Instant cards for one customer line: every objection whose
    listen_for phrase appears, plus a buying-signal card. Ordered by
    where in the line they were said."""
    playbook = playbook or load_playbook()
    offer = playbook.get("offer", {})
    said = _normalize(text)
    if not said:
        return []
    found = []
    for obj in playbook.get("objections", []):
        hits = [p for p in obj.get("listen_for", []) if _phrase_in(p, said)]
        if hits:
            first = min(said.find(p.lower()) for p in hits)
            found.append((first, {
                "kind": "objection",
                "id": obj["id"],
                "label": obj["label"],
                "say": fill(obj["say"], offer),
                "why": obj.get("why", ""),
                "heard": hits[0],
                "source": "playbook",
            }))
    signals = [p for p in playbook.get("buying_signals", []) if _phrase_in(p, said)]
    if signals:
        found.append((said.find(signals[0].lower()), {
            "kind": "buying_signal",
            "id": "buying_signal",
            "label": "Buying signal",
            "say": "Answer it, then agree the next step: pick an install day now.",
            "why": "They're asking how to move forward -- don't keep pitching.",
            "heard": signals[0],
            "source": "playbook",
        }))
    found.sort(key=lambda pair: pair[0])
    return [card for _, card in found]


# ------------------------------------------------------------- Claude

SYSTEM_TEMPLATE = """You are a live sales coach sitting beside a {company} fiber internet sales rep during a phone call. You read the live transcript and decide whether the rep needs a card telling them what to say next.

How to answer:
- Only produce a card when the customer's latest words need a response the rep might fumble: an objection, a question, or a buying signal. Otherwise return kind "none".
- "say" is one or two short spoken sentences the rep can read aloud as-is. Warm, plain, no jargon, no pressure, no fake urgency, no invented discounts or deadlines.
- Prefer a question back to the customer over a speech.
- Only state prices, plans, install details, contract terms or results that appear in OFFER below. If the right answer needs a fact that is missing, put "[fill in: what's needed]" in its place instead of guessing.
- If the customer clearly said no or asked not to be contacted, the card tells the rep to thank them and end politely.
- "why" is under 15 words, for the rep only.

OFFER (the only facts you may state):
{offer}

PLAYBOOK (proven answers; adapt them to what was actually said):
{objections}"""

CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "label": {"type": "string"},
        "say": {"type": "string"},
        "why": {"type": "string"},
    },
    "required": ["kind", "label", "say", "why"],
    "additionalProperties": False,
}


def system_prompt(playbook):
    offer = playbook.get("offer", {})
    objections = [
        {"objection": o["label"], "answer": fill(o["say"], offer), "why": o.get("why", "")}
        for o in playbook.get("objections", [])
    ]
    return SYSTEM_TEMPLATE.format(
        company=offer.get("company") or "the company's",
        offer=json.dumps(offer, indent=2, sort_keys=True),
        objections=json.dumps(objections, indent=2),
    )


def clean_transcript(lines):
    """[{'speaker': 'rep'|'customer', 'text': str}] -> trimmed, validated,
    last MAX_TRANSCRIPT_LINES only."""
    out = []
    for line in lines or []:
        if not isinstance(line, dict):
            continue
        speaker = "Rep" if line.get("speaker") == "rep" else "Customer"
        text = re.sub(r"\s+", " ", str(line.get("text") or "")).strip()[:MAX_LINE_CHARS]
        if text:
            out.append((speaker, text))
    return out[-MAX_TRANSCRIPT_LINES:]


def user_prompt(lines, context=""):
    transcript = "\n".join("%s: %s" % pair for pair in lines)
    parts = []
    context = (context or "").strip()[:1000]
    if context:
        parts.append("What the rep knows about this customer:\n" + context)
    parts.append("Live transcript so far (most recent last):\n" + transcript)
    parts.append("What should the rep say next, if anything?")
    return "\n\n".join(parts)


def _client():
    import anthropic  # imported lazily so the rest of the app runs without it

    key = anthropic_key()
    with _client_lock:
        if _client_cache["key"] != key:
            _client_cache.update(key=key, client=anthropic.Anthropic(api_key=key, timeout=20.0, max_retries=1))
        return _client_cache["client"]


def parse_card(response):
    """The model's JSON card, or None for kind 'none' / a refusal / junk."""
    if getattr(response, "stop_reason", None) == "refusal":
        return None
    text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
    if not text:
        return None
    try:
        card = json.loads(text)
    except ValueError:
        log.warning("call coach: model returned non-JSON text")
        return None
    if not isinstance(card, dict) or card.get("kind") not in KINDS or card["kind"] == "none":
        return None
    say = str(card.get("say") or "").strip()
    if not say:
        return None
    return {
        "kind": card["kind"],
        "id": "ai",
        "label": str(card.get("label") or "").strip()[:60] or card["kind"].replace("_", " ").title(),
        "say": say[:400],
        "why": str(card.get("why") or "").strip()[:160],
        "source": "ai",
    }


def suggest(lines, context="", playbook=None):
    """Returns (card_or_None, error_or_None)."""
    if not anthropic_key():
        return None, "No Anthropic API key is set (an Admin can add it on Tools > Live Call Coach)."
    cleaned = clean_transcript(lines)
    if not cleaned or cleaned[-1][0] != "Customer":
        return None, None
    playbook = playbook or load_playbook()
    import anthropic

    try:
        response = _client().beta.messages.create(
            model=model(),
            max_tokens=1024,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": CARD_SCHEMA},
            },
            system=[{"type": "text", "text": system_prompt(playbook), "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_prompt(cleaned, context)}],
        )
    except anthropic.AuthenticationError:
        return None, "Anthropic rejected the API key."
    except anthropic.RateLimitError:
        return None, "Anthropic rate limit hit -- try again in a moment."
    except anthropic.APIStatusError as exc:
        return None, sanitize("Anthropic error %s: %s" % (exc.status_code, exc.message))
    except anthropic.APIConnectionError:
        return None, "Couldn't reach Anthropic."
    return parse_card(response), None


# ------------------------------------------------------------ Deepgram

def deepgram_token():
    """Returns (token, expires_in, error). A short-lived JWT the extension
    uses for one streaming connection (WebSocket subprotocol 'bearer')."""
    key = deepgram_key()
    if not key:
        return None, None, "No Deepgram API key is set (an Admin can add it on Tools > Live Call Coach)."
    req = urllib.request.Request(
        DEEPGRAM_GRANT_URL,
        data=json.dumps({"ttl_seconds": DEEPGRAM_TOKEN_TTL}).encode("utf-8"),
        headers={"Authorization": "Token " + key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return None, None, "Deepgram rejected the token request (HTTP %s)." % exc.code
    except (urllib.error.URLError, ValueError, OSError) as exc:
        return None, None, sanitize("Couldn't reach Deepgram: %s" % exc)
    token = body.get("access_token")
    if not token:
        return None, None, "Deepgram didn't return a token."
    return token, body.get("expires_in", DEEPGRAM_TOKEN_TTL), None


# ------------------------------------------------------------ extension

ORIGIN_PLACEHOLDER = "__SERVER_ORIGIN__"
_TEMPLATED = {"manifest.json", "config.js"}


def extension_zip(server_origin):
    """The unpacked-extension folder as a .zip, with this server's origin
    written into manifest.json (host permission) and config.js."""
    origin = server_origin.rstrip("/")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(EXTENSION_DIR):
            for name in sorted(files):
                if name.startswith("."):
                    continue
                path = os.path.join(root, name)
                rel = os.path.relpath(path, EXTENSION_DIR)
                arcname = os.path.join("call-coach-extension", rel)
                if rel in _TEMPLATED:
                    with open(path, encoding="utf-8") as fh:
                        zf.writestr(arcname, fh.read().replace(ORIGIN_PLACEHOLDER, origin))
                else:
                    zf.write(path, arcname)
    return buf.getvalue()
