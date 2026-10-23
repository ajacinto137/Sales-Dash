import re

# Shared by MAIN_SALES_QUERY and WEB_SALES_QUERY -- everything but the WHERE.
_SALES_SELECT = """
SELECT
    fd.[ID],
    rates.SpeedUp AS Speed,
    fd.[FirstName],
    fd.[LastName],
    fd.[EmailAddress],
    fd.[Address],
    fd.[ApartmentSuite],
    fd.[City],
    fd.[State],
    fd.[Zipcode],
    fd.[ReferralData],
    CAST(fd.InsertDate AS date) AS Date,
    fd.InsertDate AS DateTime,
    fd.SourceToken,
    fd.[PhoneNumber],
    fd.[NJPR_municipalityName] AS Municipality,
    fd.Cc_PayBy,
    fd.[vi_subscriber_id],
    fd.[vi_subscriber_uuid],

    CASE
        WHEN EXISTS (
            SELECT 1
            FROM FTTPFormDataItems items
            JOIN FTTPRates r2
                ON r2.ID = items.RateID
            WHERE items.FormDataID = fd.ID
            AND r2.ServiceName LIKE '%Voice%'
        )
        THEN CAST(1 AS bit)
        ELSE CAST(0 AS bit)
    END AS Voice,

    CASE
        WHEN sched.SlotID IS NULL
        THEN 'Not Scheduled'
        ELSE 'Scheduled'
    END AS Scheduled,

    sched.StartDate,

    fd.[SalesChannel_ID]

FROM [PlanetWeb].[dbo].[FTTPFormData] fd

JOIN FTTPRates rates
    ON rates.ID = fd.RateID

OUTER APPLY (
    SELECT TOP 1
        jobs.SlotID,
        slots.StartDate

    FROM FTTPScheduleJobs jobs

    LEFT JOIN FTTPScheduleSlots slots
        ON slots.ID = jobs.SlotID

    WHERE jobs.FormDataID = fd.ID

    ORDER BY
        slots.StartDate DESC,
        jobs.ID DESC

) sched
"""

MAIN_SALES_QUERY = _SALES_SELECT + """
WHERE
    fd.QuoteID IS NOT NULL
    AND fd.InsertByEmployeeID IS NOT NULL
    AND fd.InsertDate >= '2026-01-01'

ORDER BY fd.ID ASC
"""

# Website (GetPlanetFiber.com map-pin) orders -- the "Web" pseudo-rep
# (added 2026-10-22, by request). MAIN_SALES_QUERY's InsertByEmployeeID
# requirement excludes these entirely, so they're loaded separately;
# InsertByEmployeeID IS NULL here keeps the two sets disjoint. Orders
# carrying a door-knocker referral code (DOOR_KNOCKER_CODES below) are
# dropped in build_sales_dataset(), not here -- those belong to External
# Rep Sales, not Web.
WEB_SALES_SOURCE_TOKEN = "ORDER_PLANET_AUGUST-2025-NewMapPin"

WEB_SALES_QUERY = _SALES_SELECT + f"""
WHERE
    fd.QuoteID IS NOT NULL
    AND fd.InsertByEmployeeID IS NULL
    AND fd.SourceToken = '{WEB_SALES_SOURCE_TOKEN}'
    AND fd.InsertDate >= '2026-01-01'

ORDER BY fd.ID ASC
"""

SERVICE_CANCELLATIONS_QUERY = """
SELECT
    vt.id,
    vt.subscriber_uuid,
    vt.completed_date::date AS completed_date,
    vt.type

FROM public.vision_tasks vt

WHERE vt.type IN (
    'Moving Outside Service Area',
    'Moved within Service Area - Did Not Stay with Planet',
    'Unsatisfied with Price',
    'Unsatisfied with Service',
    'Unsatisfied with Customer Support',
    'Other',
    'Cable Objection'
)
"""

