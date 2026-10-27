"""Customer Profile page data (added 2026-10-22, by request -- "add more
data now and we can subtract later"). Everything known about ONE order /
customer, gathered on demand when someone clicks an account card:

- PlanetWeb (SQL Server): the order itself (FTTPFormData + lookups),
  extra line items, install jobs (live and cancelled), schedule notes,
  follow-ups, and other orders from the same customer.
- KPI (Postgres): the customer's Vision account -- status/balance,
  packages, service items, payments, credits, tickets, tasks + task
  notes, extra phones/emails. Virginia customers live in the virginia_*
  copies of the subscriber/package/task tables (no VA tickets/payments
  tables exist).
- appdb: Needs Attention status/notes, and HubSpot calls with the
  customer (any of their phone numbers or emails).

Unlike the dashboard (served from the in-memory snapshot), this page
queries per request -- it's one customer by primary key / indexed uuid,
clicked one at a time, not a page every rep auto-refreshes. Each source
is loaded independently, so one database being down only blanks its own
sections (see `errors`), never the whole page.

To remove a field later: delete its line in the matching _*_fields()
function -- the template renders whatever label/value pairs it's given.
Card numbers, CVV, expiry and bank details are deliberately never
selected; only the payment method and card last 4 are shown.
"""

import re

import attention_store
import db
import hubspot_logic
import needs_attention_service
from sales_metrics import EASTERN_TZ

ORDER_QUERY = """
SELECT
    fd.ID, fd.FirstName, fd.LastName, fd.Company, fd.EmailAddress, fd.PhoneNumber,
    fd.SMSPhoneNumber, fd.Address, fd.ApartmentSuite, fd.City, fd.State, fd.Zipcode,
    fd.NJPR_municipalityName, fd.NJPR_countyName, fd.Google_FormattedAddress,
    fd.Google_Location_lat, fd.Google_Location_lng, fd.Property_StateName,
    fd.IsBusiness, fd.AreYouCustomer, fd.Comments, fd.HowDidYouHearMemo, fd.ReferralData,
    fd.SourceToken, fd.MarketingToken, fd.InsertDate, fd.ModifiedDate,
    fd.Wireless_Devices_Count, fd.ConsentForEmail, fd.ConsentForSMS, fd.ConsentForSMSMarketing,
    fd.Cc_PayBy, fd.Cc_Last4, fd.MailedInvoice, fd.KeepCurrentPhoneNumber,
    fd.RequestedFaxBridge, fd.IsCloudPhoneOrder, fd.VoiceLOAReceived, fd.VoiceLOAReceivedDate,
    fd.CustomerSaysIsUnderground, fd.CompetitorCancellation, fd.FalloffURL,
    fd.vi_subscriber_id, fd.vi_subscriber_uuid, fd.vi_subscriber_status, fd.vi_subscriber_type,
    fd.vi_status_date, fd.vi_last_status_memo, fd.vi_payment_injected_date,
    rates.ServiceName, rates.SubTitle, rates.SpeedDown, rates.SpeedUp, rates.MRC, rates.NRC,
    rates.UberSmith_term,
    hd.HearAboutUs,
    vs.WebFriendlyName AS VisionOrderStatus,
    sc.Name AS SalesChannelName,
    m.Name AS MarketName,
    sa.AreaName AS ScheduleAreaName,
    rc.DoorKnockerName, rc.IsDoorKnocker,
    an.UniqueURL, an.META_utm_source, an.META_utm_campaign, an.META_utm_medium, an.META_utm_content
FROM FTTPFormData fd
LEFT JOIN FTTPRates rates ON rates.ID = fd.RateID
LEFT JOIN FTTPFormDataHdyh hd ON hd.ID = fd.HowDidYouHearID
LEFT JOIN FTTPFormDataVisionStatus vs ON vs.StatusID = fd.vi_status_id
LEFT JOIN FTTPFormDataSalesChannels sc ON sc.ID = fd.SalesChannel_ID
LEFT JOIN FTTPMarkets m ON m.ID = fd.MarketID
LEFT JOIN FTTPScheduleAreas sa ON sa.ID = fd.ScheduleAreaID
OUTER APPLY (
    SELECT TOP 1 DoorKnockerName, IsDoorKnocker FROM FTTPReferralCodes
    WHERE Code = fd.ReferralData AND IsDeleted = 0 ORDER BY ID DESC
) rc
OUTER APPLY (
    SELECT TOP 1 * FROM FTTPFormDataAnalytics WHERE FormDataID = fd.ID ORDER BY ID DESC
) an
WHERE fd.ID = ?
"""

