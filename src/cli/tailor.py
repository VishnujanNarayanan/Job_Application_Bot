"""CLI — tailor a resume for a pasted job advert (#20).

    python -m src.cli.tailor --file jd.txt [--company Acme] [--role "Data Engineer"]
    pbpaste | python -m src.cli.tailor --company Acme --applicants 40

For adverts found outside the scraper (a referral, a careers page, a recruiter
message) or ones the run filtered out. No hard filter and no apply threshold
stops the build: the resume is ALWAYS generated, and the report says what the
filters and the threshold would have decided.

Writes the ``all_jobs`` and ``applied`` rows (so the dashboard and "Mark applied"
work) but no company cooldown, and sends no Telegram message unless --notify.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

import structlog

_ROOT = Path(__file__).resolve().parents[2]


def _configure_logging() -> None:
    # Warnings only: the report on stdout is the output, not the log.
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=logging.WARNING)
    for name in ("httpx", "httpcore", "sentence_transformers"):
        logging.getLogger(name).setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
    )


def _safe(name: str) -> str:
    return re.sub(r"[^\w .&()-]+", "", name).strip() or "Unknown"


def file_stem(company: str, role: str) -> str:
    """``Resume - Acme - Data Engineer``: what the operator sees in Downloads."""
    return f"Resume - {_safe(company)} - {_safe(role)}"


def report(t) -> str:
    """The score breakdown, the would-have-been filter verdicts and the files."""
    r = t.result
    kws = r.jd_keywords
    required = [k.token for k in kws if k.weight >= 1.0]
    nice = [k.token for k in kws if k.weight < 1.0]
    shown: set[str] = set().union(*(e.covered for e in r.entries)) if r.entries else set()

    lines = [
        f"{t.job.role} at {t.job.company}   [{t.job.job_id}]",
        f"parse: {'reused the stored parse' if t.reused_parse else 'new LLM parse'}"
        f" | {t.parsed.role_level}, {t.parsed.years_required} years asked",
        "",
        f"FINAL SCORE  {r.final_score:.3f}   (apply threshold {t.threshold:.3f})",
    ]
    if t.below_threshold:
        lines.append("  BELOW THRESHOLD: the scraper run would not have sent this job."
                     " Built anyway.")
    lines += [
        f"  fit                 {r.fit:.3f}",
        f"    lead entry        {r.lead_entry:.3f}  (similarity {r.similarity_scaled:.3f},"
        f" lead coverage {r.lead_entry_coverage:.3f})",
        f"    keyword coverage  {r.keyword_coverage:.3f}",
        f"    repetition        {r.keyword_repetition:.3f}",
        f"  applicant multiplier {r.success_prob:.3f}"
        f"  ({'unknown count' if t.job.applicants_count is None else f'{t.job.applicants_count} applicants'})",
        "",
        f"REQUIRED KEYWORDS  {sum(k in shown for k in required)}/{len(required)} shown",
        "  shown:   " + (", ".join(k for k in required if k in shown) or "none"),
        "  missing: " + (", ".join(k for k in required if k not in shown) or "none"),
    ]
    if nice:
        lines.append("  nice to have shown: "
                     + (", ".join(k for k in nice if k in shown) or "none"))
    if t.built.gap_skills:
        lines.append("  not anywhere in the profile: " + ", ".join(t.built.gap_skills))

    lines += ["", f"ENTRIES ({len(r.entries)}, in page order)"]
    seen: set[str] = set()
    for i, e in enumerate(r.entries, 1):
        added = sorted(e.covered - seen)
        seen |= e.covered
        lines.append(f"  {i}. {e.header_left}  [{e.kind}]  score {e.score:.3f},"
                     f" {len(e.bullets)} bullets")
        lines.append("       adds: " + (", ".join(added) if added else "nothing new"))

    lines += ["", "HARD FILTERS (information only, none applied)"]
    for v in t.verdicts:
        mark = "WOULD REJECT" if v.would_reject else "pass"
        lines.append(f"  {mark:<12} {v.name}: {v.detail}")

    if t.files or t.links:
        lines += ["", "FILES"]
        lines += [f"  {path}" for path in t.files.values()]
        lines += [f"  {ext} link: {url}" for ext, url in t.links.items()]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="src.cli.tailor", description=__doc__.split("\n\n")[0])
    ap.add_argument("--file", type=Path, help="advert text file (default: read stdin)")
    ap.add_argument("--company")
    ap.add_argument("--role")
    ap.add_argument("--applicants", type=int, help="applicant count, if the listing shows one")
    ap.add_argument("--url", help="the listing's URL, used as the apply link")
    ap.add_argument("--location")
    ap.add_argument("--out", type=Path, help="folder for the PDF/DOCX (default: tailor.output_dir)")
    ap.add_argument("--notify", action="store_true", help="also send the Telegram match message")
    args = ap.parse_args(argv)

    _configure_logging()
    if args.file:
        text = args.file.read_text(encoding="utf-8")
    elif sys.stdin.isatty():
        print("Paste the advert, then Ctrl-D:", file=sys.stderr)
        text = sys.stdin.read()
    else:
        text = sys.stdin.read()
    if not text.strip():
        print("error: the advert is empty", file=sys.stderr)
        return 2

    from src.config import resolve_endpoint_base_url, settings
    from src.endpoint.cache import get_or_build, prerender
    from src.llm.client import LLMError
    from src.state import master_profile
    from src.state.db import session_scope
    from src.tailor import tailor

    out_dir = args.out or (_ROOT / str(settings.tailor.output_dir))
    out_dir = Path(out_dir).expanduser()

    with session_scope() as session:
        master_profile.rebuild(session)
        try:
            t = tailor(
                session, text,
                company=args.company, role=args.role, applicants=args.applicants,
                url=args.url, location=args.location,
            )
        except LLMError as exc:
            print(f"error: the advert could not be parsed: {exc}", file=sys.stderr)
            return 1

        out_dir.mkdir(parents=True, exist_ok=True)
        stem = file_stem(t.job.company, t.job.role)
        for ext in ("pdf", "docx"):
            try:
                data, _ = get_or_build(t.job.job_id, ext, session)
            except Exception as exc:
                print(f"error: {ext} render failed: {exc}", file=sys.stderr)
                continue
            path = out_dir / f"{stem}.{ext}"
            path.write_bytes(data)
            t.files[ext] = path
        t.links = prerender(
            t.job.job_id, session,
            expires_seconds=int(settings.prerender.link_expiry_days) * 86400,
        )
        session.commit()

        if args.notify:
            from src.notifications import send_match_notification

            send_match_notification(
                job=t.job, parsed=t.parsed, result=t.result,
                gap_skills=t.built.gap_skills,
                endpoint_base_url=resolve_endpoint_base_url(str(settings.endpoint.base_url)),
                title_alias=t.built.title_alias, resume_urls=t.links,
            )

    print(report(t))
    return 0 if t.files else 1


if __name__ == "__main__":
    raise SystemExit(main())
