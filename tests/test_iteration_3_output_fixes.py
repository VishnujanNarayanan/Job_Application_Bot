"""Layer 3/6/8 output-quality fixes found by auditing a real run (2026-08-19)."""

from src.notifications import _usable_apply_url
from src.parser import term_shaped
from src.scraper.filters import job_type_disallowed


class TestJobTypeFilter:
    """`filters.job_type` was configured but never read by any code path."""

    def test_rejects_non_matching_type(self):
        assert job_type_disallowed("contract", "fulltime")
        assert job_type_disallowed("internship", "fulltime")

    def test_allows_matching_type_case_insensitively(self):
        assert not job_type_disallowed("fulltime", "fulltime")
        assert not job_type_disallowed(" FullTime ", "fulltime")

    def test_unknown_type_passes(self):
        # Null on ~25% of ads, usually because the ad never says. Rejecting
        # those would discard more real full-time jobs than contracts caught.
        assert not job_type_disallowed(None, "fulltime")

    def test_no_configured_preference_disables_the_filter(self):
        assert not job_type_disallowed("contract", None)
        assert not job_type_disallowed("contract", "")


class TestUsableApplyUrl:
    """A bad apply_url REPLACES job_url, so it must be discarded, not passed."""

    def test_email_is_rejected(self):
        assert _usable_apply_url(" hiring@example.com") == ""

    def test_schemeless_host_is_rejected(self):
        assert _usable_apply_url("www.example.com/jobs") == ""

    def test_http_and_https_survive_surrounding_whitespace(self):
        assert _usable_apply_url("  https://x.com/1 ") == "https://x.com/1"
        assert _usable_apply_url("http://x.com/1") == "http://x.com/1"

    def test_missing_url_is_empty(self):
        assert _usable_apply_url(None) == ""


class TestTermShaped:
    """Length is enforced by the JSON schema; this catches the short residue."""

    def test_keeps_real_skills_including_multiword_names(self):
        skills = ["Python", "AWS", "Google Cloud Platform", "vector databases"]
        assert term_shaped(skills) == skills

    def test_drops_degree_requirements(self):
        assert term_shaped(["Bachelor's degree"]) == []
        assert term_shaped(["B.Tech in a related field"]) == []

    def test_drops_years_of_experience_clauses(self):
        assert term_shaped(["3+ years experience"]) == []
        assert term_shaped(["5 years of experience"]) == []

    def test_drops_legal_boilerplate(self):
        # Short enough to clear the 30-char schema bound, still not a skill.
        assert term_shaped(["Equal Opportunity Employer"]) == []

    def test_drops_blank_entries(self):
        assert term_shaped(["", "   "]) == []

    def test_no_longer_drops_on_length_alone(self):
        # The old word-count cut deleted entries that contained the tech names
        # and kept boilerplate. The schema bound replaced it.
        assert term_shaped(["AI orchestration frameworks"]) == ["AI orchestration frameworks"]


class TestSkillTermBound:
    """The bound the model physically cannot exceed."""

    def test_schema_carries_max_length(self):
        from src.llm.schemas import JDParsed

        items = JDParsed.model_json_schema()["properties"]["required_skills"]["items"]
        assert items["maxLength"] == 30

    def test_over_length_skill_is_rejected(self):
        import pytest
        from pydantic import ValidationError

        from src.llm.schemas import JDParsed

        with pytest.raises(ValidationError):
            JDParsed(
                role_summary="x", role_category="ml", role_level="mid", years_required=2,
                required_skills=["Experience with Docker, Kubernetes, CI/CD and MLOps practices"],
            )


class TestSampling:
    """temperature is passed when configured and omitted when not."""

    def test_configured_temperature_is_sent(self):
        from src.config import Section
        from src.llm.client import _sampling

        assert _sampling(Section({"temperature": 0})) == {"temperature": 0.0}

    def test_absent_temperature_is_omitted(self):
        from src.config import Section
        from src.llm.client import _sampling

        assert _sampling(Section({})) == {}
