"""Iteration 2 — Phase B data layer: config accessor + schema reshape.

Pure-Python, offline. No DB, Gemini, Telegram, or AWS is touched — the
ORM models are inspected via SQLAlchemy metadata, and the config accessor
reads the checked-in config.yaml.
"""

from __future__ import annotations

import pytest

from src.config import Settings, settings


# ---------------------------------------------------------------------------
# Config accessor + operator identity (instance-ready, NFR-11)
# ---------------------------------------------------------------------------


def test_operator_block_present_and_personal_gone() -> None:
    assert settings.operator.full_name
    assert "personal" not in settings  # renamed to operator/filters/salary
    assert settings.filters.years_ceiling == 5
    assert settings.filters.job_type == "fulltime"
    assert settings.salary.default_expected_lpa == 6.0


def test_attribute_access_is_recursive() -> None:
    # Dotted access works several levels deep (CLAUDE.md convention).
    assert settings.scoring.final.fit == 0.55
    assert isinstance(settings.filters.disallowed_regions, list)


def test_missing_key_raises_loudly() -> None:
    with pytest.raises(AttributeError):
        _ = settings.operator.does_not_exist


def test_derived_filenames_match_full_name() -> None:
    # No operator literal in source — names derive from config.operator.
    slug = "_".join(settings.operator.full_name.split())
    assert settings.resume_filename == f"{slug}_Resume.pdf"
    assert settings.cover_filename == f"{slug}_Cover_Letter.pdf"


def test_filename_derivation_is_operator_agnostic(tmp_path) -> None:
    # A different operator's config yields their filenames, zero code edits.
    cfg = tmp_path / "config.yaml"
    cfg.write_text('operator:\n  full_name: "Jane Q Doe"\n')
    other = Settings(path=cfg)
    assert other.resume_filename == "Jane_Q_Doe_Resume.pdf"
    assert other.cover_filename == "Jane_Q_Doe_Cover_Letter.pdf"


def test_missing_full_name_fails_fast(tmp_path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text('operator:\n  full_name: ""\n')
    with pytest.raises(RuntimeError, match="operator.full_name"):
        Settings(path=cfg)


# ---------------------------------------------------------------------------
# Schema reshape (architecture §7.3)
# ---------------------------------------------------------------------------


def test_applied_columns_reshaped() -> None:
    from src.state.models import Applied

    cols = set(Applied.__table__.columns.keys())
    # New durable-selection columns present...
    assert {"selection_json", "template_version", "notified_at",
            "user_status", "built_at"} <= cols
    # ...and every auto-apply column is gone.
    assert cols.isdisjoint({
        "apply_type", "resume_s3_uri", "resume_s3_key", "cover_letter_used",
        "cover_letter_s3_uri", "application_status", "failure_reason",
        "applied_at",
    })


def test_all_jobs_has_near_duplicate_columns() -> None:
    from src.state.models import AllJobs

    cols = set(AllJobs.__table__.columns.keys())
    assert {"jd_embedding", "near_duplicate_of"} <= cols


def test_near_duplicate_of_is_self_referential_fk() -> None:
    from src.state.models import AllJobs

    fks = AllJobs.__table__.c.near_duplicate_of.foreign_keys
    assert len(fks) == 1, "near_duplicate_of must have exactly one FK"
    fk = next(iter(fks))
    # Self-referential: points back at all_jobs.job_id.
    assert fk.column.table.name == "all_jobs"
    assert fk.column.name == "job_id"


def test_render_cache_model() -> None:
    from src.state.models import RenderCache

    cols = set(RenderCache.__table__.columns.keys())
    assert {"cache_key", "job_id", "format", "template_version", "s3_uri",
            "created_at", "expires_at"} == cols
    assert RenderCache.__table__.primary_key.columns.keys() == ["cache_key"]


def test_removed_models_not_importable() -> None:
    import src.state.models as m

    for gone in ("ApplicationQueue", "AnswerBank", "PendingReview"):
        assert not hasattr(m, gone), f"{gone} should have been removed"
