"""Rebuild master_profile.yaml from the per-repo bullet_extract_latest.yaml files.

Hard rule #2 says code never *writes* master_profile.yaml. This tool is the
operator's hand, not the pipeline's: it is run explicitly, from the terminal,
when the operator has re-run the bullet-extract skill across their repos. The
pipeline itself still only ever reads the file.

Why a tool at all. The profile is ~19 entries x 3-6 role blocks x ~15 bullets,
each one authored in its own repo by the extract skill. Splicing that by hand is
where transcription errors live, and every re-extract round would pay the cost
again. The extract files are the source; this is the assembler.

What it carries forward from the existing file, because no extract knows it:

  * ``personal``, ``education``, ``certifications``, ``meta``
  * a work entry's ``start_date`` / ``end_date`` / ``location`` — employment
    facts, not repo facts, so most extracts omit them
  * any entry named in ``--keep``, copied verbatim (see below)

``--keep`` exists for one case: an extract OLDER than the profile it would
overwrite. Rebuilding such an entry silently reverts whatever was done to it
after that extract was written, so those entries are pinned instead and the run
reports them.

Usage::

    python -m tools.build_master_profile --check      # validate, write nothing
    python -m tools.build_master_profile              # rebuild, backing up first
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_PROFILE = _ROOT / "master_profile.yaml"
_REPOS = _ROOT.parent

#: Source repos, in the order their entries should appear in the profile. Order
#: is cosmetic — Layer 4 ranks entries by score, never by file position — but a
#: stable order keeps the diff between two rebuilds readable.
WORK_REPOS = [
    "market_data",
    "mql5",
    "dekhlaw-app",
    "law_firm_website",
]
PROJECT_REPOS = [
    "trade_quote internship",  # two entries: nse_trade_quote, trader_sentiment_analysis
    "product-explorer",
    "job_application_bot",
    "ticket-classifier-nlp",
    "Fraud_Transaction_Detection",
    "Quotes_Retrieval",
    "Age_Gender_classifier",
    "Neural_networks",
    "Linear_regression_scratch",
    "Trading_Bot",
    "Directory_app",
    "Accredian_intership",
    "functional_programming",
    "portfolioWebsite",
]

#: Entries whose extract predates the profile — see the module docstring.
DEFAULT_KEEP = ["nse_trade_quote", "trader_sentiment_analysis"]

#: ``Trader_sentiment_analysis_backup`` is a stale copy of an entry that
#: ``trade_quote internship`` also carries, and a duplicate id fails validation.
IGNORED_REPOS = {"Trader_sentiment_analysis_backup"}

_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

#: Block keys copied through, in render order. ``project_line`` and
#: ``checklist_source`` are not on the RoleBlock model — Pydantic ignores them —
#: but they are the extract's own audit trail and a human reads this file too.
_BLOCK_KEYS = [
    "role", "role_fit", "entry_header", "entry_dates", "project_line",
    "checklist", "checklist_source", "market", "target_titles", "title_aliases",
    "bullets", "extra_bullets", "covered", "missing", "titles_dropped",
    "lead_pct",
]


class BuildError(RuntimeError):
    pass


def _extract_path(repo: str) -> Path:
    return _REPOS / repo / "bullet_extract_latest.yaml"


def _month_year(ym: str) -> str:
    """``2025-06`` -> ``June 2025``; ``present`` -> ``current``."""
    if ym.strip().lower() in {"present", "current", ""}:
        return "current"
    try:
        year, month = ym.split("-")[:2]
        return f"{_MONTHS[int(month) - 1]} {int(year)}"
    except (ValueError, IndexError) as exc:
        raise BuildError(f"unparseable date '{ym}'") from exc


def _bullet(b: dict[str, Any]) -> dict[str, Any]:
    """Only what the Bullet model holds. ``kind`` is the extractor's own marker;
    the model infers the summary from position (``bullets[0]``), so carrying it
    would give the same fact two homes that could disagree."""
    out = {"id": b["id"], "text": " ".join(str(b["text"]).split())}
    if b.get("tags"):
        out["tags"] = list(b["tags"])
    return out


def _block(rb: dict[str, Any], *, entry_dates: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _BLOCK_KEYS:
        if key == "entry_dates":
            if entry_dates:
                out["entry_dates"] = entry_dates
            continue
        if key in ("bullets", "extra_bullets"):
            vals = [_bullet(b) for b in (rb.get(key) or [])]
            if vals or key == "bullets":
                out[key] = vals
            continue
        val = rb.get(key)
        if val not in (None, "", [], {}):
            out[key] = val
    if not out.get("bullets"):
        raise BuildError(f"block '{rb.get('role')}' has no bullets")
    return out


def _entry(
    src: dict[str, Any], *, kind: str, prior: dict[str, Any] | None
) -> dict[str, Any]:
    """One work or project entry, extract fields first, prior file for the rest."""
    prior = prior or {}
    out: dict[str, Any] = {"id": src["id"]}

    if kind == "work":
        out["company"] = src["company"]
        out["actual_title"] = src["actual_title"]
        aliases = list(src["safe_title_aliases"])
        # Rule #6: every alias a block may render under has to be on the entry's
        # allow-list, and a new block can introduce one the entry list predates.
        for rb in src["role_blocks"]:
            for alias in rb.get("title_aliases") or []:
                if alias not in aliases:
                    aliases.append(alias)
        out["safe_title_aliases"] = aliases
        for key in ("start_date", "end_date", "location"):
            val = src.get(key) or prior.get(key)
            if val:
                out[key] = val
        if not out.get("start_date") or not out.get("end_date"):
            raise BuildError(
                f"work entry '{src['id']}': no start/end date in the extract and "
                "none in the existing profile to carry forward"
            )
        out["employment_type"] = src.get(
            "employment_type", prior.get("employment_type", "employment")
        )
        dates = (
            f"{_month_year(out['start_date'])} to {_month_year(out['end_date'])}"
        )
    else:
        out["name"] = src["name"]
        dates = None

    # The right slot prefers a live demo over a repo: a recruiter with twenty
    # seconds opens a working site, not a source tree. An extract that recorded
    # neither falls back to whatever the operator had set (mql5's is "" on
    # purpose — that repo is private and a link would 404).
    link = src.get("demo") or src.get("link") or prior.get("link", "")
    if kind == "work":
        out["link"] = link
    else:
        out["link"] = link
        if src.get("tags"):
            out["tags"] = list(src["tags"])

    out["role_blocks"] = [
        _block(rb, entry_dates=rb.get("entry_dates") or dates)
        for rb in src["role_blocks"]
    ]
    return out


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise BuildError(f"missing extract: {path}")
    with path.open() as fh:
        return yaml.safe_load(fh)


def build(keep: list[str]) -> tuple[dict[str, Any], list[str]]:
    current = yaml.safe_load(_PROFILE.read_text())
    prior = {e["id"]: e for e in current["work_experience"] + current["projects"]}
    notes: list[str] = []

    work: list[dict[str, Any]] = []
    projects: list[dict[str, Any]] = []
    skills: list[str] = []
    gaps: list[dict[str, Any]] = []
    gap_seen: set[str] = set()
    sources: dict[str, str] = {}

    for repo in [*WORK_REPOS, *PROJECT_REPOS]:
        if repo in IGNORED_REPOS:
            continue
        path = _extract_path(repo)
        data = _load(path)
        stamp = dt.date.fromtimestamp(path.stat().st_mtime).isoformat()

        for sec, bucket, kind in (
            ("work_experience", work, "work"),
            ("projects", projects, "project"),
        ):
            for src in data.get(sec) or []:
                eid = src["id"]
                sources[eid] = f"{repo}/bullet_extract_latest.yaml — {stamp}"
                if eid in keep:
                    if eid not in prior:
                        raise BuildError(f"--keep '{eid}' is not in the current file")
                    bucket.append(prior[eid])
                    notes.append(f"kept verbatim: {eid} (extract dated {stamp})")
                    continue
                bucket.append(_entry(src, kind=kind, prior=prior.get(eid)))

        for skill in data.get("skills_pool") or []:
            if skill not in skills:
                skills.append(skill)
        for gap in data.get("gap_skills") or []:
            name = gap["skill"] if isinstance(gap, dict) else str(gap)
            if name in gap_seen:
                continue
            gap_seen.add(name)
            gaps.append(gap if isinstance(gap, dict) else {"skill": name})

    profile = {
        "personal": current["personal"],
        "work_experience": work,
        "projects": projects,
        "skills_pool": skills,
        "gap_skills": gaps,
        "education": current["education"],
        "certifications": current["certifications"],
    }
    if current.get("meta"):
        profile["meta"] = current["meta"]

    dropped = set(prior) - {e["id"] for e in work + projects}
    for eid in sorted(dropped):
        notes.append(f"dropped (no extract supplies it any more): {eid}")
    added = {e["id"] for e in work + projects} - set(prior)
    for eid in sorted(added):
        notes.append(f"new entry: {eid}")
    return profile, notes, sources


def verify(parsed: Any, keep: list[str]) -> int:
    """Every rendered bullet is byte-identical to the extract it came from.

    Hard rule #1: bullets come verbatim from the source. A dump that folds, quotes
    or re-wraps a line is a silent rewrite of resume text, so the assembled file
    is read back and compared against the extracts rather than trusted.
    """
    got = {
        b.id: b.text
        for e in parsed.entries
        for rb in e.role_blocks
        for b in rb.all_bullets
    }
    checked = 0
    for repo in [*WORK_REPOS, *PROJECT_REPOS]:
        if repo in IGNORED_REPOS:
            continue
        data = _load(_extract_path(repo))
        for sec in ("work_experience", "projects"):
            for src in data.get(sec) or []:
                if src["id"] in keep:
                    continue
                for rb in src["role_blocks"]:
                    for b in [*rb["bullets"], *(rb.get("extra_bullets") or [])]:
                        want = " ".join(str(b["text"]).split())
                        checked += 1
                        if got.get(b["id"]) != want:
                            raise BuildError(
                                f"bullet '{b['id']}' does not round-trip:\n"
                                f"  extract: {want!r}\n  rendered: {got.get(b['id'])!r}"
                            )
    return checked


class _Dumper(yaml.SafeDumper):
    """Block style everywhere, and no anchors — two bullets that happen to share
    a text would otherwise come back as a YAML alias, which reads as a bug."""

    def ignore_aliases(self, data: Any) -> bool:  # noqa: D102
        return True


def _str_presenter(dumper: yaml.Dumper, data: str) -> Any:
    style = ">" if len(data) > 90 and "\n" not in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str_presenter)


def render(profile: dict[str, Any], sources: dict[str, str]) -> str:
    """Dump section by section so each entry can carry its source line."""
    today = dt.date.today().isoformat()
    out = [
        "# master_profile.yaml — single source of truth (hard rule #2: the\n"
        "# operator edits this file; code only reads it).\n"
        "#\n"
        f"# GENERATED {today} by tools/build_master_profile.py from the per-repo\n"
        "# bullet_extract_latest.yaml files. Hand edits here are lost on the next\n"
        "# rebuild — change the extract in its own repo instead, or pin the entry\n"
        "# with --keep.\n\n"
    ]

    def dump(obj: Any, indent: int = 0) -> str:
        text = yaml.dump(
            obj, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=88,
            default_flow_style=False,
        )
        pad = " " * indent
        return "".join(pad + line if line.strip() else line for line in text.splitlines(True))

    out.append(dump({"personal": profile["personal"]}))
    for sec in ("work_experience", "projects"):
        out.append(f"{sec}:\n")
        for entry in profile[sec]:
            src = sources.get(entry["id"], "unknown source")
            out.append(f"\n  # ===== {entry['id']} — from {src} =====\n")
            body = dump([entry], indent=2)
            out.append(body)
        out.append("\n")
    out.append(
        "# skills_pool and gap_skills are MACHINE INPUT for JD-parse repair and the\n"
        "# dashboard's gap list. Nothing in either renders; every keyword that matters\n"
        "# also lives inside a bullet above.\n"
    )
    for sec in ("skills_pool", "gap_skills", "education", "certifications"):
        out.append(dump({sec: profile[sec]}))
        out.append("\n")
    if profile.get("meta"):
        out.append(dump({"meta": profile["meta"]}))
    return "".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="validate, write nothing")
    ap.add_argument(
        "--keep", nargs="*", default=DEFAULT_KEEP,
        help="entry ids to copy verbatim from the existing profile",
    )
    args = ap.parse_args(argv)

    try:
        profile, notes, sources = build(list(args.keep))
    except BuildError as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1

    text = render(profile, sources)

    # Validate what will actually be on disk, not the in-memory dict: a dumping
    # bug that mangles a bullet has to fail here, not at the next pipeline run.
    from src.state.master_profile import MasterProfile

    parsed = MasterProfile.model_validate(yaml.safe_load(text))

    try:
        verified = verify(parsed, list(args.keep))
    except BuildError as exc:
        print(f"verification failed: {exc}", file=sys.stderr)
        return 1

    blocks = sum(len(e.role_blocks) for e in parsed.entries)
    bullets = sum(len(rb.all_bullets) for e in parsed.entries for rb in e.role_blocks)
    for note in notes:
        print(f"  - {note}")
    print(
        f"{len(parsed.work_experience)} work + {len(parsed.projects)} projects, "
        f"{blocks} blocks, {bullets} bullets ({verified} verified verbatim), "
        f"{len(parsed.skills_pool)} skills"
    )

    if args.check:
        print("--check: nothing written")
        return 0

    backup = _PROFILE.with_suffix(f".yaml.bak-{dt.date.today().isoformat()}")
    shutil.copy2(_PROFILE, backup)
    _PROFILE.write_text(text)
    print(f"wrote {_PROFILE} (backup: {backup.name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
