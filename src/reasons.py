"""`not_applied.reason_category` constants (architecture §7.4).

Centralised so every layer uses the same strings and CloudWatch metric
filters (Iteration 3) match reliably. Auto-apply reasons (APPLY_FAILURE,
MANUAL_REQUIRED, REJECTED_BY_USER, STALE) were removed in the pivot.
"""

from __future__ import annotations

# Layer 2 — rejected at scrape on raw/structured-at-scrape data
HARD_FILTER_LAYER_2 = "HARD_FILTER_LAYER_2"
LOCATION_DISALLOWED = "LOCATION_DISALLOWED"
COMPANY_COOLDOWN = "COMPANY_COOLDOWN"
DUPLICATE = "DUPLICATE"
# Company on filters.company_blocklist.
COMPANY_BLOCKED = "COMPANY_BLOCKED"
# Title names a rejected employment type (filters.title_blocklist).
TITLE_DISALLOWED = "TITLE_DISALLOWED"
# Too many applicants for even a best-case fit to clear the threshold.
TOO_MANY_APPLICANTS = "TOO_MANY_APPLICANTS"
# LinkedIn says the posting no longer accepts applications (backlog refresh).
JOB_CLOSED = "JOB_CLOSED"
# Listing arrived with no description body (e.g. a throttled LinkedIn
# description fetch). Can't parse or score, and an empty-text embedding is
# identical to every other empty one — which collapses near-duplicate
# detection — so drop it before embedding.
EMPTY_JD = "EMPTY_JD"

# Layer 3 — rejected after Gemini parse
HARD_FILTER_LAYER_3 = "HARD_FILTER_LAYER_3"
JOB_TYPE_DISALLOWED = "JOB_TYPE_DISALLOWED"
PARSE_FAILURE = "PARSE_FAILURE"

# Layer 4 / 5 — scoring + build outcomes
LOW_SCORE = "LOW_SCORE"
BUILD_FAILURE = "BUILD_FAILURE"
