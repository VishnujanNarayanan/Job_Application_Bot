"""Layer 3/6/8 output-quality fixes found by auditing a real run (2026-08-19)."""

from src.notifications import _usable_apply_url
from src.llm.schemas import _as_terms
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


class TestBoilerplateRejection:
    """Requirement boilerplate is rejected before it can be split into
    plausible-looking fragments."""

    def test_degree_line_yields_nothing(self):
        # Splitting this first would leave "Engineering", "Data Science", "AI"
        # looking like skills the ad asked for. It didn't.
        assert _as_terms(
            "Bachelor's or Master's degree in Computer Science, Engineering, "
            "Data Science, AI, or a related field"
        ) == []

    def test_years_of_experience_yields_nothing(self):
        assert _as_terms("3+ years of experience building ML solutions") == []

    def test_legal_boilerplate_yields_nothing(self):
        assert _as_terms("Equal Opportunity Employer") == []

    def test_real_skills_containing_degree_words_survive(self):
        """The reject-before-split runs on the whole bullet, so a false
        positive costs every technology in it — these must not match."""
        assert _as_terms("Master Data Management") == ["Master Data Management"]
        assert "Snowflake" in _as_terms(
            "Experience with Master Data Management and Snowflake"
        )
        assert _as_terms("master-slave replication") == ["master-slave replication"]
        assert _as_terms("Degree of parallelism tuning") == ["Degree of parallelism tuning"]

    def test_degree_phrasings_are_all_rejected(self):
        assert _as_terms("Masters of Business Administration") == []
        assert _as_terms("Bachelor of Science in Computer Science") == []
        assert _as_terms("B.Tech in CS") == []
        assert _as_terms("PhD preferred") == []

    def test_bare_qualifier_is_dropped(self):
        # From "or similar frameworks", once the noun is split away.
        assert "similar" not in _as_terms(
            "Experience building APIs using FastAPI or similar frameworks"
        )


class TestSkillTermBound:
    """The bound is advertised in the schema and made true by a validator.

    Ollama does NOT enforce `maxLength` in its decoding grammar (measured
    2026-08-19: the full schema returned over-long strings and the parse died),
    so the bound cannot be relied on as a hard constraint — the validator has
    to hold it up.
    """

    def _parsed(self, skills):
        from src.llm.schemas import JDParsed

        return JDParsed(
            role_summary="x", role_category="ml", role_level="mid",
            years_required=2, required_skills=skills,
        )

    def test_schema_advertises_max_length(self):
        from src.llm.schemas import _MAX_SKILL_CHARS, JDParsed

        items = JDParsed.model_json_schema()["properties"]["required_skills"]["items"]
        assert items["maxLength"] == _MAX_SKILL_CHARS

    def test_bound_clears_real_certifications(self):
        """The bound was 30 and cut through real skills. A bracketed short form
        is kept alongside the long one, which also brings both inside the
        bound — "Docker Certified Associate (DCA)" is 32 chars as one string."""
        assert _as_terms("Kubernetes Application Developer") == [
            "Kubernetes Application Developer"
        ]
        assert _as_terms("Docker Certified Associate (DCA)") == [
            "Docker Certified Associate", "DCA"
        ]
        assert _as_terms("NLU (Natural Language Understanding)") == [
            "NLU", "Natural Language Understanding"
        ]

    def test_parenthesised_lists_are_split_separately(self):
        """A parenthesis is a list, not a separator. Splitting through one
        truncated its contents mid-bracket and lost the technologies."""
        assert _as_terms(
            "Hands-on model fine-tuning experience (LoRA, QLoRA, SFT, DPO)"
        ) == ["model fine-tuning", "LoRA", "QLoRA", "SFT", "DPO"]
        assert _as_terms(
            "Experience with distributed training frameworks (DeepSpeed, FSDP)"
        ) == ["distributed training", "DeepSpeed", "FSDP"]

    def test_bound_still_rejects_prose(self):
        assert _as_terms("Experience integrating with third-party APIs and services") == []

    def test_over_length_blob_is_split_not_rejected(self):
        # Dropping this entry would throw away four real technologies.
        parsed = self._parsed(
            ["Experience with Docker, Kubernetes, CI/CD pipelines, and MLOps practices"]
        )
        assert "Docker" in parsed.required_skills
        assert "Kubernetes" in parsed.required_skills
        assert "MLOps" in parsed.required_skills

    def test_lead_in_is_stripped(self):
        assert self._parsed(["Experience with Docker"]).required_skills == ["Docker"]
        assert self._parsed(["Familiarity with CI/CD"]).required_skills == ["CI/CD"]

    def test_slash_terms_survive_splitting(self):
        # CI/CD and ECS/EKS are single technologies, not two each.
        parsed = self._parsed(["Experience with CI/CD and ECS/EKS"])
        assert "CI/CD" in parsed.required_skills
        assert "ECS/EKS" in parsed.required_skills

    def test_duplicates_are_collapsed(self):
        parsed = self._parsed(["Python", "python", "Experience with Python"])
        assert parsed.required_skills == ["Python"]

    def test_programming_skills_lead_in_is_stripped(self):
        # "Strong programming skills in Python" is 35 chars: without stripping
        # the lead-in it exceeds the bound and Python is lost from an ML ad.
        assert self._parsed(
            ["Strong programming skills in Python and SQL"]
        ).required_skills == ["Python", "SQL"]

    def test_generic_tail_is_stripped_to_the_bare_term(self):
        parsed = self._parsed(
            ["Experience with Docker, CI/CD pipelines, AWS services, RAG architectures"]
        )
        assert "CI/CD" in parsed.required_skills
        assert "AWS" in parsed.required_skills
        assert "RAG" in parsed.required_skills

    def test_nice_to_have_drops_what_required_already_says(self):
        from src.llm.schemas import JDParsed

        parsed = JDParsed(
            role_summary="x", role_category="ml", role_level="mid", years_required=2,
            required_skills=["Python", "Docker"], nice_to_have=["Docker", "Kafka"],
        )
        assert parsed.nice_to_have == ["Kafka"]

    def test_every_surviving_skill_is_within_the_bound(self):
        parsed = self._parsed(
            ["Experience with AWS services such as SageMaker, S3, Lambda, RDS and DynamoDB"]
        )
        assert parsed.required_skills, "must not empty the list"
        from src.llm.schemas import _MAX_SKILL_CHARS

        assert all(len(s) <= _MAX_SKILL_CHARS for s in parsed.required_skills)


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
