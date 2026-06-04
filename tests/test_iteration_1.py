"""Iteration 1 — end-to-end skeleton tests.

Each iteration's behaviour gets its own test file. Iter 1 tests are
offline (mocks for DB and Telegram). The real end-to-end run against
Neon happens as the acceptance check, not in pytest.
"""

from __future__ import annotations


# Layers 2 (scraper), 3 (parser), and 4 (scorer) all became real in Iteration
# 2 and are covered by test_iteration_2_scraper.py / _parser.py / _scorer.py.
# The Iter-1 reject-all decide() stub is gone (replaced by the real
# evaluate()). What remains here is the Layer-7 models-import smoke check.


# ---------------------------------------------------------------------------
# Layer 7 — models import
# ---------------------------------------------------------------------------


def test_all_models_importable() -> None:
    from src.state.models import (  # noqa: F401
        AllJobs,
        Applied,
        CompanyCooldown,
        MasterBullets,
        MasterMeta,
        MasterSummaries,
        MasterTitleAliases,
        NotApplied,
        PortalHealth,
        ProcessingQueue,
        SearchRotationState,
    )
