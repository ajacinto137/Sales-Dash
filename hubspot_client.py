"""Read-only HubSpot API client for the Outbound Call Analytics feature
(README.md "HubSpot Outbound Call Analytics").

Server-side only: the service key comes from the Admin Portal (saved
encrypted in appdb, see app_secrets.py -- takes precedence) or else
HUBSPOT_SERVICE_KEY in the environment (.env via python-dotenv / Docker
Compose env_file), and is only ever placed in the
Authorization header of requests made from this module -- it is never
passed to a template, returned by a route, or logged (every error string
leaving this module goes through sanitize()).

Plain urllib, no `requests`/SDK -- same "no new framework" convention as
the rest of the app. Only GET and the read-only CRM search POST are
issued; nothing here can write to HubSpot.

Handles: pagination (`paging.next.after` cursors), 429 rate limits
(honouring Retry-After), 5xx/network retries with exponential backoff,
and 401/403 turned into a HubSpotError naming the endpoint, HTTP status
and -- when HubSpot reports it -- the missing scope(s)."""

import json
import logging
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request

from dotenv import load_dotenv

import app_secrets

load_dotenv()

BASE_URL = "https://api.hubapi.com"
REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRIES = 5
# HubSpot's CRM search endpoints are limited to a few requests/second per
# account, separately from the general burst limit -- stay under it.
SEARCH_MIN_INTERVAL_SECONDS = 0.25
# Search can never page past 10,000 results for one query;
# iter_calls_modified_since() advances its filter instead.
SEARCH_RESULT_CAP = 10_000

CALL_PROPERTIES = [
    "hs_object_id",
    "hubspot_owner_id",
    "hs_timestamp",
    "hs_call_direction",
    "hs_call_duration",
    "hs_call_disposition",
    "hs_call_status",
    "hs_call_to_number",
    "hs_call_from_number",
    "hs_call_title",
    "hs_call_recording_url",
    "hs_createdate",
    "hs_lastmodifieddate",
]

log = logging.getLogger("hubspot")


SECRET_NAME = "hubspot_service_key"


def _env_key():
    return os.environ.get("HUBSPOT_SERVICE_KEY", "").strip()


def get_service_key():
    """The key saved in Admin > HubSpot Integration, else .env's."""
    return (app_secrets.get_secret(SECRET_NAME)["value"] or "").strip() or _env_key()


def key_source():
    """'admin', 'env', or None -- for the Admin page."""
    if (app_secrets.get_secret(SECRET_NAME)["value"] or "").strip():
        return "admin"
    return "env" if _env_key() else None


def is_configured():
    return bool(get_service_key())


def sanitize(message):
    text = str(message)
    for key in {get_service_key(), _env_key()}:
        if key:
            text = text.replace(key, "***")
    return text


def validate_key(candidate):
    """Checks a key before it's saved: one minimal owners read.
    Returns (usable, message). A 403 means HubSpot accepted the key but
    it lacks a scope -- still saved, with the scope named."""
    try:
        HubSpotClient(service_key=candidate)._request("GET", "/crm/v3/owners", params={"limit": 1})
        return True, None
    except HubSpotError as exc:
        message = str(exc).replace(candidate, "***")
        if exc.status == 403:
            return True, message
        return False, message


class HubSpotError(Exception):
    """`endpoint`, `status` (HTTP status or None for network errors) and
    `required_scopes` (list, possibly empty) are kept separately so the
    Admin page can show a precise diagnostic."""

    def __init__(self, message, endpoint=None, status=None, required_scopes=None):
        super().__init__(sanitize(message))
        self.endpoint = endpoint
        self.status = status
        self.required_scopes = required_scopes or []


def _scopes_from_error_body(body):
    """HubSpot's MISSING_SCOPES 403 body lists the scopes it wanted under
    errors[].context.requiredGranularScopes (newer) or
    errors[].context.requiredScopes (older)."""
    scopes = []
    for err in (body or {}).get("errors") or []:
        context = err.get("context") or {}
        for key in ("requiredGranularScopes", "requiredScopes", "requiredScope"):
            for scope in context.get(key) or []:
                if scope not in scopes:
                    scopes.append(scope)
    return scopes