VISION_PACKAGES_QUERY = """
SELECT
    vsp.uuid,
    vsp.subscriber_uuid,
    vsp.bill_date,
    vsp.active,
    vsp.origin_ticket_id,
    vsp.name,
    vsp.term,
    vsp.install_time,
    vsp.last_invoice_date,
    vsp.transaction_date,
    vsp.mailed_invoice,
    vsp.subscriber_mailing_address_uuid,
    vsp.emailed_invoice,
    vsp.subscriber_location_uuid,
    vsp.package_uuid,
    vsp.contract_confirmed,
    vsp.subscriber_package_contract_uuid,
    vsp.original_package_link_uuid,
    vsp.new_package_link_uuid,
    vsp.lat,
    vsp.lng,
    vsp.geom,
    vsp.cohort_id,
    vsp.first_invoice_date,
    vsp.subscriber_first_invoice_date,
    vsp.estimated_cancellation_date,
    'NJ/NY' AS market

FROM public.vision_subscribers_packages vsp

JOIN public.vision_packages vp
    ON vp.uuid = vsp.package_uuid

WHERE
    vp.reportable_gpon = true
    AND vsp.first_invoice_date IS NOT NULL

UNION ALL

SELECT
    vsp.uuid,
    vsp.subscriber_uuid,
    vsp.bill_date,
    vsp.active,
    vsp.origin_ticket_id,
    vsp.name,
    vsp.term,
    vsp.install_time,
    vsp.last_invoice_date,
    vsp.transaction_date,
    vsp.mailed_invoice,
    vsp.subscriber_mailing_address_uuid,
    vsp.emailed_invoice,
    vsp.subscriber_location_uuid,
    vsp.package_uuid,
    vsp.contract_confirmed,
    vsp.subscriber_package_contract_uuid,
    vsp.original_package_link_uuid,
    vsp.new_package_link_uuid,
    vsp.lat,
    vsp.lng,
    vsp.geom,
    vsp.cohort_id,
    vsp.first_invoice_date,
    vsp.subscriber_first_invoice_date,
    vsp.estimated_cancellation_date,
    'VA' AS market

FROM public.virginia_vision_subscribers_packages vsp

JOIN public.virginia_vision_packages vp
    ON vp.uuid = vsp.package_uuid

WHERE
    vp.reportable_gpon = true
    AND vsp.first_invoice_date IS NOT NULL
"""

# External Rep Sales (Admin Portal > External Rep Sales). Adapted verbatim
# from Refresh_Main() in the original VBA workbook, plus 46 referral codes
# the business added 2026-10-20 -- the referral codes and excluded order
# IDs below are the source of truth; don't edit them without the business
# confirming the change. SQL Server's collation is case-insensitive, so a
# code also matches differently-cased ReferralData (e.g. 'Tseneca'). Synced into appdb by
# external_sales_store.py, never queried per page load.
EXTERNAL_REP_SALES_QUERY = """
SELECT
    fd.[ID],
    rates.SpeedUp AS Speed,
    fd.[FirstName],
    fd.[LastName],
    fd.[EmailAddress],
    fd.[Address],
    fd.[ApartmentSuite],
    fd.[City],
    fd.[State],
    fd.[Zipcode],
    fd.[ReferralData],
    CAST(fd.InsertDate AS date) AS Date,
    fd.Q1,
    fd.[PhoneNumber],
    fd.[NJPR_municipalityName] AS Municipality,
    fd.Cc_PayBy,
    fd.[vi_subscriber_id],
    fd.[vi_subscriber_uuid],

    CASE
        WHEN EXISTS (
            SELECT 1
            FROM FTTPFormDataItems items
            JOIN FTTPRates r2
                ON r2.ID = items.RateID
            WHERE items.FormDataID = fd.ID
              AND r2.ServiceName LIKE '%Voice%'
        )
        THEN CAST(1 AS bit)
        ELSE CAST(0 AS bit)
    END AS Voice,

    CASE
        WHEN sched.SlotID IS NULL
            THEN 'Not Scheduled'
        ELSE 'Scheduled'
    END AS Scheduled,

    sched.StartDate

FROM [PlanetWeb].[dbo].[FTTPFormData] fd

JOIN FTTPRates rates
    ON rates.ID = fd.RateID

OUTER APPLY (
    SELECT TOP 1
        jobs.SlotID,
        slots.StartDate
    FROM FTTPScheduleJobs jobs
    LEFT JOIN FTTPScheduleSlots slots
        ON slots.ID = jobs.SlotID
    WHERE jobs.FormDataID = fd.ID
    ORDER BY
        slots.StartDate DESC,
        jobs.ID DESC
) sched

WHERE fd.QuoteID IS NOT NULL

AND fd.ReferralData IN (
    'mritchie',
    'dpond',
    'bthompson',
    'agarcia',
    'jberg',
    'tnofsinger',
    'jsmallwood',
    'pleland',
    'jsherrow',
    'jmcglothlin',
    'avicari',
    'nsvinth',
    'amcglothlin',
    'mfritz',
    'pnielson',
    'jkamp',
    'lwilliamson',
    'mfontana',
    'ccovington',
    'ccarman',
    'jtownsend',
    'smaloney',
    'obrown',
    'jsunstrum',
    'bdavis',
    'dharacic',
    'eharacic',
    'jmortensen',
    'dthompson',
    'msanchez',
    'jkenny',
    'llopez',
    'jescobar',
    'myoung',
    'csessions',
    'cvest',
    'amorgan',
    'iprice',
    'ljackson',
    'asharber',
    'jjamison',
    'mbustamante',
    'sbustamante',
    'twells',
    'bstevens',
    'jsparks',
    'lbrossard',
    'ehendricks',
    'efreeman',
    'plavender',
    'psadoyama',
    'broskelley',
    'jcampbell',
    'acampbell',
    'clyman',
    'etarr',
    'pwabel',
    'bdufur',
    'ndavis',
    'djones',
    'lfurrow',
    'ttaylor',
    'repperson',
    'bmickelsen',
    'hseats',
    'oreed',
    'dbechtol',
    'tplacencio',
    'mmartinez',
    'wmortensen',
    'taldridge',
    'mlavender',
    'pstolley',
    'clebaron',
    'kshamy',
    'hchecketts',
    'mwagner',
    'gbaldauf',
    'dwagner',
    'jtinney',
    'plee',
    'hbranam',
    'kfredekind',
    'mastle',
    'dhardy',
    'jcaudillo',
    'mastle2',
    'ethompson',
    'thill',
    'ttah',
    'joakley',

    -- Added 2026-10-20 by request: the business-provided list of 137
    -- codes, which already included every code above.
    'tseneca',
    'tvaldez',
    'elopez',
    'dritchie',
    'dbailey',
    'shale',
    'dmestas',
    'chumpherys',
    'chumpherys2',
    'dgilbert',
    'mloveland',
    'bwhitby',
    'lzepeda',
    'bgreenfield',
    'clindberg',
    'jromander',
    'jsutton',
    'dclark',
    'grubbelke',
    'karias',
    'jcole',
    'rstokes',
    'imartinez',
    'hmccleve',
    'tbaker',
    'arodriguez',
    'athompson',
    'sbailey',
    'gnilsen',
    'anasir',
    'bzaugg',
    'wwomack',
    'lriley',
    'jdye',
    'jfuchs',
    'ckarpowitz',
    'daldridge',
    'hashworth',
    'jdayton',
    'nhinderliter',
    'swest',
    'ssimmons',
    'bfadness',
    'mmendieta',
    'shenrik',
    'bgill'
)

AND fd.ID NOT IN (
    '201443',
    '203769',
    '214436'
)

ORDER BY fd.ID ASC
"""

