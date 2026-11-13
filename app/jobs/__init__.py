"""Job registry.

Only jobs that are actually implemented are registered, so the scheduler never
advertises work it cannot do.
"""

from __future__ import annotations

from app.jobs.claim_readiness import check_claim_readiness
from app.jobs.costs import refresh_costs
from app.jobs.daily_summary import send_daily_summary
from app.jobs.detect_incoming import detect_incoming
from app.jobs.discover import discover_projects
from app.jobs.eligibility import check_eligibility
from app.jobs.probe_faucets import probe_faucets
from app.jobs.retention import apply_retention
from app.scheduler import JobFn

JOBS: dict[str, JobFn] = {
    "probe_faucets": probe_faucets,
    "discover": discover_projects,
    "eligibility": check_eligibility,
    "claim_readiness": check_claim_readiness,
    "detect_incoming": detect_incoming,
    "costs": refresh_costs,
    "daily_summary": send_daily_summary,
    "retention": apply_retention,
}

