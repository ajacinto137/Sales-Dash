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
