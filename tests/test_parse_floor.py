"""#100 / #71: the dictionary parse, the router and the pipeline around them.

The years and job-type cases are real advert wording from the 2026-10-16
replay, each one a disagreement with the LLM that the rules were fixed for.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.llm.sections import split_sections
from src.parsing import modes
from src.parsing.dictionary import (
    Dictionary,
    build_parsed,
    extract_job_type,
    extract_years,
    split_required_nice,
)
from src.parsing.modes import ParseMode
from src.parsing.router import Reason, Route, RoutingRules, route


def years(text: str) -> int | None:
    return extract_years(split_sections(text))


# --- years -----------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("* 8+ years of Strong programming experience in Python", 8),
    ("* 8+ experience in Python development", 8),
    ("* 15 or more years of progressive experience in software engineering, "
     "with at least 5 years in architecture", 15),
    ("* 12+ years of software engineering experience, including a minimum of 5 years "
     "in software architecture", 12),
    ("The Lead Watch Engineer must have a minimum of seven (7) years experience", 7),
    ("* 4.5-8 years of experience in software engineering", 4),
    ("**Minimum 3 Year(s) Of Experience Is Required**", 3),
])
def test_years_are_read_as_the_overall_requirement(text, expected):
    assert years(text) == expected


@pytest.mark.parametrize("text", [
    "* Witbe has been operating for 25 years. We're publicly traded.",
    "PayPal has been revolutionizing commerce globally for more than 25 years.",
    "InfoCepts is a global leader with nearly 20 years' experience enabling customers",
    "* A 15 years full-time education is required.",
])
def test_company_history_and_education_are_not_experience(text):
    assert years(text) is None


def test_a_maximum_is_not_a_requirement():
    assert years("* Minimum of 5 years and maximum of 10 years' experience") == 5


def test_alternatives_take_the_smaller_number():
    text = ("* Bachelor's degree and 3+ years of experience in data science, or 7+ years "
            "of experience in data science in lieu of a degree")
    assert years(text) == 3


def test_preferred_qualifications_do_not_raise_the_requirement():
    text = ("**Required Qualifications:**\n\n* 4+ years of Software Engineering experience\n\n"
            "**Desired Qualifications:**\n\n* 10+ years of Software Engineering experience\n")
    assert years(text) == 4


# --- job type ----------------------------------------------------------------


@pytest.mark.parametrize("title, text, expected", [
    ("Full Stack Developer Intern", "", "internship"),
    ("ADMINISTRATOR L3(CONTRACT)", "", "contract"),
    ("Data Analyst", "This is a Contract to Hire position based out of San Diego.", "contract"),
    ("Machine Learning Engineer - Expert", "**Position:**\n\n**Contract**\n\nWe are", "contract"),
    ("Full Stack Engineer", "Duration: 2 Years of Contract", "contract"),
    ("Frontend Web Developer", "seeking a Frontend Developer to work on a Contractor basis.",
     "contract"),
    ("JavaScript Developer", "This is a part-time remote role for a JavaScript Developer.",
     "parttime"),
    ("Data Scientist", "DATA SCIENCE INTERN\n\n**Duration:**\n\n3 Months", "internship"),
    ("Content Writer", "Job Types: Full-time, Permanent, Fresher, Internship", "fulltime"),
])
def test_job_type(title, text, expected):
    assert extract_job_type(title, text) == expected


@pytest.mark.parametrize("text", [
    "may include provisions under the McNamara-O'Hara Service Contract Act (SCA).",
    "Pay depends on seniority, location and union contract terms.",
    "We are a full-time team building data products.",
])
def test_legal_boilerplate_is_not_a_job_type(text):
    assert extract_job_type("Data Engineer", text) is None


# --- skills ------------------------------------------------------------------


DICT = Dictionary(vocabulary=(("Kafka", "kafka"), ("BigQuery", "bigquery")),
                  pool=("Python", "SQL"), families=())


def test_the_pool_spelling_wins_and_longest_comes_first():
    d = Dictionary(vocabulary=(("python", "python"),), pool=("Python",), families=("PostgreSQL",))
    terms = d.terms()
    assert ("Python", "python") in terms
    assert len(terms[0][1]) >= len(terms[-1][1])


def test_nice_to_have_comes_from_its_section_and_inline_markers():
    text = ("**Requirements:**\n\n* Python and SQL\n* Kafka is a plus\n\n"
            "**Preferred qualifications:**\n\n* BigQuery\n")
    skills = ["Python", "SQL", "Kafka", "BigQuery"]
    required, nice = split_required_nice(skills, text, split_sections(text), DICT)
    assert required == ["Python", "SQL"]
    assert nice == ["Kafka", "BigQuery"]


def test_build_parsed_needs_no_llm():
    job = SimpleNamespace(role="Data Engineer", jd_text=(
        "**Requirements:**\n\n* 3+ years of experience with Python and SQL\n"
        "* Kafka is a plus\n\n**Responsibilities:**\n\n* Build pipelines\n"))
    parsed = build_parsed(job, DICT)
    assert parsed.required_skills == ["Python", "SQL"]
    assert parsed.nice_to_have == ["Kafka"]
    assert parsed.years_required == 3
    assert parsed.responsibilities == ["Build pipelines"]


# --- router ------------------------------------------------------------------


# The routing logic, at fixed cut-offs; the configured values are checked below.
CUTS = dict(floor=0.40, skip_below=0.44, trust_above=0.75)
FLOOR_ONLY = RoutingRules(**CUTS)
ROUTED = RoutingRules(enabled=True, daily_llm_budget=10, **CUTS)


@pytest.mark.parametrize("score, rules, calls, expected", [
    (0.39, FLOOR_ONLY, 0, (Route.SKIPPED_FLOOR, Reason.BELOW_FLOOR)),
    (0.40, FLOOR_ONLY, 0, (Route.LLM, Reason.ROUTING_DISABLED)),
    (0.90, FLOOR_ONLY, 0, (Route.LLM, Reason.ROUTING_DISABLED)),
    (0.39, ROUTED, 0, (Route.SKIPPED_FLOOR, Reason.BELOW_FLOOR)),
    (0.43, ROUTED, 0, (Route.DICTIONARY, Reason.BELOW_SKIP)),
    (0.60, ROUTED, 0, (Route.LLM, Reason.MIDDLE_BAND)),
    (0.75, ROUTED, 0, (Route.DICTIONARY, Reason.ABOVE_TRUST)),
    (0.60, ROUTED, 10, (Route.CARRIED_OVER, Reason.BUDGET_CAP)),
    (0.80, ROUTED, 10, (Route.DICTIONARY, Reason.ABOVE_TRUST)),
])
def test_route(score, rules, calls, expected):
    d = route(score, llm_calls_today=calls, rules=rules)
    assert (d.route, d.reason) == expected


def test_a_zero_floor_never_skips():
    assert route(0.0, llm_calls_today=0, rules=RoutingRules(floor=0.0)).route is Route.LLM


def test_config_loads_with_71_routing_on():
    from src.parsing.router import rules_from_config

    rules = rules_from_config()
    assert rules.floor == 0.437 and rules.enabled is True
    assert rules.floor <= rules.skip_below <= rules.trust_above


# --- pipeline ----------------------------------------------------------------


class _Fake:
    def __init__(self, mode, parsed=None, error=None):
        self.mode, self.parsed, self.error, self.calls = mode, parsed, error, 0

    def parse(self, job):
        self.calls += 1
        if self.error:
            raise self.error
        return self.parsed


@pytest.fixture
def fakes(monkeypatch):
    """Fake parsers, and a scorer that returns the job's ``score``."""
    dict_parsed = SimpleNamespace(years_required=3, job_type=None, name="dict")
    llm_parsed = SimpleNamespace(name="llm")
    d = _Fake(ParseMode.DICTIONARY, dict_parsed)
    l = _Fake(ParseMode.LLM, llm_parsed)
    modes.reset_registry()
    modes.register(d)
    modes.register(l)
    monkeypatch.setattr("src.tailor.score_job", lambda profile, job, parsed, **_: SimpleNamespace(
        final_score=job.score, fit=job.score))
    yield d, l
    modes.reset_registry()


