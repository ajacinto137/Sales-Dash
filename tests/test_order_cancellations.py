"""Order cancellations on the main dataset (2026-10-30): an order whose
fiber install job was cancelled and never rebooked is Cancelled, not Needs
Attention (an order that never had an install job stays Needs Attention),
and every order lands in exactly one of Installed /
Pending / Needs Attention / Cancelled, so those columns add up to Sales."""

import pandas as pd

import sales_metrics as sm


def main_sales(rows):
    return pd.DataFrame([
        {
            "ID": order_id,
            "Date": "2026-10-01",
            "SourceToken": "PAS Employee: Amy Rep",
            "SalesChannel_ID": None,
            "Install Status": install_status,
            "StartDate": start,
            "Scheduled": None,
            "OrderCancellationDate": cancel,
        }
        for order_id, install_status, start, cancel in rows
    ])


def test_order_cancellation_is_cancelled_not_needs_attention():
    df = sm.build_sales_dataset(main_sales([
        (1, None, None, "2026-10-05"),          # cancelled, no date
        (2, None, "2026-10-02", "2026-10-05"),  # cancelled, past date
        (3, None, None, None),                  # never had a job -> Needs Attention
        (6, None, "2026-10-02", None),          # date passed -> Needs Attention
        (4, None, "2099-01-01", None),          # Pending
        (5, "Installed", "2026-10-02", "2026-10-05"),  # installed wins
    ]), None, None)
    assert dict(zip(df["sale_id"], df["status"])) == {
        1: "Cancelled", 2: "Cancelled", 3: "Needs Attention", 4: "Pending", 5: "Installed", 6: "Needs Attention",
    }


def test_status_columns_add_up_to_sales():
    df = sm.build_sales_dataset(main_sales([
        (1, None, None, "2026-10-05"),
        (2, None, "2026-10-02", None),
        (3, None, "2099-01-01", None),
        (4, "Installed", "2026-10-02", None),
    ]), None, None)
    row = sm.calculate_rep_metrics(df)[0]
    assert (row["installs"], row["pending"], row["needs_attention"], row["cancels"]) == (1, 1, 1, 1)
    assert row["installs"] + row["pending"] + row["needs_attention"] + row["cancels"] == row["sales"]


def test_missing_cancellation_column_falls_back_to_needs_attention():
    sales = main_sales([(1, None, "2026-10-02", "2026-10-05")]).drop(columns="OrderCancellationDate")
    assert sm.build_sales_dataset(sales, None, None)["status"].tolist() == ["Needs Attention"]
