"""Tests for the Sales Channel field on the Customer Profile.

Run with: pytest tests/test_customer_profile_channel.py -v
"""

import customer_profile as cp


def test_recorded_channel_is_kept():
    assert cp._sales_channel({"SalesChannelName": "Outbound Call", "InsertByEmployeeID": 262}) == "Outbound Call"


def test_internal_rep_with_no_channel_is_door_to_door():
    assert cp._sales_channel({"SalesChannelName": None, "InsertByEmployeeID": 262}) == "Door to Door"


def test_website_order_with_no_channel_stays_blank():
    assert cp._sales_channel({"SalesChannelName": None, "InsertByEmployeeID": None}) is None
