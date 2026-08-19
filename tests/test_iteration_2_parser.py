"""Iteration 2 — Phase B Layer 3: JD parser (Gemini Call 1a).

Offline. The Gemini transport is injected as a stub ``complete`` callable,
so no API key or network is needed. Skill grounding's fast path (substring)
is pure; one test exercises the spaCy lemma fallback (the model is present
in the venv).
"""

from __future__ import annotations

from src.llm.schemas import JDParsed
from src.parser import apply_to_row, grounded_skills, parse
from src.state.models import AllJobs


def _job(jd_text: str = "Build data pipelines with Python and SQL on AWS.") -> AllJobs:
    return AllJobs(
        job_id="j1",
        company="Acme",
        role="Data Engineer",
        site="indeed",
        location="Bengaluru",
        jd_text=jd_text,
    )


def _stub_complete(parsed: JDParsed):
    def _complete(response_model, prompt, *, system=None, prompt_fn=None):  # noqa: ARG001
        assert response_model is JDParsed
        return parsed
    return _complete


# ---------------------------------------------------------------------------
# parse() — injected transport + grounding
# ---------------------------------------------------------------------------


def test_parse_returns_schema_and_grounds_skills() -> None:
    raw = JDParsed(
        role_summary="Build pipelines.",
        role_category="data",
        role_level="junior",
        years_required=2,
        required_skills=["Python", "SQL", "Rust"],   # Rust is not in the JD
        nice_to_have=["AWS", "Kubernetes"],          # Kubernetes is not in the JD
    )
    parsed = parse(_job(), complete=_stub_complete(raw))
    assert isinstance(parsed, JDParsed)
    # Rust is dropped by grounding: it is not in the JD text.
    assert "Rust" not in parsed.required_skills
    assert "Python" in parsed.required_skills
    assert "SQL" in parsed.required_skills
    assert parsed.nice_to_have == ["AWS"]                # Kubernetes dropped


def test_parse_recovers_pool_skills_the_model_omitted(monkeypatch) -> None:
    """A pool skill the JD names is required, whatever the model returned.

    The pool is patched rather than read from master_profile.json: that file is
    gitignored and absent on CI, so a test reading it passes locally and fails
    there — which is exactly what happened.
    """
    import src.parser as parser_module

    monkeypatch.setattr(parser_module, "_pool_terms", lambda: ("Python", "AWS", "SQL"))
    raw = JDParsed(
        role_summary="Build pipelines.",
        role_category="data",
        role_level="junior",
        years_required=2,
        required_skills=["Python"],
        nice_to_have=["AWS"],
    )
    parsed = parse(_job(), complete=_stub_complete(raw))
    # AWS is in the JD and in the pool, so it becomes a required skill even
    # though the model only offered it as nice-to-have.
    assert "AWS" in parsed.required_skills


def test_parse_passes_role_categories_into_prompt() -> None:
    seen = {}

    def _complete(response_model, prompt, *, system=None, prompt_fn=None):
        # Mirrors the real client: the prompt is rendered per provider, so
        # assert on what a provider would actually receive.
        seen["prompt"] = prompt_fn(None) if prompt_fn else prompt
        seen["system"] = system
        return JDParsed(
            role_summary="x", role_category="data", role_level="mid",
            years_required=1,
        )

    parse(_job(), complete=_complete)
    # Config cluster keys are offered to the model.
    assert "data" in seen["prompt"] and "backend" in seen["prompt"]
    assert "never infer" in seen["system"]


# ---------------------------------------------------------------------------
# apply_to_row() — new Iteration-2 fields copied through
# ---------------------------------------------------------------------------


def test_apply_to_row_copies_new_fields() -> None:
    job = _job()
    parsed = JDParsed(
        role_summary="s",
        role_category="data",
        role_level="mid",
        years_required=3,
        required_skills=["Python"],
        nice_to_have=["AWS"],
        responsibilities=["Build pipelines"],
        team_or_product="Data Platform",
        job_type="fulltime",
        location_type="hybrid",
        salary_min_lpa=8.0,
        salary_max_lpa=12.0,
        salary_currency="INR",
    )
    apply_to_row(job, parsed)
    assert job.role_category == "data"
    assert job.team_or_product == "Data Platform"
    assert job.job_type == "fulltime"
    assert job.location_type == "hybrid"
    assert job.salary_min_lpa == 8.0
    assert job.salary_max_lpa == 12.0
    assert job.salary_currency == "INR"


# ---------------------------------------------------------------------------
# grounded_skills() — substring fast path, dedup, lemma fallback
# ---------------------------------------------------------------------------


def test_grounded_skills_substring_and_dedup() -> None:
    jd = "We use Python, SQL and Apache Airflow daily."
    out = grounded_skills(["Python", "python", "SQL", "Go"], jd)
    assert out == ["Python", "SQL"]   # case-insensitive dedup; Go not present


def test_grounded_skills_empty_jd() -> None:
    assert grounded_skills(["Python"], "") == []


def test_grounded_skills_lemma_fallback() -> None:
    # "pipelines" appears inflected; the skill "pipeline" grounds via lemma.
    jd = "You will own data pipelines end to end."
    out = grounded_skills(["pipeline"], jd)
    assert out == ["pipeline"]


