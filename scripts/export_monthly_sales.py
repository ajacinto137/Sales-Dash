"""Exports every order placed in a given month to CSV -- the "send me this
month's sales" report. Defaults to the current Eastern-time month (matches
_today() elsewhere in this app); pass --year/--month for a past month.

"Order" here means fd.QuoteID IS NOT NULL on FTTPFormData -- every row that
reached the quote step, regardless of whether a PAS rep or the website
(GetPlanetFiber.com) placed it. This is intentionally broader than the
dashboard's own Main Sales query, which also requires InsertByEmployeeID
IS NOT NULL and so excludes website/map-pin orders entirely.

Run inside the dashboard container (it already has pyodbc + the ODBC
driver + this repo's modules on the path):

    docker compose exec -T dashboard python3 scripts/export_monthly_sales.py > sales.csv
    docker compose exec -T dashboard python3 scripts/export_monthly_sales.py --year 2026 --month 9 > sales.csv
"""

import argparse
import os
import sys
from calendar import monthrange
from datetime import date, timedelta

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
from queries import DOOR_KNOCKER_CODES, WEB_SALES_SOURCE_TOKEN
from sales_metrics import _today, extract_rep_name, map_sales_channel

# Maps a raw FTTPFormData.SourceToken value to a human-readable order
# source, for tokens that aren't a "PAS Employee: <name>" rep token.
# Add new tokens here as the business identifies them (confirmed with
# PlanetWeb/engineering on 2026-10-22: this is the website's map-pin
# order flow).
SOURCE_TOKEN_LABELS = {
    WEB_SALES_SOURCE_TOKEN: "GetPlanetFiber.com",
}


def referral_type(referral_data):
    if not isinstance(referral_data, str) or not referral_data.strip():
        return ""
    return "Door Knocker" if referral_data.strip().lower() in DOOR_KNOCKER_CODES else "Other"

QUERY = """
SELECT
    fd.[ID] AS OrderID,
    fd.[SourceToken],
    fd.[ReferralData],
    CAST(fd.InsertDate AS date) AS DatePlaced,
    fd.[FirstName],
    fd.[LastName],
    fd.[EmailAddress],
    fd.[PhoneNumber],
    fd.[Address],
    fd.[ApartmentSuite],
    fd.[City],
    fd.[State],
    fd.[Zipcode],
    rates.ServiceName AS Package,
    rates.SpeedUp,
    rates.SpeedDown,
    fd.[SalesChannel_ID]
FROM [PlanetWeb].[dbo].[FTTPFormData] fd
JOIN FTTPRates rates
    ON rates.ID = fd.RateID
WHERE
    fd.QuoteID IS NOT NULL
    AND fd.InsertDate >= ?
    AND fd.InsertDate < ?
ORDER BY fd.ID ASC
"""


def order_placed_by(source_token, rep_name):
    if rep_name != "Unknown":
        return rep_name
    if isinstance(source_token, str) and source_token.strip():
        token = source_token.strip()
        return SOURCE_TOKEN_LABELS.get(token, token)
    return "Unknown"


def month_bounds(year, month):
    start = date(year, month, 1)
    days_in_month = monthrange(year, month)[1]
    end = date(year, month, days_in_month) + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def export(year, month, out=sys.stdout):
    start, end = month_bounds(year, month)

    conn = db.get_planetweb_connection()
    df = pd.read_sql(QUERY, conn, params=[start, end])
    conn.close()

    df["Order Placed By"] = df.apply(
        lambda r: order_placed_by(r["SourceToken"], extract_rep_name(r["SourceToken"])), axis=1
    )
    df["Referral Type"] = df["ReferralData"].apply(referral_type)
    df["Sales Channel"] = df["SalesChannel_ID"].apply(map_sales_channel)
    df["Address"] = (
        df["Address"].fillna("")
        + df["ApartmentSuite"].fillna("").apply(lambda v: f" {v}" if v else "")
        + ", " + df["City"].fillna("")
        + ", " + df["State"].fillna("")
        + " " + df["Zipcode"].fillna("")
    )
    df["Speed"] = df["SpeedDown"].astype("Int64").astype(str) + "/" + df["SpeedUp"].astype("Int64").astype(str)

    result = df[[
        "OrderID",
        "Order Placed By",
        "ReferralData",
        "Referral Type",
        "DatePlaced",
        "Address",
        "City",
        "State",
        "Zipcode",
        "Package",
        "Speed",
        "Sales Channel",
        "FirstName",
        "LastName",
        "EmailAddress",
        "PhoneNumber",
    ]].rename(columns={
        "ReferralData": "Referral Data",
        "DatePlaced": "Date Placed",
        "FirstName": "First Name",
        "LastName": "Last Name",
        "EmailAddress": "Email",
        "PhoneNumber": "Phone",
    })

    result.to_csv(out, index=False)
    print(f"Rows: {len(result)} ({start} to {end})", file=sys.stderr)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    today = _today()
    parser.add_argument("--year", type=int, default=today.year)
    parser.add_argument("--month", type=int, default=today.month)
    args = parser.parse_args()
    export(args.year, args.month)
