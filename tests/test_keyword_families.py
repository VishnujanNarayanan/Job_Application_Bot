"""Keyword families (#23): one skill, many spellings.

``hit`` stays the grader-parity primitive; ``matches`` widens it with spelling
rules and config/keyword_families.yaml. Every pair below is a real miss from the
2026-10-04 runs or a phrasing measured in the stored adverts.
"""

from __future__ import annotations

import pytest

from src.scorer import keywords as kw


def _m(tok: str, text: str) -> bool:
    return kw.matches(tok, kw.norm(text))


# --- spelling rules: no family entry needed --------------------------------


@pytest.mark.parametrize("tok,text", [
    ("Express.js", "Built the API in Node.js and Express."),   # Visionite, 2026-10-04
    ("React.js", "Wrote the pages in React."),                 # Visionite, 2026-10-04
    ("ReactJS", "Wrote the pages in React."),
    ("React", "Wrote the pages in React.js."),
    ("Node", "Ran it on Node.js."),
    ("RESTful APIs", "Built REST APIs."),
    ("REST API", "Exposed RESTful APIs."),
    ("Power BI", "Built PowerBI dashboards."),
    ("code review", "Ran code reviews on every merge."),
    ("Infrastructure-as-Code", "Kept infrastructure as code in the repo."),
])
def test_spelling_variants_match_without_a_family(tok, text):
    assert _m(tok, text)


# --- families: variants both ways, members one way ------------------------------


@pytest.mark.parametrize("tok,text", [
    ("Postgres", "Stored rows in PostgreSQL."),
    ("PostgreSQL", "Stored rows in Postgres."),
    ("CI/CD pipelines", "Gated merges with CI/CD in GitHub Actions."),   # 32 false gaps
    ("API design", "Exposed REST APIs in FastAPI."),
    ("Golang", "Wrote the service in Go."),
    ("GCP", "Deployed to Google Cloud Run."),
    ("LLMs", "Called a large language model to parse adverts."),
    ("Generative AI", "Built GenAI features."),
    ("K8s", "Deployed on Kubernetes."),
])
def test_family_variants_match_both_ways(tok, text):
    assert _m(tok, text)


@pytest.mark.parametrize("family,member_text", [
    ("NoSQL", "Stored documents in MongoDB."),
    ("vector databases", "Stored embeddings with pgvector."),
    ("containerization", "Packaged the service in Docker."),
    ("relational databases", "Modelled the data in SQLite."),
    ("version control", "Merged through Git branches."),
    ("observability", "Published metrics to CloudWatch."),
])
def test_a_member_covers_its_family(family, member_text):
    assert _m(family, member_text)


@pytest.mark.parametrize("member,family_text", [
    ("MongoDB", "Stored data in a NoSQL database."),
    ("pgvector", "Stored embeddings in a vector database."),
    ("Docker", "Ran it in containers."),
    ("Kubernetes", "Packaged the service in Docker."),
])
def test_a_family_never_covers_a_member(member, family_text):
    """The asymmetry that keeps the resume from claiming a tool it never names."""
    assert not _m(member, family_text)


# --- what must still NOT match ------------------------------------------------------


@pytest.mark.parametrize("tok,text", [
    (".NET", "Reported the net profit."),     # a leading dot is load-bearing
    ("SAS", "Styled with Sass."),             # "ss" is not a plural
    ("Java", "Wrote JavaScript."),
    ("PostgreSQL", "Wrote SQL queries."),   # one way only: SQL never proves PostgreSQL (#191)
    ("SQL", "Ran NoSQL stores."),
    ("Go", "Built in Django."),
    ("communication", "Communicated results to the desk."),   # word form, not spelling (#30)
])
def test_no_false_positives(tok, text):
    assert not _m(tok, text)


def test_sentence_punctuation_does_not_hide_a_match():
    """norm keeps '.', so the last word of a sentence used to carry it."""
    assert _m("Docker", "Packaged the service in Docker.")
    assert _m(".NET", "Built services in .NET.")


# --- downstream: bold, de-duplication, gap skills --------------------------------------


@pytest.mark.parametrize("tok,text,expected", [
    ("React.js", "Wrote the pages in React and TypeScript.", "React"),
    ("Postgres", "Stored rows in PostgreSQL.", "PostgreSQL"),
    ("containerization", "Packaged the service in Docker.", "Docker"),
    ("RESTful APIs", "Built REST APIs in FastAPI.", "REST APIs"),
])
def test_bold_marks_the_spelling_the_bullet_uses(tok, text, expected):
    spans, shown = kw.keyword_spans(text, [tok])
    assert [text[a:b] for a, b in spans] == [expected]
    assert shown == {tok}


