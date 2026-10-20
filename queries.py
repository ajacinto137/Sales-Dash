MAIN_SALES_QUERY = """
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

WHERE
    fd.QuoteID IS NOT NULL
    AND fd.InsertByEmployeeID IS NOT NULL
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
# from Refresh_Main() in the original VBA workbook -- the referral codes
# and excluded order IDs below are the source of truth; don't edit them
# without the business confirming the change. Synced into appdb by
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
    'joakley'
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