ITEMS_QUERY = """
SELECT items.Quantity, r.ServiceName, r.MRC, r.NRC
FROM FTTPFormDataItems items
JOIN FTTPRates r ON r.ID = items.RateID
WHERE items.FormDataID = ?
ORDER BY items.ID
"""

JOBS_QUERY = """
SELECT j.ID, jt.Name AS JobType, s.StartDate, s.EndDate, st.Name AS Status, j.StatusDate,
       ast.Name AS AdminStatus, j.SchedulingNotes, j.IsTwoManJob, j.IsRequiresBucket,
       j.AddressOverride, j.InsertDate, u.FullName AS CreatedBy
FROM FTTPScheduleJobs j
LEFT JOIN FTTPScheduleJobTypes jt ON jt.ID = j.TypeID
LEFT JOIN FTTPScheduleSlots s ON s.ID = j.SlotID
LEFT JOIN FTTPScheduleJobStatuses st ON st.ID = j.StatusID
LEFT JOIN FTTPScheduleJobAdminStatuses ast ON ast.ID = j.AdminStatusID
LEFT JOIN Users u ON u.ID = j.InsertUserID
WHERE j.FormDataID = ?
ORDER BY s.StartDate DESC, j.ID DESC
"""

CANCELED_JOBS_QUERY = """
SELECT c.ID, jt.Name AS JobType, s.StartDate, c.CanceledDate, c.CancelReason,
       u.FullName AS CanceledBy
FROM FTTPScheduleJobsCanceled c
LEFT JOIN FTTPScheduleJobTypes jt ON jt.ID = c.TypeID
LEFT JOIN FTTPScheduleSlots s ON s.ID = c.SlotID
LEFT JOIN Users u ON u.ID = c.CanceledByUserID
WHERE c.FormDataID = ?
ORDER BY c.CanceledDate DESC
"""

NOTES_QUERY = """
SELECT n.Contents, nt.Name AS NoteType, n.IsCustomerViewable, n.InsertDate, u.FullName AS Author
FROM FTTPScheduleNotes n
LEFT JOIN FTTPScheduleNoteTypes nt ON nt.ID = n.NoteTypeID
LEFT JOIN Users u ON u.ID = n.InsertUserID
WHERE n.FormDataID = ? AND ISNULL(n.IsDeleted, 0) = 0
ORDER BY n.InsertDate DESC
"""

FOLLOW_UPS_QUERY = """
SELECT f.ID, f.InsertDate, f.Memo, f.IsClosed, f.ClosedDate, ev.StatusName AS EmailStatus,
       assigned.FullName AS AssignedTo,
       STUFF((
           SELECT ', ' + r.ReasonName
           FROM FTTPFollowUpToReason fr JOIN FTTPFollowUpReasons r ON r.ID = fr.ReasonID
           WHERE fr.FollowUpID = f.ID
           FOR XML PATH('')
       ), 1, 2, '') AS Reasons
FROM FTTPFollowUps f
LEFT JOIN FTTPFollowUpAcEventStatusID ev ON ev.StatusID = f.Ac_EventStatusID
LEFT JOIN Users assigned ON assigned.ID = f.AssignedToUserID
WHERE f.FormDataID = ? AND ISNULL(f.IsDeleted, 0) = 0
ORDER BY f.InsertDate DESC
"""

