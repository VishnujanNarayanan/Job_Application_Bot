"""Easy Apply bot (src/autoapply/run.py): when it runs and when it holds back."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.autoapply import run

IST = ZoneInfo("Asia/Kolkata")


@pytest.mark.parametrize("when,expected", [
    (datetime(2026, 11, 30, 9, 0, tzinfo=IST), True),     # Monday 09:00
    (datetime(2026, 11, 30, 17, 59, tzinfo=IST), True),
    (datetime(2026, 11, 30, 18, 0, tzinfo=IST), False),   # 18:00 is outside
    (datetime(2026, 11, 30, 8, 59, tzinfo=IST), False),
    (datetime(2026, 11, 28, 11, 0, tzinfo=IST), False),   # Saturday
])
def test_working_hours(when, expected) -> None:
    assert run.in_window(when) is expected


def test_wsl_path() -> None:
    from src.autoapply.edge import wsl_path
    assert wsl_path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe") == \
        "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "STATE", tmp_path / "state.json")
    return tmp_path / "state.json"


def _cfg(monkeypatch, **over):
    keys = ("enabled", "dry_run", "search_term", "title_must_contain", "run_pipeline_first", "per_run",
            "max_per_day", "every_minutes", "days", "hours", "timezone", "max_match_age_hours")
    base = {k: getattr(run.settings.autoapply, k) for k in keys}
    base.update(over)

    class C(dict):
        __getattr__ = dict.__getitem__

    monkeypatch.setattr(run, "cfg", lambda: C(base))


def test_off_switch_and_window_hold_it_back(state_file, monkeypatch) -> None:
    _cfg(monkeypatch, enabled=False, dry_run=False)
    assert run.run_once() == {"ran": False, "why": "autoapply.enabled is false"}
    _cfg(monkeypatch, enabled=True, dry_run=False)
    monkeypatch.setattr(run, "in_window", lambda at=None: False)
    assert run.run_once()["why"] == "outside working hours"


def test_daily_cap(state_file, monkeypatch) -> None:
    _cfg(monkeypatch, enabled=True, dry_run=False, max_per_day=20)
    monkeypatch.setattr(run, "in_window", lambda at=None: True)
    day = run.now_local().strftime("%Y-%m-%d")
    run.save_state({"tried": {}, "days": {day: {"applied": 20}}})
    assert run.run_once()["why"] == "daily cap reached"


def test_linkedin_id() -> None:
    assert run.linkedin_id("linkedin-li-4495075579", "") == "4495075579"
    assert run.linkedin_id("x", "https://www.linkedin.com/jobs/view/4493891384/") == "4493891384"


def test_only_the_bot_asks_linkedin_for_easy_apply(monkeypatch) -> None:
    import types

    from src.scraper import jobspy_wrapper

    calls = []

    def fake_scrape_jobs(**kw):
        calls.append(kw)
        return None

    for group, ea in ((["linkedin"], True), (["naukri"], True), (["linkedin"], False)):
        jobspy_wrapper._scrape_with_retry(fake_scrape_jobs, site_group=group, search_term="data engineer",
                                          country="india", location="India", results_wanted=5, hours_old=1,
                                          fetch_description=False, proxies=None, max_retries=1,
                                          backoff_base_seconds=0, easy_apply=ea)
    assert [c.get("easy_apply") for c in calls] == [True, None, None]

    seen = {}
    monkeypatch.setattr(run.subprocess, "run", lambda args, **kw: seen.setdefault("args", args))
    run.run_pipeline()
    assert "--easy-apply" in seen["args"] and "--hours-old" in seen["args"]