def _job(score):
    return SimpleNamespace(job_id="j1", score=score)


def test_below_the_floor_the_llm_is_never_called(fakes):
    from src.parsing.pipeline import parse_job

    d, l = fakes
    r = parse_job(_job(0.30), profile=None, rules=FLOOR_ONLY)
    assert r.decision.route is Route.SKIPPED_FLOOR
    assert r.parsed.name == "dict" and r.mode is ParseMode.DICTIONARY
    assert l.calls == 0 and r.llm_called is False


def test_at_the_floor_the_llm_decides_and_the_dictionary_score_is_kept(fakes):
    from src.parsing.pipeline import parse_job

    d, l = fakes
    r = parse_job(_job(0.55), profile=None, rules=FLOOR_ONLY)
    assert r.parsed.name == "llm" and r.llm_called and l.calls == 1
    assert r.dict_score == 0.55 and r.dict_parsed.name == "dict"


def test_an_llm_error_carries_over_while_routing_is_off(fakes):
    from src.llm.client import LLMError
    from src.parsing.pipeline import parse_job

    _, l = fakes
    l.error = LLMError("schema validation failed")
    with pytest.raises(LLMError):
        parse_job(_job(0.55), profile=None, rules=FLOOR_ONLY)


def test_an_llm_error_falls_back_to_the_dictionary_once_routing_is_on(fakes):
    from src.llm.client import LLMError
    from src.parsing.pipeline import parse_job

    _, l = fakes
    l.error = LLMError("schema validation failed")
    r = parse_job(_job(0.55), profile=None, rules=ROUTED)
    assert r.decision.route is Route.DICTIONARY_FALLBACK and r.decision.reason is Reason.LLM_ERROR
    assert r.parsed.name == "dict" and r.fell_back