# Other orders by the same customer: same email or same Vision subscriber.
RELATED_ORDERS_QUERY = """
SELECT TOP 15 fd.ID, fd.InsertDate, fd.SourceToken, fd.QuoteID, rates.ServiceName
FROM FTTPFormData fd
LEFT JOIN FTTPRates rates ON rates.ID = fd.RateID
WHERE fd.ID <> ?
  AND ((? IS NOT NULL AND fd.EmailAddress = ?) OR (? IS NOT NULL AND fd.vi_subscriber_uuid = ?))
ORDER BY fd.ID DESC
"""


def _vision_tables(state):
    prefix = "virginia_" if (state or "").strip().upper() == "VA" else ""
    return {
        "subscribers": f"{prefix}vision_subscribers",
        "packages": f"{prefix}vision_subscribers_packages",
        "items": f"{prefix}vision_subscribers_packages_items",
        "tasks": f"{prefix}vision_tasks",
        "task_notes": f"{prefix}vision_task_notes",
        "is_va": bool(prefix),
    }


# ---------------------------------------------------------------- helpers

def _rows(cursor):
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _clean(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def fmt_datetime(value):
    if value is None:
        return None
    if hasattr(value, "hour") and (value.hour or value.minute):
        return value.strftime("%b %-d, %Y, %-I:%M %p")
    return value.strftime("%b %-d, %Y")


def fmt_date(value):
    return value.strftime("%b %-d, %Y") if value is not None else None


def fmt_money(value):
    if value is None:
        return None
    return f"${float(value):,.2f}"


def fmt_bool(value):
    if value is None:
        return None
    return "Yes" if value else "No"


_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(value):
    """Vision ticket/task notes are stored as HTML -- shown as plain text
    (Jinja autoescapes it either way; this just drops the tags)."""
    value = _clean(value)
    if not value:
        return None
    text = _TAG_RE.sub(" ", value.replace("<br>", "\n").replace("<br/>", "\n"))
    return re.sub(r"[ \t]+", " ", text).strip()


def phone_e164(phone):
    """'(862) 268-0700' -> '+18622680700' (HubSpot stores to_number this way)."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 10:
        digits = "1" + digits
    return f"+{digits}" if len(digits) == 11 else None


def _fields(pairs):
    """[(label, value), ...] minus empty values -- the template renders
    exactly what's left."""
    return [{"label": label, "value": value} for label, value in pairs if _clean(value) not in (None, "")]


# ------------------------------------------------------------- PlanetWeb

def _order_sections(o):
    speed = None
    if o.get("SpeedDown") is not None and o.get("SpeedUp") is not None:
        speed = f"{int(o['SpeedDown'])}/{int(o['SpeedUp'])} Mbps"
    term = o.get("UberSmith_term")
    lat, lng = o.get("Google_Location_lat"), o.get("Google_Location_lng")
    door_knocker = o.get("DoorKnockerName") if o.get("IsDoorKnocker") else None
    payment = " · ".join(p for p in [_clean(o.get("Cc_PayBy")), f"card ending {o['Cc_Last4']}" if _clean(o.get("Cc_Last4")) else None] if p)
    return {
        "contact": _fields([
            ("Phone", o.get("PhoneNumber")),
            ("SMS Phone", o.get("SMSPhoneNumber")),
            ("Email", o.get("EmailAddress")),
            ("Company", o.get("Company")),
            ("Account Type", "Business" if o.get("IsBusiness") else "Residential"),
            ("Existing Customer?", fmt_bool(o.get("AreYouCustomer"))),
            ("Email Consent", fmt_bool(o.get("ConsentForEmail"))),
            ("SMS Consent", fmt_bool(o.get("ConsentForSMS"))),
            ("SMS Marketing Consent", fmt_bool(o.get("ConsentForSMSMarketing"))),
        ]),
        "order": _fields([
            ("Order ID", o.get("ID")),
            ("Order Placed", fmt_datetime(o.get("InsertDate"))),
            ("Last Modified", fmt_datetime(o.get("ModifiedDate"))),
            ("Package", o.get("ServiceName")),
            ("Package Details", o.get("SubTitle")),
            ("Speed (down/up)", speed),
            ("Monthly Price", fmt_money(o.get("MRC"))),
            ("One-time Charge", fmt_money(o.get("NRC"))),
            ("Term", f"{term} months" if term else None),
            ("Wireless Devices", o.get("Wireless_Devices_Count")),
            ("Payment", payment),
            ("Mailed Invoice", fmt_bool(o.get("MailedInvoice"))),
            ("Keep Current Phone #", fmt_bool(o.get("KeepCurrentPhoneNumber"))),
            ("Cloud Phone Order", fmt_bool(o.get("IsCloudPhoneOrder"))),
            ("Fax Bridge Requested", fmt_bool(o.get("RequestedFaxBridge"))),
            ("Voice LOA Received", fmt_date(o.get("VoiceLOAReceivedDate")) if o.get("VoiceLOAReceived") else None),
            ("Competitor Cancellation", "Yes" if o.get("CompetitorCancellation") else None),
            ("Customer Comments", o.get("Comments")),
        ]),
        "source": _fields([
            ("Sales Channel", o.get("SalesChannelName")),
            ("Source Token", o.get("SourceToken")),
            ("Referral Code", o.get("ReferralData")),
            ("Door Knocker", door_knocker),
            ("How They Heard", o.get("HearAboutUs")),
            ("How They Heard (note)", o.get("HowDidYouHearMemo")),
            ("Marketing Token", o.get("MarketingToken")),
            ("UTM Source", o.get("META_utm_source")),
            ("UTM Campaign", o.get("META_utm_campaign")),
            ("UTM Medium", o.get("META_utm_medium")),
            ("UTM Content", o.get("META_utm_content")),
            ("Landing URL", o.get("UniqueURL")),
            ("Abandoned-cart URL", o.get("FalloffURL")),
        ]),
        "location": _fields([
            ("Verified Address", o.get("Google_FormattedAddress")),
            ("Municipality", o.get("NJPR_municipalityName")),
            ("County", o.get("NJPR_countyName")),
            ("Market", o.get("MarketName")),
            ("Schedule Area", o.get("ScheduleAreaName")),
            ("Customer Says Underground", "Yes" if o.get("CustomerSaysIsUnderground") else None),
        ]),
        "vision_order": _fields([
            ("Vision Subscriber ID", o.get("vi_subscriber_id")),
            ("Vision Status (at order)", o.get("vi_subscriber_status")),
            ("Vision Type", o.get("vi_subscriber_type")),
            ("Order Sync Status", o.get("VisionOrderStatus")),
            ("Order Sync Updated", fmt_datetime(o.get("vi_status_date"))),
            ("Order Sync Message", o.get("vi_last_status_memo")),
            ("Payment Sent to Vision", fmt_datetime(o.get("vi_payment_injected_date"))),
        ]),
        "map_url": f"https://www.google.com/maps?q={lat},{lng}" if lat is not None and lng is not None else None,
    }


def _load_planetweb(sale_id):
    conn = db.get_planetweb_connection()
    try:
        cur = conn.cursor()
        cur.execute(ORDER_QUERY, sale_id)
        orders = _rows(cur)
        if not orders:
            return None
        order = orders[0]

        cur.execute(ITEMS_QUERY, sale_id)
        items = [{
            "name": r["ServiceName"], "quantity": r["Quantity"],
            "mrc": fmt_money(r["MRC"]), "nrc": fmt_money(r["NRC"]),
        } for r in _rows(cur)]

        cur.execute(JOBS_QUERY, sale_id)
        jobs = [{
            "id": r["ID"], "type": r["JobType"],
            "slot": (f"{fmt_datetime(r['StartDate'])} – {r['EndDate'].strftime('%-I:%M %p')}"
                     if r["StartDate"] and r["EndDate"] else fmt_datetime(r["StartDate"])),
            "status": r["Status"], "status_date": fmt_datetime(r["StatusDate"]),
            "admin_status": r["AdminStatus"], "notes": _clean(r["SchedulingNotes"]),
            "flags": ", ".join(f for f, on in [("Two-man job", r["IsTwoManJob"]), ("Needs bucket truck", r["IsRequiresBucket"])] if on),
            "address_override": _clean(r["AddressOverride"]),
            "created": fmt_datetime(r["InsertDate"]), "created_by": r["CreatedBy"],
        } for r in _rows(cur)]

        cur.execute(CANCELED_JOBS_QUERY, sale_id)
        canceled_jobs = [{
            "id": r["ID"], "type": r["JobType"], "slot": fmt_datetime(r["StartDate"]),
            "canceled": fmt_datetime(r["CanceledDate"]), "reason": _clean(r["CancelReason"]),
            "canceled_by": r["CanceledBy"],
        } for r in _rows(cur)]

        cur.execute(NOTES_QUERY, sale_id)
        notes = [{
            "text": strip_html(r["Contents"]), "type": r["NoteType"], "author": r["Author"],
            "customer_visible": bool(r["IsCustomerViewable"]), "created": fmt_datetime(r["InsertDate"]),
        } for r in _rows(cur) if _clean(r["Contents"])]

        cur.execute(FOLLOW_UPS_QUERY, sale_id)
        follow_ups = [{
            "created": fmt_datetime(r["InsertDate"]), "memo": _clean(r["Memo"]),
            "reasons": r["Reasons"], "assigned_to": r["AssignedTo"],
            "closed": fmt_datetime(r["ClosedDate"]) if r["IsClosed"] else None,
            "email_status": r["EmailStatus"],
        } for r in _rows(cur)]

        email = _clean(order.get("EmailAddress"))
        uuid = _clean(order.get("vi_subscriber_uuid"))
        cur.execute(RELATED_ORDERS_QUERY, sale_id, email, email, uuid, uuid)
        related = [{
            "id": r["ID"], "placed": fmt_datetime(r["InsertDate"]), "package": r["ServiceName"],
            "source": r["SourceToken"], "completed": r["QuoteID"] is not None,
        } for r in _rows(cur)]
    finally:
        conn.close()

    return {
        "raw": order,
        **_order_sections(order),
        "line_items": items,
        "jobs": jobs,
        "canceled_jobs": canceled_jobs,
        "notes": notes,
        "follow_ups": follow_ups,
        "related_orders": related,
    }


# ------------------------------------------------------------ KPI/Vision

def _load_vision(subscriber_uuid, state):
    t = _vision_tables(state)
    conn = db.get_kpi_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT id, status, type, balance, created_date, full_name, company_name "
            f"FROM {t['subscribers']} WHERE uuid = %s LIMIT 1",
            (subscriber_uuid,),
        )
        subs = _rows(cur)
        sub = subs[0] if subs else {}
        account = _fields([
            ("Vision Account Status", sub.get("status")),
            ("Account Type", sub.get("type")),
            ("Balance", fmt_money(sub.get("balance"))),
            ("Vision ID", sub.get("id")),
            ("Account Created", fmt_datetime(sub.get("created_date"))),
            ("Name in Vision", sub.get("full_name")),
            ("Company in Vision", sub.get("company_name")),
        ])

        # virginia_vision_subscribers_packages has no task_cancellation_date.
        task_cancel = "NULL" if t["is_va"] else "task_cancellation_date"
        cur.execute(
            f"SELECT name, active, term, bill_date, install_time, first_invoice_date, last_invoice_date, "
            f"estimated_cancellation_date, {task_cancel} AS task_cancellation_date, contract_confirmed "
            f"FROM {t['packages']} WHERE subscriber_uuid = %s ORDER BY first_invoice_date DESC NULLS LAST",
            (subscriber_uuid,),
        )
        packages = [{
            "name": r["name"], "active": r["active"], "term": r["term"],
            "bill_day": r["bill_date"], "install_time": fmt_datetime(r["install_time"]),
            "first_invoice": fmt_date(r["first_invoice_date"]), "last_invoice": fmt_date(r["last_invoice_date"]),
            "cancellation": fmt_date(r["estimated_cancellation_date"] or r["task_cancellation_date"]),
            "contract_confirmed": fmt_bool(r["contract_confirmed"]),
        } for r in _rows(cur)]

        cur.execute(
            f"SELECT name, display_description, item_price, active, active_date "
            f"FROM {t['items']} WHERE subscriber_uuid = %s ORDER BY active DESC, active_date DESC NULLS LAST",
            (subscriber_uuid,),
        )
        items = [{
            "name": r["name"], "description": _clean(r["display_description"]),
            "price": fmt_money(r["item_price"]), "active": r["active"], "since": fmt_date(r["active_date"]),
        } for r in _rows(cur)]

        cur.execute(
            f"SELECT uuid, type, active, completed, completed_date, due_date, scheduled_start_date "
            f"FROM {t['tasks']} WHERE subscriber_uuid = %s "
            f"ORDER BY COALESCE(completed_date, due_date, scheduled_start_date) DESC NULLS LAST LIMIT 60",
            (subscriber_uuid,),
        )
        task_rows = _rows(cur)
        task_uuids = [r["uuid"] for r in task_rows if r["uuid"]]
        task_notes = {}
        if task_uuids:
            cur.execute(
                f"SELECT task_uuid, stripped_note, note, created_by, time_created "
                f"FROM {t['task_notes']} WHERE task_uuid = ANY(%s) ORDER BY time_created DESC",
                (task_uuids,),
            )
            for r in _rows(cur):
                text = _clean(r["stripped_note"]) or strip_html(r["note"])
                if text:
                    task_notes.setdefault(r["task_uuid"], []).append({
                        "text": text, "by": r["created_by"], "created": fmt_datetime(r["time_created"]),
                    })
        tasks = [{
            "type": r["type"],
            "state": "Completed" if r["completed"] else ("Open" if r["active"] else "Inactive"),
            "completed": fmt_date(r["completed_date"]), "due": fmt_date(r["due_date"]),
            "notes": task_notes.get(r["uuid"], []),
        } for r in task_rows]

        payments, credits, tickets, phones, emails = [], [], [], [], []
        if not t["is_va"]:
            cur.execute(
                "SELECT transaction_date, payment_type, total, voided FROM vision_payment_receipts "
                "WHERE subscriber_uuid = %s ORDER BY transaction_date DESC LIMIT 24",
                (subscriber_uuid,),
            )
            payments = [{"date": fmt_date(r["transaction_date"]), "type": r["payment_type"],
                         "total": fmt_money(r["total"]), "voided": bool(r["voided"])} for r in _rows(cur)]

            cur.execute(
                "SELECT transaction_date, credit_type, total, voided FROM vision_credit_receipts "
                "WHERE subscriber_uuid = %s ORDER BY transaction_date DESC LIMIT 24",
                (subscriber_uuid,),
            )
            credits = [{"date": fmt_date(r["transaction_date"]), "type": r["credit_type"],
                        "total": fmt_money(r["total"]), "voided": bool(r["voided"])} for r in _rows(cur)]

            cur.execute(
                "SELECT ticket_id, ticket_description, category_description, subcategory_description, "
                "status_description, priority_name, assigned_to, metadata_created_date, closed_date "
                "FROM vision_tickets WHERE subscriber_uuid = %s "
                "ORDER BY metadata_created_date DESC NULLS LAST LIMIT 30",
                (subscriber_uuid,),
            )
            tickets = [{
                "id": r["ticket_id"], "description": strip_html(r["ticket_description"]),
                "category": " / ".join(p for p in [r["category_description"], r["subcategory_description"]] if p),
                "status": r["status_description"], "priority": r["priority_name"],
                "assigned_to": r["assigned_to"], "created": fmt_datetime(r["metadata_created_date"]),
                "closed": fmt_datetime(r["closed_date"]),
            } for r in _rows(cur)]

            cur.execute(
                "SELECT phone_number, name, subscriber_contact_type, default_phone_number "
                "FROM vision_subscribers_phone_numbers WHERE subscriber_uuid = %s",
                (subscriber_uuid,),
            )
            phones = [{"value": r["phone_number"], "label": r["name"] or r["subscriber_contact_type"],
                       "default": bool(r["default_phone_number"])} for r in _rows(cur) if r["phone_number"]]

            cur.execute(
                "SELECT email_address, name, subscriber_contact_type, default_email_address "
                "FROM vision_subscribers_email_addresses WHERE subscriber_uuid = %s",
                (subscriber_uuid,),
            )
            emails = [{"value": r["email_address"], "label": r["name"] or r["subscriber_contact_type"],
                       "default": bool(r["default_email_address"])} for r in _rows(cur) if r["email_address"]]
    finally:
        conn.close()

    return {
        "found": bool(sub),
        "account": account,
        "packages": packages,
        "service_items": items,
        "tasks": tasks,
        "payments": payments,
        "credits": credits,
        "tickets": tickets,
        "phones": phones,
        "emails": emails,
        "has_billing_tables": not t["is_va"],
    }


