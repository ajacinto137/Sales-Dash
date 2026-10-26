"""Every PlanetWeb job query the dashboard counts from is limited to the
fiber install job types (queries.FIBER_INSTALL_JOB_TYPE_IDS) -- a service
call or separate phone port must never stand in for an install.

Run with: pytest tests/test_fiber_install_jobs.py -v
"""

import pytest

import install_escalation
import queries

FILTER = "TypeID IN (1, 10, 15)"


def test_fiber_install_types_are_fiber_install_and_phone_port_combo_only():
    assert queries.FIBER_INSTALL_JOB_TYPE_IDS == (1, 10, 15)


@pytest.mark.parametrize("query, expected", [
    (queries.MAIN_SALES_QUERY, 1),
    (queries.WEB_SALES_QUERY, 1),
    (queries.EXTERNAL_REP_SALES_QUERY, 1),
    # cancelled jobs, cancelled-in-place jobs, and the "rebooked" check
    (queries.EXTERNAL_ORDER_CANCELLATIONS_QUERY, 3),
    (install_escalation.READY_JOBS_QUERY, 1),
])
def test_job_queries_filter_to_fiber_installs(query, expected):
    assert query.count(FILTER) == expected