def test_a_budget_error_always_stops_the_batch(fakes):
    from src.llm.client import LLMBudgetError
    from src.parsing.pipeline import parse_job

    _, l = fakes
    l.error = LLMBudgetError("quota")
    with pytest.raises(LLMBudgetError):
        parse_job(_job(0.55), profile=None, rules=ROUTED)


def test_the_route_is_recorded_on_the_row(fakes):
    from datetime import datetime, timezone

    from src.parsing.pipeline import parse_job, record_route

    job = _job(0.30)
    r = parse_job(job, profile=None, rules=FLOOR_ONLY)
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    record_route(job, r, FLOOR_ONLY, now=now)
    assert (job.parse_mode, job.parse_route_reason) == ("skipped_floor", "below_floor")
    assert job.dict_score == 0.30 and job.llm_called is False
    assert job.routing_version == FLOOR_ONLY.version and job.routed_at == now


def test_with_the_llm_unavailable_a_middle_band_job_uses_the_dictionary(fakes):
    """The ATS run's free providers are spent: the job is still parsed, for free."""
    from src.parsing.pipeline import parse_job

    _, l = fakes
    r = parse_job(_job(0.55), profile=None, rules=ROUTED, llm_available=False)
    assert l.calls == 0 and r.llm_called is False
    assert r.decision.route is Route.DICTIONARY_FALLBACK
    assert r.decision.reason is Reason.LLM_UNAVAILABLE
    assert r.parsed.name == "dict"


def test_llm_unavailable_does_not_change_the_floor(fakes):
    from src.parsing.pipeline import parse_job

    r = parse_job(_job(0.30), profile=None, rules=ROUTED, llm_available=False)
    assert r.decision.route is Route.SKIPPED_FLOOR
