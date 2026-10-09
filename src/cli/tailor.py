"""CLI — tailor a resume for a pasted job advert (#20).

    python -m src.cli.tailor --file jd.txt [--company Acme] [--role "Data Engineer"]
    python -m src.cli.tailor https://www.linkedin.com/jobs/view/4475971015/
    python -m src.cli.tailor --url https://www.linkedin.com/jobs/view/4475971015/
    python -m src.cli.tailor --clip --company Acme   # advert from the clipboard
    python -m src.cli.tailor --company Acme          # paste it, then END on its own line

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
import time
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


END_MARK = "END"


def read_until_end(stream) -> str:
    """Read pasted lines until a line that is just ``END``, or end of input.

    Ctrl-D alone was unreliable: it only ends input at the start of an empty
    line, and a paste leaves the cursor at the end of its last line, so the
    first press was swallowed and the command looked hung.
    """
    lines = []
    for line in stream:
        if line.strip() == END_MARK:
            break
        lines.append(line)
    return "".join(lines)


def read_clipboard() -> str:
    """The clipboard's text: Windows' under WSL, else xclip / pbpaste."""
    import shutil
    import subprocess

    if shutil.which("powershell.exe"):
        cmd = ["powershell.exe", "-NoProfile", "-Command", "Get-Clipboard -Raw"]
    elif shutil.which("pbpaste"):
        cmd = ["pbpaste"]
    elif shutil.which("xclip"):
        cmd = ["xclip", "-selection", "clipboard", "-o"]
    else:
        raise RuntimeError("no clipboard tool found (powershell.exe, pbpaste or xclip)")
    out = subprocess.run(cmd, capture_output=True, timeout=20, check=True)
    return out.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n")


def _safe(name: str) -> str:
    return re.sub(r"[^\w .&()-]+", "", name).strip() or "Unknown"


def file_stem(company: str, role: str) -> str:
    """``Resume - Acme - Data Engineer``: what the operator sees in Downloads."""
    return f"Resume - {_safe(company)} - {_safe(role)}"


def report(t) -> str:
    """The score breakdown, the would-have-been filter verdicts and the files."""
    from src.tailor import breakdown

    b = breakdown(t)
    applicants = ("unknown count" if b["applicants"] is None
                  else f"{b['applicants']} applicants")
    lines = [
        f"{b['role']} at {b['company']}   [{b['job_id']}]",
        f"parse: {'reused the stored parse' if b['reused_parse'] else 'new LLM parse'}"
        f" | {b['role_level']}, {b['years_required']} years asked",
        "",
        f"FINAL SCORE  {b['final_score']:.3f}   (apply threshold {b['threshold']:.3f})",
    ]
    if b["below_threshold"]:
        lines.append("  BELOW THRESHOLD: the scraper run would not have sent this job."
                     " Built anyway.")
    lines += [
        f"  fit                 {b['fit']:.3f}",
        f"    lead entry        {b['lead_entry']:.3f}  (similarity {b['similarity']:.3f},"
        f" lead coverage {b['lead_coverage']:.3f})",
        f"    keyword coverage  {b['keyword_coverage']:.3f}",
        f"    repetition        {b['repetition']:.3f}",
        f"  applicant multiplier {b['applicant_multiplier']:.3f}  ({applicants})",
        "",
        f"REQUIRED KEYWORDS  {len(b['required_shown'])}/{b['required_total']} shown",
        "  shown:   " + (", ".join(b["required_shown"]) or "none"),
        "  missing: " + (", ".join(b["required_missing"]) or "none"),
    ]
    if b["nice_total"]:
        lines.append("  nice to have shown: " + (", ".join(b["nice_shown"]) or "none"))
    if b["not_in_profile"]:
        lines.append("  not anywhere in the profile: " + ", ".join(b["not_in_profile"]))

    lines += ["", f"ENTRIES ({len(b['entries'])}, in page order)"]
    for i, e in enumerate(b["entries"], 1):
        lines.append(f"  {i}. {e['header']}  [{e['kind']}]  score {e['score']:.3f},"
                     f" {e['bullets']} bullets")
        lines.append("       adds: " + (", ".join(e["adds"]) or "nothing new"))

    lines += ["", "HARD FILTERS (information only, none applied)"]
    for v in b["filters"]:
        mark = "WOULD REJECT" if v["would_reject"] else "pass"
        lines.append(f"  {mark:<12} {v['name']}: {v['detail']}")

    if t.files or t.links:
        lines += ["", "FILES"]
        lines += [f"  {path}" for path in t.files.values()]
        lines += [f"  {ext} link: {url}" for ext, url in t.links.items()]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="src.cli.tailor", description=__doc__.split("\n\n")[0])
    ap.add_argument("link", nargs="?",
                    help="a LinkedIn job link, instead of the advert text")
    ap.add_argument("--file", type=Path, help="advert text file (default: paste it in)")
    ap.add_argument("--clip", action="store_true",
                    help="read the advert from the clipboard (Windows clipboard under WSL)")
    ap.add_argument("--company")
    ap.add_argument("--role")
    ap.add_argument("--applicants", type=int, help="applicant count, if the listing shows one")
    ap.add_argument("--url", help="the listing's URL. On its own (no advert text) a LinkedIn"
                                  " job link is fetched; with text it is the apply link")
    ap.add_argument("--location")
    ap.add_argument("--out", type=Path, help="folder for the PDF/DOCX (default: tailor.output_dir)")
    ap.add_argument("--notify", action="store_true", help="also send the Telegram match message")
    args = ap.parse_args(argv)

    _configure_logging()
    if args.link:
        text = args.link
    elif args.url and not args.file and not args.clip:
        # --url alone is the source. Only a LinkedIn link can be fetched; for
        # any other site the advert text is needed, and the url becomes its
        # apply link.
        from src.scraper.jobspy_wrapper import linkedin_job_id

        if not linkedin_job_id(args.url):
            print("error: only a LinkedIn job link can be fetched. Give the advert text too"
                  " (--file, --clip or paste it) and --url becomes its apply link.",
                  file=sys.stderr)
            return 2
        text = args.url
    elif args.file:
        text = args.file.read_text(encoding="utf-8")
    elif args.clip:
        try:
            text = read_clipboard()
        except (OSError, RuntimeError) as exc:
            print(f"error: could not read the clipboard: {exc}", file=sys.stderr)
            return 2
    elif sys.stdin.isatty():
        print(f"Paste the advert, then type {END_MARK} on a line of its own and press Enter:",
              file=sys.stderr)
        text = read_until_end(sys.stdin)
    else:
        text = sys.stdin.read()
    if not text.strip():
        print("error: the advert is empty", file=sys.stderr)
        return 2

    from src.config import resolve_endpoint_base_url, settings
    from src.endpoint.cache import get_or_build, prerender
    from src.llm.client import LLMError
    from src.scraper.jobspy_wrapper import LinkedInFetchError
    from src.state import master_profile
    from src.state.db import session_scope
    from src.tailor import tailor

    started = time.monotonic()

    def say(message: str) -> None:
        # stderr, flushed: the steps show as they happen, and stdout stays the
        # report alone so it can still be piped or saved.
        print(f"[{time.monotonic() - started:5.1f}s] {message}", file=sys.stderr, flush=True)

    out_dir = args.out or (_ROOT / str(settings.tailor.output_dir))
    out_dir = Path(out_dir).expanduser()

    with session_scope() as session:
        say("Checking the master profile is current...")
        master_profile.rebuild(session)
        try:
            t = tailor(
                session, text,
                company=args.company, role=args.role, applicants=args.applicants,
                # A fetched link stores its clean canonical URL, not the tracking one.
                url=None if text == args.url else args.url,
                location=args.location, progress=say,
            )
        except LLMError as exc:
            print(f"error: the advert could not be parsed: {exc}", file=sys.stderr)
            return 1
        except LinkedInFetchError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        out_dir.mkdir(parents=True, exist_ok=True)
        stem = file_stem(t.job.company, t.job.role)
        for ext in ("pdf", "docx"):
            say(f"Rendering the {ext.upper()}...")
            try:
                data, _ = get_or_build(t.job.job_id, ext, session)
            except Exception as exc:
                print(f"error: {ext} render failed: {exc}", file=sys.stderr)
                continue
            path = out_dir / f"{stem}.{ext}"
            path.write_bytes(data)
            t.files[ext] = path
        say("Making download links...")
        t.links = prerender(
            t.job.job_id, session,
            expires_seconds=int(settings.prerender.link_expiry_days) * 86400,
        )
        session.commit()

        if args.notify:
            from src.notifications import send_match_notification

            say("Sending the Telegram message...")
            send_match_notification(
                job=t.job, parsed=t.parsed, result=t.result,
                gap_skills=t.built.gap_skills,
                endpoint_base_url=resolve_endpoint_base_url(str(settings.endpoint.base_url)),
                title_alias=t.built.title_alias, resume_urls=t.links,
            )

    say("Done")
    print(file=sys.stderr)
    print(report(t))
    return 0 if t.files else 1


if __name__ == "__main__":
    raise SystemExit(main())