def test_one_requirement_in_two_spellings_counts_once():
    from types import SimpleNamespace

    parsed = SimpleNamespace(
        required_skills=["REST APIs", "RESTful APIs", "Postgres", "PostgreSQL", "Python"],
        nice_to_have=["REST API"], responsibilities=[],
    )
    tokens = [k.token for k in kw.jd_keywords(parsed)]
    # The registry names each skill once, by its own name (#188/#189).
    assert tokens == ["REST APIs", "PostgreSQL", "Python"]


def test_gap_skills_read_bullets_and_spellings():
    """"CI/CD pipelines" was a gap in 32 matched jobs while the profile said CI/CD."""
    from src.main import _compute_gap_skills

    bullets = (kw.norm("Gated merges with CI/CD in GitHub Actions."),
               kw.norm("Built the pages in React."))
    gaps = _compute_gap_skills(["CI/CD pipelines", "React.js", "Kafka"], ["Python"], bullets)
    assert gaps == ["Kafka"]


def test_gap_skills_keep_the_leading_dot_of_dotnet():
    """tokens_of strips it; ".NET" must not be cleared by a bullet saying "net"."""
    from src.main import _compute_gap_skills

    bullets = (kw.norm("Trained a small neural net in NumPy."),)
    assert _compute_gap_skills([".NET", ".NET Core"], [], bullets) == [".NET", ".NET Core"]


# --- #191: allowed swaps, one way --------------------------------------------------

@pytest.mark.parametrize("tok,text", [
    ("SQL", "Tuned PostgreSQL."),
    ("SQL", "Queried events with Spark SQL."),
    ("AI-assisted development", "Built it with Claude Code and Cursor."),
    ("tool calling", "Exposed the API as an MCP server."),
    ("infrastructure automation", "Provisioned it with Terraform."),
    ("semantic search", "Ranked passages by vector search in pgvector."),
    ("exception handling", "Added error handling and logging."),
    ("predictive modelling", "Predictive modeling of churn."),
    ("grounding", "Kept answers grounded in retrieved documents."),
])
def test_allowed_swaps(tok, text):
    assert _m(tok, text)


@pytest.mark.parametrize("tok,text", [
    ("SQL", "Stored sessions in MongoDB."),
    ("Claude Code", "Practised AI-assisted development."),
    ("workflow orchestration", "Scheduled jobs with cron."),
])
def test_swaps_never_allowed(tok, text):
    assert not _m(tok, text)


# --- #191: every row of the issue's two tables, plus the never list -------------

@pytest.mark.parametrize("tok,text,want", [
    ("AI", "Maintained the pipeline and said so.", False),     # letters inside words
    ("AI", "Wired the OpenAI API.", False),
    ("Spark", "Planned it in Apache Spark.", True),
    ("NoSQL", "Kept sessions in MongoDB.", True),
    ("MongoDB", "Used NoSQL stores.", False),
    ("relational databases", "Stored rows in PostgreSQL.", True),
    ("AWS", "Deployed on Amazon Web Services.", True),
    ("SQL", "Stored events in PostgreSQL.", True),
    ("SQL", "Queried with Spark SQL.", True),
    ("React", "Built React Native apps.", False),              # never: React Native
    ("PostgreSQL", "Wrote SQL queries.", False),
    ("workflow orchestration", "Scheduled the flows in Airflow.", True),
    ("infrastructure automation", "Provisioned it with Terraform.", True),
    ("SQL", "Kept documents in MongoDB.", False),
    ("Java", "Wrote JavaScript.", False),
])
def test_issue_191_tables(tok, text, want):
    assert _m(tok, text) is want


@pytest.mark.parametrize("tok,text,want", [
    ("Azure", "Ran pipelines in Azure DevOps.", False),
    ("Azure", "Deployed to Azure and tracked work in Azure DevOps.", True),
    ("Claude", "Used Claude Code daily.", False),
    ("GitHub", "Used GitHub Copilot.", False),
    ("GitHub", "Ran checks in GitHub Actions.", True),
    ("Snowflake", "Designed a snowflake schema.", False),
    ("testing", "Ran A/B testing on prices.", False),
    ("testing", "Load testing with k6.", True),
    ("AWS", "Deployed AWS Lambda functions.", True),
    ("Spring", "Built Spring Boot services.", True),
])
def test_a_longer_name_proves_the_skill_inside_unless_listed_never(tok, text, want):
    assert _m(tok, text) is want


def test_never_names_are_not_bolded():
    from src.scorer.keywords import keyword_spans

    assert keyword_spans("Ran pipelines in Azure DevOps.", ["Azure"])[1] == set()


def test_weekly_review_lists_skills_a_new_term_contains():
    from tools.registry_review import _contains

    assert _contains("Azure DevOps Server", ["Azure DevOps", "Azure", "Kafka"]) == ["Azure DevOps", "Azure"]
