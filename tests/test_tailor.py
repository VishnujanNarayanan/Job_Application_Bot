"""Tailor a resume for a pasted advert (#20): no hard filters, no threshold.

The scoring and building themselves are the run's own (`score_job`,
`build_applied`, shared with `src.main`) and are tested there; these tests pin
what is different for a pasted advert -- the gates are reported, never applied;
the same text maps to the same stored job; no cooldown and no Telegram message.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src import tailor as tl
from src.llm.schemas import JDParsed
from src.scorer.keywords import Keyword
from src.state.models import AllJobs, Applied, CompanyCooldown, RenderCache

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
AD = """Testing Engineer\n\nRequired: 8+ years of ETL testing.\nStrong SQL querying skills."""


class FakeSession:
    """Just enough of a Session for `tailor`: rows keyed by (model, primary key)."""

    def __init__(self):
        self.rows: dict[tuple[type, str], object] = {}
        self.commits = 0

    def _key(self, obj):
        pk = obj.cache_key if isinstance(obj, RenderCache) else obj.job_id
        return (type(obj), pk)

    def get(self, model, key):
        return self.rows.get((model, key))

    def add(self, obj):
        self.rows[self._key(obj)] = obj

    def delete(self, obj):
        self.rows.pop(self._key(obj), None)

    def commit(self):
        self.commits += 1

    def scalar(self, *_a, **_k):
        return None  # company_last_notified: never notified

    def query(self, model):
        rows = [o for (m, _), o in self.rows.items() if m is model]
        return SimpleNamespace(filter=lambda *_a: list(rows))

    def of(self, model):
        return [o for (m, _), o in self.rows.items() if m is model]


def _parsed(years=8, skills=("SQL", "ETL testing")):
    return JDParsed(
        role_summary="Validate data pipelines and ETL transformations.",
        role_category="data",
        role_level="mid",
        years_required=years,
        required_skills=list(skills),
        nice_to_have=[],
        responsibilities=["validate pipelines"],
        job_type="fulltime",
    )


def _result(score=0.21):
    entry = SimpleNamespace(
        header_left="Data Engineer at Citesert", kind="work", score=0.61,
        bullets=[1, 2, 3], covered={"sql"},
    )
    return SimpleNamespace(
        final_score=score, fit=0.30, success_prob=0.825, lead_entry=0.5,
        similarity_scaled=0.4, lead_entry_coverage=0.5, keyword_coverage=0.5,
        keyword_repetition=1.0, entries=[entry],
        jd_keywords=(Keyword("sql", 1.0), Keyword("etl testing", 1.0)),
    )


@pytest.fixture
def stubbed(monkeypatch):
    """Stub the profile, the embedder and the scorer/builder; count parses."""
    calls = {"parse": 0, "selection": {"entries": ["a"]}}

    def fake_parse(job):
        calls["parse"] += 1
        return _parsed()

    def fake_build(profile, job, parsed, result):
        applied = Applied(
            job_id=job.job_id, selection_json=dict(calls["selection"]),
            template_version="v1", final_score=result.final_score, user_status="pending",
        )
        return tl.Built(SimpleNamespace(entries=[]), applied, ["ETL testing"], "Data Engineer")

    monkeypatch.setattr("src.state.master_profile.load_profile",
                        lambda s: SimpleNamespace(work=[], projects=[]))
    monkeypatch.setattr("src.scorer.embeddings.embed_documents", lambda texts: [[0.0]] * len(texts))
    monkeypatch.setattr(tl, "score_job", lambda *a, **k: _result())
    monkeypatch.setattr(tl, "build_applied", fake_build)
    calls["parse_fn"] = fake_parse
    return calls


def _run(session, stubbed, text=AD, **kw):
    return tl.tailor(session, text, parse_fn=stubbed["parse_fn"], now=NOW, **kw)


# --- id ----------------------------------------------------------------------

def test_the_same_advert_gets_the_same_id_whatever_its_line_breaks():
    a = tl.manual_job_id("Testing Engineer\n\nStrong SQL")
    assert a == tl.manual_job_id("  Testing   Engineer Strong\nSQL ")
    assert a.startswith("manual-") and len(a) == len("manual-") + 12
    assert a != tl.manual_job_id("Testing Engineer\n\nStrong Python")


# --- gates reported, never applied --------------------------------------------

def test_an_advert_the_years_filter_rejects_still_gets_a_resume(stubbed):
    s = FakeSession()
    t = _run(s, stubbed, company="NTT DATA", role="Testing Engineer")

    assert len(s.of(Applied)) == 1, "the resume selection must be stored"
    years = next(v for v in t.verdicts if v.name == "years ceiling")
    assert years.would_reject and "asks 8" in years.detail
    assert t.job.site == "manual" and t.job.outcome == "matched"


def test_a_score_below_the_threshold_still_builds_and_says_so(stubbed):
    s = FakeSession()
    t = _run(s, stubbed)
    assert t.result.final_score < t.threshold
    assert t.below_threshold
    assert s.of(Applied)
    assert "BELOW THRESHOLD" in _report(t)


def test_no_cooldown_is_written(stubbed):
    s = FakeSession()
    _run(s, stubbed, company="NTT DATA")
    assert s.of(CompanyCooldown) == []


def test_company_and_role_default_to_unknown(stubbed):
    t = _run(FakeSession(), stubbed)
    assert (t.job.company, t.job.role) == ("Unknown", "Unknown")


def test_an_empty_advert_is_refused(stubbed):
    with pytest.raises(ValueError):
        _run(FakeSession(), stubbed, text="  \n ")


# --- pasting twice -------------------------------------------------------------

def test_pasting_the_same_advert_twice_reuses_the_job_and_its_parse(stubbed):
    s = FakeSession()
    first = _run(s, stubbed)
    second = _run(s, stubbed, text=AD.replace("\n\n", "\n"))

    assert stubbed["parse"] == 1, "the second paste must not spend an LLM call"
    assert second.reused_parse and not first.reused_parse
    assert len(s.of(AllJobs)) == 1 and len(s.of(Applied)) == 1


def test_a_re_paste_keeps_the_operators_status(stubbed):
    s = FakeSession()
    t = _run(s, stubbed)
    s.get(Applied, t.job.job_id).user_status = "applied"
    _run(s, stubbed)
    assert s.get(Applied, t.job.job_id).user_status == "applied"


def test_a_changed_selection_drops_the_stale_render(stubbed):
    """The render cache is keyed on job + template version, not on the
    selection, so a re-paste that selects differently must not serve the old file."""
    s = FakeSession()
    t = _run(s, stubbed)
    s.add(RenderCache(cache_key=f"{t.job.job_id}_v1_pdf", job_id=t.job.job_id, format="pdf"))

    _run(s, stubbed)                     # same selection: render kept
    assert len(s.of(RenderCache)) == 1
    stubbed["selection"] = {"entries": ["b"]}
    _run(s, stubbed)                     # new selection: render dropped
    assert s.of(RenderCache) == []


def test_flags_on_a_re_paste_correct_the_stored_job(stubbed):
    s = FakeSession()
    _run(s, stubbed)
    t = _run(s, stubbed, company="NTT DATA", applicants=40)
    assert t.job.company == "NTT DATA" and t.job.applicants_count == 40


def test_the_stored_parse_round_trips():
    from src.parser import apply_to_row

    job = AllJobs(job_id="manual-x", company="A", role="B", site="manual", jd_text=AD)
    apply_to_row(job, _parsed())
    back = tl.parsed_from_row(job)
    assert back is not None
    assert back.required_skills == _parsed().required_skills
    assert back.years_required == 8


def test_the_stored_skill_lists_come_back_verbatim():
    """`parse` adds pool skills AFTER validation, so validating the stored list
    again can drop some; the re-paste must score on exactly what was stored."""
    from src.parser import apply_to_row

    job = AllJobs(job_id="manual-x", company="A", role="B", site="manual", jd_text=AD)
    apply_to_row(job, _parsed())
    job.required_skills = ["SQL", "data", "Data pipelines", "ETL/ELT"]
    assert tl.parsed_from_row(job).required_skills == job.required_skills


# --- report ------------------------------------------------------------------

def _report(t):
    from src.cli.tailor import report

    return report(t)


def test_the_report_carries_the_full_breakdown(stubbed):
    text = _report(_run(FakeSession(), stubbed, company="NTT DATA", role="Testing Engineer"))
    for part in ("FINAL SCORE", "fit", "lead entry", "keyword coverage", "repetition",
                 "applicant multiplier", "REQUIRED KEYWORDS  1/2", "missing: etl testing",
                 "ENTRIES (1", "adds: sql", "HARD FILTERS", "WOULD REJECT years ceiling",
                 "not anywhere in the profile: ETL testing"):
        assert part in text, part


def test_the_file_name_says_which_job_it_is():
    from src.cli.tailor import file_stem

    assert file_stem("NTT DATA", "Testing Engineer / QA") == "Resume - NTT DATA - Testing Engineer  QA"


# --- CLI: no Telegram unless asked -------------------------------------------

@pytest.mark.parametrize("notify", [False, True])
def test_telegram_is_sent_only_with_notify(monkeypatch, tmp_path, stubbed, notify):
    from contextlib import contextmanager

    from src.cli import tailor as cli

    sent = []
    s = FakeSession()

    @contextmanager
    def scope():
        yield s

    monkeypatch.setattr("src.state.db.session_scope", scope)
    monkeypatch.setattr("src.state.master_profile.rebuild", lambda session: None)
    real = tl.tailor
    monkeypatch.setattr("src.tailor.tailor", lambda session, text, **kw: real(
        session, text, parse_fn=stubbed["parse_fn"], now=NOW, **kw))
    monkeypatch.setattr("src.endpoint.cache.get_or_build",
                        lambda job_id, ext, session: (b"bytes", "x"))
    monkeypatch.setattr("src.endpoint.cache.prerender", lambda *a, **k: {})
    monkeypatch.setattr("src.notifications.send_match_notification",
                        lambda **kw: sent.append(kw))
    ad = tmp_path / "jd.txt"
    ad.write_text(AD)

    argv = ["--file", str(ad), "--out", str(tmp_path / "out")] + (["--notify"] if notify else [])
    assert cli.main(argv) == 0
    assert bool(sent) is notify
    assert sorted(p.suffix for p in (tmp_path / "out").iterdir()) == [".docx", ".pdf"]


def test_a_pasted_advert_ends_at_a_line_reading_END():
    """Ctrl-D was swallowed after a paste (cursor at the end of the last line)."""
    import io

    from src.cli.tailor import read_until_end

    stream = io.StringIO("Testing Engineer\n\nStrong SQL\n  END  \nnot part of it\n")
    assert read_until_end(stream) == "Testing Engineer\n\nStrong SQL\n"
    assert read_until_end(io.StringIO("no marker, input just ends")) == "no marker, input just ends"


def test_every_backend_step_is_reported(stubbed):
    """The operator sees the work happen: save, parse (or reuse), score, build."""
    s = FakeSession()
    first: list[str] = []
    tl.tailor(s, AD, parse_fn=stubbed["parse_fn"], now=NOW, progress=first.append)
    text = "\n".join(first)
    for part in ("New advert", "Reading the advert with the AI parser", "required skills",
                 "Loaded the profile", "Scoring every entry", "score 0.210",
                 "Building the resume selection", "1 entries, 3 bullets", "Saved"):
        assert part in text, part

    again: list[str] = []
    tl.tailor(s, AD, parse_fn=stubbed["parse_fn"], now=NOW, progress=again.append)
    assert any("Seen this advert before" in m for m in again)
    assert any("no AI call" in m for m in again)


def test_the_llm_observer_reports_providers_and_fallbacks():
    from src.llm.client import _emit, observe

    seen = []
    with observe(lambda event, fields: seen.append((event, fields["provider"]))):
        _emit("trying", provider="groq", model="m")
        _emit("served", provider="groq", seconds=1.0)
    _emit("trying", provider="outside", model="m")      # no observer: ignored
    assert seen == [("trying", "groq"), ("served", "groq")]