# ----------------------------------------------------------------- appdb

CALLS_SHOWN = 100


def _call_numbers(phones):
    """Every way HubSpot may have stored these phones: +1XXXXXXXXXX
    (almost all calls) and 1XXXXXXXXXX (a few dozen)."""
    numbers = set()
    for phone in phones:
        number = phone_e164(phone)
        if number:
            numbers.update((number, number[1:]))
    return sorted(numbers)


def _load_calls(phones, emails):
    """HubSpot calls with this customer (widened 2026-10-27, by request):
    to or from any of their phone numbers (order phone, SMS phone, Vision
    phones), or logged on a HubSpot contact with any of their emails.
    Newest CALLS_SHOWN, plus totals over all of them."""
    numbers = _call_numbers(phones)
    emails = sorted({e.strip().lower() for e in emails if e and e.strip()})
    if not numbers and not emails:
        return {"calls": [], "total": 0, "outbound": 0, "inbound": 0, "last_call": None}
    conn = db.get_appdb_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT portal_id, ui_domain FROM hubspot_sync_state WHERE id = 1")
            portal_id, ui_domain = cur.fetchone() or (None, None)
            cur.execute(
                """
                WITH matched AS (
                    SELECT c.*, (c.to_number = ANY(%(numbers)s) OR c.from_number = ANY(%(numbers)s)) AS by_phone
                    FROM hubspot_calls c
                    LEFT JOIN hubspot_contacts hc ON hc.contact_id = c.contact_id
                    WHERE c.to_number = ANY(%(numbers)s) OR c.from_number = ANY(%(numbers)s)
                       OR lower(trim(hc.email)) = ANY(%(emails)s)
                )
                SELECT c.call_timestamp, c.direction, c.duration_ms, c.title, c.recording_url, c.contact_id, c.by_phone,
                       COALESCE(d.label, c.disposition) AS outcome,
                       COALESCE(sr.name, m.hubspot_name) AS rep,
                       count(*) OVER () AS total,
                       count(*) FILTER (WHERE c.direction = 'OUTBOUND') OVER () AS outbound,
                       count(*) FILTER (WHERE c.direction = 'INBOUND') OVER () AS inbound
                FROM matched c
                LEFT JOIN hubspot_dispositions d ON d.disposition_id = c.disposition
                LEFT JOIN hubspot_user_mappings m ON m.hubspot_owner_id = c.hubspot_owner_id
                LEFT JOIN sales_reps sr ON sr.id = m.sales_rep_id
                ORDER BY c.call_timestamp DESC NULLS LAST
                LIMIT %(limit)s
                """,
                {"numbers": numbers, "emails": emails, "limit": CALLS_SHOWN},
            )
            rows = _rows(cur)
    finally:
        conn.close()
    calls = [{
        "when": fmt_datetime(r["call_timestamp"].astimezone(EASTERN_TZ)) if r["call_timestamp"] else None,
        "direction": (r["direction"] or "").title() or None,
        "duration": f"{int(r['duration_ms'] // 60000)}m {int(r['duration_ms'] // 1000 % 60)}s" if r["duration_ms"] else None,
        "outcome": r["outcome"], "rep": r["rep"], "title": r["title"],
        "matched_by": "Phone" if r["by_phone"] else "Email",
        "recording_url": hubspot_logic.safe_https_url(r["recording_url"]),
        "contact_url": hubspot_logic.hubspot_contact_url(ui_domain, portal_id, r["contact_id"]),
    } for r in rows]
    first = rows[0] if rows else {}
    return {
        "calls": calls,
        "total": int(first.get("total") or 0),
        "outbound": int(first.get("outbound") or 0),
        "inbound": int(first.get("inbound") or 0),
        "last_call": calls[0]["when"] if calls else None,
    }