# ---------------------------------------------------------------------------
# Schema shapes that a provider rejected server-side on 2026-08-08
# ---------------------------------------------------------------------------

def test_null_list_fields_are_read_as_empty():
    """Models emit `"responsibilities": null` instead of omitting the key.

    A default only applies to an ABSENT key, so this failed validation — and
    Groq validates tool calls server-side, making each occurrence cost a whole
    retried call against a per-minute token limit.
    """
    from src.llm.schemas import JDParsed

    parsed = JDParsed.model_validate({
        "role_summary": "Backend role.",
        "role_category": "backend",
        "role_level": "mid",
        "years_required": 3,
        "required_skills": None,
        "nice_to_have": None,
        "responsibilities": None,
    })

    assert parsed.responsibilities == []
    assert parsed.required_skills == []
    assert parsed.nice_to_have == []


def test_a_long_role_summary_is_accepted():
    """A 632-character summary was rejected by a 500 cap, wasting two calls.

    The bound is a sanity check, not a budget — it must never be the reason a
    parse fails.
    """
    from src.llm.schemas import JDParsed

    parsed = JDParsed.model_validate({
        "role_summary": "x" * 632,
        "role_category": "backend",
        "role_level": "mid",
        "years_required": 3,
    })

    assert len(parsed.role_summary) == 632


# ---------------------------------------------------------------------------
# JD clipping — bounding what reaches the model
# ---------------------------------------------------------------------------

def test_short_descriptions_are_untouched():
    """Most of the value is in bounding outliers; typical JDs pass through."""
    from src.llm.prompts import clip_jd_text

    text = "Short and sweet."
    assert clip_jd_text(text) is text


def test_clipping_keeps_both_ends():
    """Head-only truncation was measured and rejected: pay is stated a median
    77% of the way into a JD, so cutting the tail loses the salary signal in
    most listings that carry one.
    """
    from src.llm.prompts import clip_jd_text

    text = "HEAD" + ("x" * 20_000) + "12 LPA TAIL"
    clipped = clip_jd_text(text, head=200, tail=100)

    assert clipped.startswith("HEAD")
    assert clipped.endswith("12 LPA TAIL")
    assert len(clipped) < len(text)


def test_clipping_marks_the_gap():
    """The model must be told text is missing, not handed a sentence that
    stops mid-clause and invited to infer the rest."""
    from src.llm.prompts import clip_jd_text

    clipped = clip_jd_text("a" * 9000, head=100, tail=50)
    assert "omitted" in clipped


def test_a_zero_tail_does_not_return_the_whole_text():
    """`text[-0:]` is the entire string, not the empty one — a slicing trap
    that silently disables clipping."""
    from src.llm.prompts import clip_jd_text

    clipped = clip_jd_text("a" * 5000, head=100, tail=0)
    assert len(clipped) < 200


def test_the_prompt_uses_the_clipped_text():
    """The saving is only real if the prompt builder applies it."""
    from src.llm.prompts import jd_parse_prompt
    from src.state.models import AllJobs

    job = AllJobs(
        job_id="clip-1", company="X", role="Engineer", site="test",
        jd_text="START" + ("y" * 40_000) + "END",
    )
    prompt = jd_parse_prompt(job)

    assert len(prompt) < 10_000, "the full 40k description must not be sent"
    assert "START" in prompt and "END" in prompt


def test_configured_bounds_keep_the_scoring_signals():
    """Guards the trade this setting was chosen for.

    Clipping harder measurably degraded years_required and role_level, which
    drive success_prob (30% of the final score). These floors are what stops
    someone tuning tokens down at the scorer's expense.
    """
    from src.config import settings

    # jd_text_default, not the primary's own jd_text: the primary IS the
    # top-level llm block, so reading the bound from there would report
    # whatever the local model is set to (unlimited) rather than the bound a
    # hosted provider actually gets.
    cfg = settings.llm.jd_text_default
    assert int(cfg.head_chars) >= 2500, "the head carries role and requirements"
    assert int(cfg.tail_chars) >= 1000, "the tail carries pay and how to apply"


def test_the_clip_follows_the_provider_that_will_answer():
    """How much description to send is a property of the PROVIDER.

    A local model has no token budget and should read the whole ad — pay is
    first mentioned a median 77% of the way in. A metered provider further
    down the same chain still needs it bounded, so building the prompt once
    up front would send the wrong size to one of them.
    """
    from src.llm.client import provider_config
    from src.llm.prompts import clip_jd_text

    jd = "A" * 11_815   # the longest real listing in the database

    bounded = clip_jd_text(jd)
    unbounded = clip_jd_text(jd, provider_cfg=provider_config("ollama"))

    assert len(bounded) < 6_000, "a metered provider must stay clipped"
    assert unbounded == jd, "a local provider should see the whole description"


def test_the_prompt_builder_is_handed_to_the_client_per_provider():
    """parse() must pass a builder, not a finished string, or the override
    above can never take effect."""
    seen = {}

    def _complete(response_model, prompt, *, system=None, prompt_fn=None):
        seen["prompt_fn"] = prompt_fn
        return JDParsed(
            role_summary="x", role_category="data", role_level="mid",
            years_required=1,
        )

    parse(_job(), complete=_complete)

    assert callable(seen["prompt_fn"]), "the client needs a per-provider builder"
