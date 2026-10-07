"""Readable terminal output for a pipeline run.

``python -m src.main`` logs structured JSON, one object per event, which is
what CloudWatch and the Actions log want but is unreadable to a person
watching a run: a single LLM error dumped the model's whole rejected reply
onto the screen. When stderr is a terminal, this renderer prints instead:

    09:52:39  ▶ Run started (live)
    09:52:57  🔎 Searching "junior backend engineer" (picked by hand)
    09:54:01  ✓ Scraped 37 jobs (37 new, 12 remote)
    09:54:02  ✓ Pre-checks: 24 of 37 go to the LLM
    09:54:13  [2/24] Junior Software Engineer @ Vi-Scan
                groq 4.1s · score 0.62 · ✅ MATCH · 24 applicants
                📨 Sent to Telegram
    09:58:10  ■ Done in 5m31s: 6 matched, 31 skipped

Kept: run milestones, one heading per job, which LLM answered and how fast,
retries / fallbacks / deadlines (one line each, error text cut to the
provider's own message), the score and verdict, Telegram sends, every
warning and error, and an end-of-run summary.

Cut: per-step resume plumbing (docx assembled, PDF converted, S3 put and
presign, prerendered), skill-recovery bookkeeping, client construction,
index export, applicant refreshes -- and, in src.main, third-party noise
(sentence-transformers' progress bars and load messages, JobSpy's "finished
scraping").

``LOG_FORMAT=json`` forces the old output in a terminal; anything that isn't
a terminal (Actions, the dashboard's Run button, cron) always gets JSON.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime

import structlog

# Events whose information is already shown elsewhere, or that only matter
# when debugging -- the full JSON (LOG_FORMAT=json, CloudWatch) still has them.
_DROPPED = frozenset({
    "llm_client_built", "llm_fallback_succeeded", "pool_skills_recovered",
    "vocabulary_skills_recovered", "selection_built", "docx_assembled",
    "pdf_converted", "pdf_convert_start", "s3_cache_put", "s3_cache_hit",
    "s3_cache_miss", "s3_presigned", "resume_rendered", "prerendered",
    "index_exported", "index_export_skipped", "applicants_refreshed",
    "scrape_start", "scrape_done", "backlog_refreshed", "qualifications_loaded",
    "keyword_prose_match", "complete_fn_ignored", "skill_rejected",
    "qualifications_title_unknown",
})

_PROVIDER_MESSAGE = re.compile(r"""['"]message['"]\s*:\s*['"](.+?)['"]\s*[,}]""")

_RESET, _BOLD, _DIM = "\033[0m", "\033[1m", "\033[2m"
_GREEN, _YELLOW, _RED, _CYAN = "\033[32m", "\033[33m", "\033[31m", "\033[36m"

_INDENT = " " * 12


def short_error(text: object, limit: int = 120) -> str:
    """The provider's own message if one is embedded, else the first line.

    Groq's tool_use_failed carries the model's entire rejected reply in
    ``failed_generation``; the useful part is the one-line ``message``.
    """
    raw = str(text or "")
    match = _PROVIDER_MESSAGE.search(raw)
    line = match.group(1) if match else raw.strip().splitlines()[0] if raw.strip() else ""
    line = re.sub(r"^Error code: \d+ - ", "", line).strip()
    return line if len(line) <= limit else line[: limit - 1] + "…"


class ConsoleRenderer:
    """structlog's final processor: an event in, one readable line (or none) out."""

    def __init__(self, colour: bool | None = None):
        self.colour = sys.stderr.isatty() if colour is None else colour
        self._started: datetime | None = None
        self._llm: str | None = None  # "groq 4.1s" for the job being processed

    # -- helpers -----------------------------------------------------------

    def _c(self, code: str, text: str) -> str:
        return f"{code}{text}{_RESET}" if self.colour else text

    def _stamp(self, event: dict) -> tuple[str, datetime | None]:
        try:
            when = datetime.fromisoformat(str(event.get("timestamp")).replace("Z", "+00:00"))
            return when.astimezone().strftime("%H:%M:%S"), when
        except ValueError:
            return " " * 8, None

    def _line(self, stamp: str, text: str) -> str:
        return f"{self._c(_DIM, stamp)}  {text}"

    def _detail(self, text: str) -> str:
        return f"{_INDENT}{text}"

    # -- rendering ---------------------------------------------------------

    def __call__(self, _logger, method: str, event: dict) -> str:
        name = str(event.get("event", ""))
        if name in _DROPPED:
            raise structlog.DropEvent
        stamp, when = self._stamp(event)
        text = self._render(name, event, stamp, when)
        if text is None:
            level = event.get("level", method)
            if level not in ("warning", "error", "critical"):
                raise structlog.DropEvent
            text = self._generic(name, event, level, stamp)
        return text

    def _render(self, name: str, e: dict, stamp: str, when) -> str | None:  # noqa: C901
        line, detail, c = self._line, self._detail, self._c

        if name == "run_started":
            self._started = when
            mode = "dry run, test chat only" if e.get("dry_run") else "live"
            return line(stamp, c(_BOLD, f"▶ Run started ({mode})"))
        if name == "terms_chosen":
            how = "picked by hand" if e.get("source") == "manual" else "next in rotation"
            terms = ", ".join(f'"{t}"' for t in e.get("terms") or [])
            return line(stamp, f"🔎 Searching {terms} ({how})")
        if name == "scrape_term_done":
            return line(stamp, c(_GREEN, "✓ ") + f'Scraped "{e.get("term")}": {e.get("raw_count")} jobs '
                        f'({e.get("new_count")} new, {e.get("remote_count")} remote)')
        if name == "backlog_loaded":
            return line(stamp, f"↺ {e.get('count')} jobs carried over from earlier runs")
        if name == "prechecks_done":
            text = c(_GREEN, "✓ ") + f"Pre-checks: {e.get('to_parse')} of {e.get('checked')} go to the LLM"
            skipped = _outcomes(e.get("outcomes") or {})
            return line(stamp, text) + (("\n" + detail(c(_DIM, "skipped: " + skipped))) if skipped else "")
        if name == "master_profile_rebuilt":
            return line(stamp, "↻ Master profile rebuilt")

        # -- per job --
        if name == "job_started":
            self._llm = None
            return line(stamp, c(_BOLD, f"[{e.get('n')}/{e.get('of')}] ")
                        + f"{e.get('role')} @ {e.get('company')}")
        if name == "llm_served":
            secs = e.get("seconds")
            self._llm = f"{e.get('provider')}" + (f" {secs:.1f}s" if isinstance(secs, (int, float)) else "")
            raise structlog.DropEvent  # shown on the job's verdict line instead
        if name == "llm_retry":
            wait = e.get("wait_seconds")
            return detail(c(_YELLOW, f"↻ {_slot(e)} retry {e.get('attempt')}/{e.get('max_attempts')}"
                            f" in {wait}s: ") + short_error(e.get("error")))
        if name == "llm_failure":
            return detail(c(_RED, f"✗ {e.get('provider') or _slot(e)}: ") + short_error(e.get("error")))
        if name == "llm_fallback_engaged":
            return detail(c(_YELLOW, f"→ trying {e.get('to_provider')}"))
        if name == "llm_deadline_exceeded":
            return detail(c(_YELLOW, f"⏱ {e.get('provider')}: no reply in {e.get('deadline_seconds'):g}s"))
        if name == "llm_budget_exhausted":
            return detail(c(_RED, f"✗ {e.get('provider')} is out of quota/budget: ") + short_error(e.get("error")))
        if name == "llm_provider_unusable":
            return detail(c(_RED, f"✗ {e.get('provider')} unusable this run: ") + short_error(e.get("error")))
        if name == "parse_failed_carried_over":
            return detail(c(_RED, "✗ Every LLM failed for this job, skipped"))
        if name == "job_filtered":
            why = {"JOB_TYPE_DISALLOWED": f"not full-time ({e.get('value')})",
                   "HARD_FILTER_LAYER_3": f"needs {e.get('value')} years"}.get(e.get("reason"), e.get("reason"))
            return detail(self._with_llm(c(_DIM, f"filtered: {why}")))
        if name == "job_scored":
            score, threshold = e.get("score"), e.get("threshold")
            verdict = c(_GREEN + _BOLD, "✅ MATCH") if e.get("matched") else c(_DIM, f"below {threshold}")
            applicants = e.get("applicants")
            parts = [f"score {score:.2f}" if isinstance(score, (int, float)) else f"score {score}", verdict]
            if applicants is not None:
                parts.append(f"{applicants} applicants")
            return detail(self._with_llm(" · ".join(parts)))
        if name == "notification_sent":
            return detail(c(_CYAN, "📨 Sent to Telegram"))
        if name == "dry_run_match":
            return detail(c(_CYAN, "📨 Would notify (dry run)"))
        if name == "prerender_failed":
            return detail(c(_YELLOW, f"⚠ resume {e.get('fmt')} not pre-rendered: ") + short_error(e.get("error")))
        if name == "notification_error":
            return detail(c(_RED, "✗ Telegram send failed: ") + short_error(e.get("error")))

        # -- end of run --
        if name == "time_budget_reached":
            return line(stamp, c(_YELLOW, f"⏱ Time budget reached after {e.get('minutes')} min; "
                                         f"{e.get('not_reached')} jobs not started"))
        if name == "run_aborted":
            return line(stamp, c(_RED, f"✗ Run stopped: {e.get('reason')} "
                                      f"({e.get('processed')} done, {e.get('remaining')} left)"))
        if name == "telegram_taps_recorded":
            return line(stamp, f"✓ Recorded {e.get('count')} Telegram button tap(s)")
        if name == "run_complete":
            took = ""
            if self._started and when:
                secs = int((when - self._started).total_seconds())
                took = f" in {secs // 60}m{secs % 60:02d}s"
            head = c(_BOLD, f"■ Done{took}: {e.get('matched')} matched, {e.get('skipped')} skipped, "
                            f"{e.get('scraped')} scraped")
            skipped = _outcomes(e.get("outcomes") or {})
            return line(stamp, head) + (("\n" + detail(c(_DIM, "skipped: " + skipped))) if skipped else "")
        if name in ("run_skipped", "run_cancelled"):
            return line(stamp, f"■ Run not started ({e.get('reason')})")
        return None

    def _with_llm(self, text: str) -> str:
        """Prefix the provider that parsed this job, once."""
        prefix, self._llm = (f"{self._llm} · " if self._llm else ""), None
        return prefix + text

    def _generic(self, name: str, e: dict, level: str, stamp: str) -> str:
        colour = _RED if level in ("error", "critical") else _YELLOW
        mark = "✗" if colour == _RED else "⚠"
        fields = {k: v for k, v in e.items()
                  if k not in ("event", "level", "timestamp", "exception", "exc_info")}
        if "error" in fields:
            fields["error"] = short_error(fields["error"])
        rest = " ".join(f"{k}={v}" for k, v in fields.items())
        return self._line(stamp, self._c(colour, f"{mark} {name}") + (f" {rest}" if rest else ""))


def _slot(e: dict) -> str:
    return str(e.get("provider") or e.get("which") or "LLM")


def _outcomes(outcomes: dict) -> str:
    """``18 score below threshold · 5 too many applicants`` in pipeline order."""
    from src.notifications import _OUTCOME_LABELS

    labels = dict(_OUTCOME_LABELS)
    ordered = [k for k, _ in _OUTCOME_LABELS if outcomes.get(k)]
    ordered += [k for k in outcomes if k not in labels and outcomes.get(k)]
    return " · ".join(f"{outcomes[k]} {labels.get(k, k).lower()}" for k in ordered)