# ------------------------------------------------------------------ main

def load_customer_profile(sale_id, state, subscriber_uuid, phone, include_calls):
    """Everything for one customer. `errors` maps a source name
    ("planetweb", "vision", "attention", "calls") to a short message when
    that source failed -- the page shows a notice in its place."""
    errors = {}
    profile = {"order": None, "vision": None, "attention": None, "calls": None, "errors": errors}

    try:
        profile["order"] = _load_planetweb(sale_id)
    except Exception as exc:
        errors["planetweb"] = db.sanitize_error(exc)

    order = profile["order"] or {}
    raw = order.get("raw") or {}
    subscriber_uuid = _clean(subscriber_uuid) or _clean(raw.get("vi_subscriber_uuid"))
    state = _clean(state) or _clean(raw.get("State"))
    phone = _clean(phone) or _clean(raw.get("PhoneNumber"))

    if subscriber_uuid:
        try:
            profile["vision"] = _load_vision(subscriber_uuid, state)
        except Exception as exc:
            errors["vision"] = db.sanitize_error(exc)

    available, status_map, notes_map, _progress = attention_store.get_attention_overview([sale_id])
    if available:
        since = needs_attention_service.get_needs_attention_since(sale_id)
        notes = [
            {**n, "created": fmt_datetime(n["created_at"].astimezone(EASTERN_TZ)) if n.get("created_at") else None}
            for n in notes_map.get(int(sale_id), [])
        ]
        profile["attention"] = {
            "status": status_map.get(int(sale_id)),
            "notes": notes,
            "since": fmt_date(since) if since else None,
        }
    else:
        errors["attention"] = "Attention status and notes are temporarily unavailable."

    if include_calls:
        try:
            vision = profile["vision"] or {}
            profile["calls"] = _load_calls(
                [phone, raw.get("SMSPhoneNumber")] + [p["value"] for p in vision.get("phones") or []],
                [raw.get("EmailAddress")] + [e["value"] for e in vision.get("emails") or []],
            )
        except Exception as exc:
            errors["calls"] = db.sanitize_error(exc)

    return profile