def _describe_http_error(method, path, status, body):
    message = (body or {}).get("message") or ""
    category = (body or {}).get("category") or ""
    scopes = _scopes_from_error_body(body)
    if status == 401:
        detail = "authentication failed -- the HUBSPOT_SERVICE_KEY is invalid, expired, or revoked"
    elif status == 403:
        detail = "permission denied"
        if scopes:
            detail += f" -- missing scope(s): {', '.join(scopes)}"
    else:
        detail = f"HTTP {status}"
    text = f"HubSpot {method} {path} failed ({status}): {detail}"
    if message:
        text += f". HubSpot said: {message}"
    if category and category not in text:
        text += f" [{category}]"
    return text, scopes


class HubSpotClient:
    def __init__(self, service_key=None):
        self._key = service_key or get_service_key()
        if not self._key:
            raise HubSpotError("HUBSPOT_SERVICE_KEY is not set in the backend environment.")
        self._last_search_at = 0.0

    def _request(self, method, path, params=None, body=None):
        url = BASE_URL + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        data = json.dumps(body).encode("utf-8") if body is not None else None

        attempt = 0
        while True:
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", f"Bearer {self._key}")
            req.add_header("Accept", "application/json")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as res:
                    raw = res.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                status = exc.code
                try:
                    err_body = json.loads(exc.read() or b"{}")
                except (ValueError, OSError):
                    err_body = {}
                retryable = status == 429 or 500 <= status < 600
                if retryable and attempt < MAX_RETRIES:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        wait = float(retry_after) if retry_after else None
                    except ValueError:
                        wait = None
                    if wait is None:
                        wait = min(60.0, (2 ** attempt) + random.uniform(0, 0.5))
                    log.warning("HubSpot %s %s -> %s; retrying in %.1fs (attempt %d/%d)",
                                method, path, status, wait, attempt + 1, MAX_RETRIES)
                    time.sleep(wait)
                    attempt += 1
                    continue
                text, scopes = _describe_http_error(method, path, status, err_body)
                raise HubSpotError(text, endpoint=f"{method} {path}", status=status, required_scopes=scopes) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt < MAX_RETRIES:
                    wait = min(60.0, (2 ** attempt) + random.uniform(0, 0.5))
                    log.warning("HubSpot %s %s network error (%s); retrying in %.1fs",
                                method, path, sanitize(exc), wait)
                    time.sleep(wait)
                    attempt += 1
                    continue
                raise HubSpotError(
                    f"HubSpot {method} {path} unreachable: {exc}", endpoint=f"{method} {path}"
                ) from None

    # ---------------- Owners ----------------

    def iter_owners(self, archived=False):
        after = None
        while True:
            params = {"limit": 100, "archived": "true" if archived else "false"}
            if after:
                params["after"] = after
            page = self._request("GET", "/crm/v3/owners", params=params)
            for owner in page.get("results") or []:
                yield owner
            after = ((page.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                return

    def list_all_owners(self):
        """Active + archived owners. Archived ones are kept so historical
        calls from reps who have since left still resolve to a name."""
        owners = {}
        for owner in self.iter_owners(archived=False):
            owners[str(owner["id"])] = dict(owner, archived=False)
        for owner in self.iter_owners(archived=True):
            owners.setdefault(str(owner["id"]), dict(owner, archived=True))
        return list(owners.values())

    # ---------------- Calls ----------------

    def _search_calls(self, body):
        wait = SEARCH_MIN_INTERVAL_SECONDS - (time.monotonic() - self._last_search_at)
        if wait > 0:
            time.sleep(wait)
        try:
            return self._request("POST", "/crm/v3/objects/calls/search", body=body)
        finally:
            self._last_search_at = time.monotonic()

    def iter_calls_modified_since(self, since_ms, page_size=200, extra_filters=None):
        """Every call whose hs_lastmodifieddate >= since_ms (epoch ms)
        and that matches extra_filters (e.g. an hs_timestamp floor),
        oldest-modified first, yielded in pages. When one query reaches
        search's 10,000-result cap, restarts from the newest modified
        time seen so far (>=, so nothing on that boundary is skipped --
        the upsert makes re-seen rows harmless)."""
        cursor_ms = int(since_ms)
        while True:
            after = None
            seen_in_query = 0
            newest_ms = cursor_ms
            while True:
                body = {
                    "filterGroups": [{"filters": [
                        {"propertyName": "hs_lastmodifieddate", "operator": "GTE", "value": str(cursor_ms)},
                        *(extra_filters or []),
                    ]}],
                    "sorts": [{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}],
                    "properties": CALL_PROPERTIES,
                    "limit": page_size,
                }
                if after:
                    body["after"] = after
                page = self._search_calls(body)
                results = page.get("results") or []
                seen_in_query += len(results)
                for call in results:
                    modified = _parse_epoch_ms((call.get("properties") or {}).get("hs_lastmodifieddate"))
                    if modified and modified > newest_ms:
                        newest_ms = modified
                yield results
                after = ((page.get("paging") or {}).get("next") or {}).get("after")
                if not after:
                    return
                if seen_in_query + page_size > SEARCH_RESULT_CAP:
                    break
            if newest_ms <= cursor_ms:
                # 10,000 calls all modified in the same millisecond --
                # can't advance; bail rather than loop forever.
                raise HubSpotError(
                    "HubSpot call search could not advance past the 10,000-result limit at one timestamp.",
                    endpoint="POST /crm/v3/objects/calls/search",
                )
            cursor_ms = newest_ms

    # ---------------- Contacts ----------------

    def get_call_contact_ids(self, call_ids):
        """{call_id: contact_id} for the first contact associated with
        each call (v4 associations batch read, <=1000 ids per request).
        Calls with no contact are simply absent."""
        result = {}
        ids = [str(i) for i in call_ids]
        for start in range(0, len(ids), 1000):
            page = self._request("POST", "/crm/v4/associations/calls/contacts/batch/read",
                                 body={"inputs": [{"id": i} for i in ids[start:start + 1000]]})
            for row in page.get("results") or []:
                targets = sorted((str(t["toObjectId"]) for t in row.get("to") or [] if t.get("toObjectId")), key=int)
                if targets:
                    result[str((row.get("from") or {}).get("id"))] = targets[0]
        return result

    def get_contacts(self, contact_ids):
        """{contact_id: {"name", "email"}} (batch read, 100 per request).
        Deleted/missing contacts are absent."""
        result = {}
        ids = list(dict.fromkeys(str(i) for i in contact_ids))
        for start in range(0, len(ids), 100):
            page = self._request("POST", "/crm/v3/objects/contacts/batch/read", body={
                "inputs": [{"id": i} for i in ids[start:start + 100]],
                "properties": ["firstname", "lastname", "email"],
            })
            for row in page.get("results") or []:
                props = row.get("properties") or {}
                name = " ".join(p for p in ((props.get("firstname") or "").strip(), (props.get("lastname") or "").strip()) if p)
                result[str(row["id"])] = {"name": name or None, "email": (props.get("email") or "").strip() or None}
        return result

    def find_contacts_by_email(self, email):
        """[{"id", "name", "email"}] for contacts whose primary email is
        `email`, oldest first -- HubSpot matches it case-insensitively."""
        wait = SEARCH_MIN_INTERVAL_SECONDS - (time.monotonic() - self._last_search_at)
        if wait > 0:
            time.sleep(wait)
        try:
            page = self._request("POST", "/crm/v3/objects/contacts/search", body={
                "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
                "properties": ["firstname", "lastname", "email"],
                "limit": 10,
            })
        finally:
            self._last_search_at = time.monotonic()
        contacts = []
        for row in page.get("results") or []:
            if not row.get("id"):
                continue
            props = row.get("properties") or {}
            name = " ".join(p for p in ((props.get("firstname") or "").strip(), (props.get("lastname") or "").strip()) if p)
            contacts.append({"id": str(row["id"]), "name": name or None, "email": (props.get("email") or "").strip() or None})
        return sorted(contacts, key=lambda c: int(c["id"]))

    def get_account_details(self):
        """{"portalId", "uiDomain", ...} -- for building HubSpot UI links."""
        return self._request("GET", "/account-info/v3/details")

    # ---------------- Dispositions ----------------

    def list_call_dispositions(self):
        """[{id, label}, ...] -- HubSpot's own call outcome definitions
        (including any custom outcomes this portal added)."""
        result = self._request("GET", "/calling/v1/dispositions")
        if isinstance(result, list):
            return result
        return result.get("results") or []


def _parse_epoch_ms(value):
    """hs_lastmodifieddate arrives as an ISO-8601 string from v3; older
    payloads use epoch ms. None for anything unparseable."""
    if value in (None, ""):
        return None
    text = str(value)
    if text.isdigit():
        return int(text)
    from datetime import datetime
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None