# Order cancellations (External Rep Sales' "Order Cancellation Date").
# PlanetWeb has no order-level cancel field; an order counts as cancelled
# when one of its install jobs was set to status Canceled (StatusID 16,
# which exists since 2026-03-24 -- earlier cancellations can't be told
# apart from reschedules, StatusID 15) and it has no live, non-cancelled
# job since (i.e. it wasn't rebooked). Cancelled jobs are moved to
# FTTPScheduleJobsCanceled (CanceledDate); a few stay in FTTPScheduleJobs
# with the status set (StatusDate). Latest cancellation per order.
EXTERNAL_ORDER_CANCELLATIONS_QUERY = """
SELECT
    c.FormDataID AS OrderID,
    CAST(MAX(c.CancelDate) AS date) AS OrderCancellationDate
FROM (
    SELECT FormDataID, CanceledDate AS CancelDate
    FROM FTTPScheduleJobsCanceled
    WHERE StatusID = 16

    UNION ALL

    SELECT FormDataID, StatusDate
    FROM FTTPScheduleJobs
    WHERE StatusID = 16
) c
WHERE NOT EXISTS (
    SELECT 1
    FROM FTTPScheduleJobs live
    WHERE live.FormDataID = c.FormDataID
      AND live.StatusID <> 16
)
GROUP BY c.FormDataID
"""


def _door_knocker_codes():
    """The door-knocker (External Rep Sales) referral codes, lowercased,
    parsed out of EXTERNAL_REP_SALES_QUERY's `ReferralData IN (...)` list
    -- that list is the source of truth, so this never hand-copies it.
    Confirmed by the business 2026-10-22: these first-initial+lastname
    codes identify door-to-door reps, unlike promo codes such as
    'welcometoplanet'. Lowercased to match SQL Server's case-insensitive
    collation."""
    match = re.search(
        r"ReferralData IN \((.*?)\)\s*\n\s*AND fd\.ID NOT IN",
        EXTERNAL_REP_SALES_QUERY,
        re.DOTALL,
    )
    return {code.lower() for code in re.findall(r"'([^']+)'", match.group(1))}


DOOR_KNOCKER_CODES = _door_knocker_codes()
