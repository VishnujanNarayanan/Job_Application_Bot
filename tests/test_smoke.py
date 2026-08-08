"""Iteration 0 smoke tests.

Verify the scaffold is structurally intact. No business logic is tested
here — Iteration 0 has none. Each subsequent iteration adds its own
tests alongside the real implementation it introduces.
"""

from pathlib import Path

import yaml


def test_repo_layout(repo_root: Path) -> None:
    """Every path mandated by architecture doc Section 7 must exist."""
    must_exist = [
        "src",
        "src/__init__.py",
        "src/main.py",
        "src/scheduler.py",
        "src/parser.py",
        "src/notifications.py",
        "src/analytics.py",
        "src/scraper",
        "src/scraper/__init__.py",
        "src/scraper/jobspy_wrapper.py",
        "src/scraper/rotation.py",
        "src/scraper/filters.py",
        "src/scorer",
        "src/scorer/__init__.py",
        "src/scorer/embeddings.py",
        "src/scorer/selector.py",
        "src/scorer/ordering.py",
        "src/scorer/apply_decision.py",
        "src/builder",
        "src/builder/__init__.py",
        "src/builder/llm_call.py",
        "src/endpoint",
        "src/endpoint/__init__.py",
        "src/endpoint/app.py",
        "src/endpoint/assembler.py",
        "src/endpoint/hyperlinks.py",
        "src/endpoint/pdf_convert.py",
        "src/endpoint/cache.py",
        "src/aws",
        "src/aws/__init__.py",
        "src/aws/iam_session.py",
        "src/aws/s3.py",
        "src/state",
        "src/state/__init__.py",
        "src/state/models.py",
        "src/state/migrations",
        "src/state/master_profile.py",
        "src/state/cleanup.py",
        "src/llm",
        "src/llm/__init__.py",
        "src/llm/client.py",
        "src/llm/schemas.py",
        "src/llm/prompts.py",
        "src/cli",
        "src/cli/__init__.py",
        "src/cli/inspect.py",
        "src/cli/dryrun.py",
        "src/cli/reparse.py",
        "tests",
        "config",
        "config/config.yaml",
        "resumes/templates",
        "resumes/applied",
        "data/logs",
        "requirements.txt",
        ".env.example",
        ".gitignore",
        "master_profile.example.yaml",
        "README.md",
        # CHANGELOG.md, PRD.md, CLAUDE.md and job_automation_architecture.md
        # are deliberately NOT listed. They were gitignored and untracked on
        # 2026-07-05 as operator-owned docs, so they exist on the operator's
        # disk but not in a fresh clone. Asserting them here passed locally
        # and failed every CI run from that day onward.
    ]
    missing = [p for p in must_exist if not (repo_root / p).exists()]
    assert not missing, f"Missing scaffold paths:\n" + "\n".join(f"  {p}" for p in missing)


def test_operator_docs_are_not_tracked_by_git() -> None:
    """The operator docs must stay out of the repo.

    The inverse of the bug above: rather than asserting these files exist
    (which broke CI), assert they are not tracked — that is the property that
    was actually intended, and it holds in a clone as well as on the
    operator's machine.

    CHANGELOG.md was on this list until 2026-08-08 and is now deliberately
    tracked: it records what changed in the CODE, so it belongs to the repo.
    The rest describe this instance, not the software.
    """
    import subprocess

    operator_docs = [
        "PRD.md",
        "CLAUDE.md",
        "job_automation_architecture.md",
        "TODO.md",
    ]
    tracked = subprocess.run(
        ["git", "ls-files", "--", *operator_docs],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True,
        text=True,
    ).stdout.split()

    assert not tracked, (
        "operator docs must stay untracked (gitignored on 2026-07-05): "
        f"{tracked}"
    )


def test_role_clusters_yaml_absent(repo_root: Path) -> None:
    """role_clusters lives inside config.yaml, not as a separate file."""
    assert not (repo_root / "config" / "role_clusters.yaml").exists()


def test_config_yaml_loads_and_has_required_sections(config_path: Path) -> None:
    """config.yaml must parse cleanly and expose all top-level sections."""
    with config_path.open() as f:
        config = yaml.safe_load(f)

    required = {
        "operator", "filters", "salary", "scheduler", "scraper",
        "selection", "scoring", "builder", "voice", "cover_pdf",
        "embeddings", "spacy", "llm", "database", "storage",
        "notifications", "analytics", "logging",
    }
    missing = required - set(config.keys())
    assert not missing, f"Missing config sections: {missing}"


def test_config_no_role_clusters_yaml(repo_root: Path) -> None:
    """Confirm no stray role_clusters.yaml was created (role-cluster
    acceptance was removed; all scraped jobs proceed to scoring)."""
    assert not (repo_root / "config" / "role_clusters.yaml").exists()


def test_success_prob_weights_sum_to_one(config_path: Path) -> None:
    """success_prob weights must sum to 1.0 (seniority + recency)."""
    with config_path.open() as f:
        config = yaml.safe_load(f)

    sp = config["scoring"]["success_prob"]
    total = sp["weight_seniority"] + sp["weight_recency"]
    assert abs(total - 1.0) < 1e-9, f"success_prob weights sum to {total}, expected 1.0"


def test_final_score_weights_sum_to_one(config_path: Path) -> None:
    """Final score weights must sum to 1.0."""
    with config_path.open() as f:
        config = yaml.safe_load(f)

    fw = config["scoring"]["final"]
    total = fw["fit"] + fw["success_prob"] + fw["recency"] + fw["project"]
    assert abs(total - 1.0) < 1e-9, f"final score weights sum to {total}, expected 1.0"


def test_master_profile_stub_importable() -> None:
    """MasterProfile stub must be importable from its agreed location."""
    from src.state.master_profile import MasterProfile
    assert MasterProfile is not None


def test_main_importable() -> None:
    """src.main.main() must be importable. Behaviour is tested in
    tests/test_iteration_1.py — it now drives the real pipeline."""
    from src.main import main
    assert callable(main)
