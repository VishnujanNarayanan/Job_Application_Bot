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
NEAR_DUPLICATE = "NEAR_DUPLICATE"

# Layer 3 — rejected after Gemini parse
HARD_FILTER_LAYER_3 = "HARD_FILTER_LAYER_3"
ROLE_MISMATCH = "ROLE_MISMATCH"
PARSE_FAILURE = "PARSE_FAILURE"

# Layer 4 / 5 — scoring + build outcomes
LOW_SCORE = "LOW_SCORE"
BUILD_FAILURE = "BUILD_FAILURE"
