"""#177 / #195 §1: candidate terms from advert lines (tools.term_discovery)."""

from __future__ import annotations

from tools import term_discovery as td

KNOWN = {"python", "kafka", "docker", "aws", "ci/cd", "kubernetes"}


def known(term: str) -> bool:
    return term.casefold() in KNOWN


def cands(line: str) -> set[str]:
    return set(td.candidates_in_line(line, known))


def test_names_by_shape():
    got = cands("Build services in Python using PyTorch, GPT-4 and S3 on RAID storage.")
    assert {"PyTorch", "GPT-4", "S3", "RAID"} <= got


def test_a_list_next_to_a_known_skill_yields_its_unknown_members():
    got = cands("Messaging with Kafka, RabbitMQ and NATS")
    assert {"RabbitMQ", "NATS"} <= got


def test_ask_phrases_and_labelled_lines():
    assert "Group Policy" in cands("Experience with Group Policy and Active Directory")
    assert "Terragrunt" in cands("Tools: Terragrunt, Docker")


def test_ordinary_words_are_not_candidates():
    got = cands("Hands-on, end-to-end ownership; must have strong communication. Develop, Implement, Understanding")
    assert not ({"Hands-on", "end-to-end", "Develop", "Implement", "Understanding"} & got)


def test_parts_of_slashed_names_and_headings_are_not_candidates():
    got = cands("Drive CI/CD pipelines. Preferred Skills: Key Responsibilities")
    assert "CD" not in got and "Preferred Skills" not in got and "Key Responsibilities" not in got


def test_cleaning():
    assert td._clean("Adobe Experience Manager (AEM") == "Adobe Experience Manager"
    assert td._clean("API and") == "API"
    assert td._clean("  **Snowflake**, ") == "Snowflake"


def test_numbered_inline_lists_split():
    got = cands("Steps: 1. Use Jira 2. Configure Rally 3. Report")
    assert not any(". " in c for c in got)


def test_suggestions():
    from types import SimpleNamespace

    from src.state.registry import APPROVE

    reg = SimpleNamespace(exact={"aws": SimpleNamespace(term="AWS", label=APPROVE),
                                 "multi-tenancy": SimpleNamespace(term="multi-tenancy", label=APPROVE)})
    assert td.suggest("AWS Systems Manager", reg) == "part of / variant of AWS?"
    assert td.suggest("multi-tenant", reg) == "spelling of multi-tenancy?"
    assert td.suggest("Certified Scrum Professional", reg) == "credential?"
    assert td.suggest("Rally", reg) == "new skill?"
