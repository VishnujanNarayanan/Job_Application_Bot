"""The Easy Apply bot finds its jobs in Edge: LinkedIn's job list -> JobSpy-shaped rows."""

import json

from src.autoapply import discover, run
from src.scraper import jobspy_wrapper

# Shaped like LinkedIn's jobs-guest list and posting pages; made-up data.
LIST = """
<li><div class="base-card" data-entity-urn="urn:li:jobPosting:4495381210">
  <a class="base-card__full-link" href="https://in.linkedin.com/jobs/view/x-4495381210">x</a>
  <h3 class="base-search-card__title"> Snowflake Data Architect </h3>
  <h4 class="base-search-card__subtitle"><a href="#">Acme Data</a></h4>
  <span class="job-search-card__location">Pune, Maharashtra, India</span>
  <time datetime="2026-11-28">27 minutes ago</time></div></li>
<li><div class="base-card" data-entity-urn="urn:li:jobPosting:4494256245">
  <h3 class="base-search-card__title">Data Engineer</h3>
  <h4 class="base-search-card__subtitle">Beta Labs</h4>
  <span class="job-search-card__location">Bengaluru, Karnataka, India</span>
  <time datetime="2026-11-28">40 minutes ago</time></div></li>
"""
POSTING = """
<h2 class="top-card-layout__title">Snowflake Data Architect</h2>
<span class="num-applicants__caption">Be among the first 25 applicants</span>
<div class="show-more-less-html__markup"><p>Build <strong>Snowflake</strong> pipelines in Python.</p></div>
<ul><li class="description__job-criteria-item"><h3>Seniority level</h3><span>Mid-Senior level</span></li>
<li class="description__job-criteria-item"><h3>Employment type</h3><span>Full-time</span></li></ul>
"""


def test_search_url_has_easy_apply_and_the_hour() -> None:
    url = discover.search_url("data engineer", "India", 3600, start=10)
    assert "jobs-guest/jobs/api/seeMoreJobPostings/search" in url
    for part in ("keywords=data+engineer", "location=India", "f_TPR=r3600", "f_AL=true", "start=10"):
        assert part in url


def test_cards_become_pipeline_jobs(tmp_path) -> None:
    cards = discover.parse_cards(LIST)
    assert [c["id"] for c in cards] == ["4495381210", "4494256245"]
    assert cards[0]["company"] == "Acme Data" and cards[0]["date_posted"] == "2026-11-28"
    row = discover.to_row(cards[0], POSTING)
    assert row["id"] == "li-4495381210" and row["site"] == "linkedin"
    assert "Snowflake" in row["description"] and row["applicants_text"].startswith("Be among")
    assert row["job_level"] == "mid-senior level" and row["job_type"] == "fulltime"
    bare = discover.to_row(cards[1], None)  # no posting page: still a job, no description
    assert bare["description"] is None

    jobs = jobspy_wrapper.jobs_from_file(discover.write([row, bare, row], tmp_path / "found.json"))
    assert [j.job_id for j in jobs] == ["linkedin-li-4495381210", "linkedin-li-4494256245"]
    assert jobs[0].applicants_count == 24 and jobs[0].role == "Snowflake Data Architect"
    assert run.linkedin_id(jobs[0].job_id, jobs[0].job_url) == "4495381210"


def test_found_jobs_go_to_the_pipeline_without_a_scrape(monkeypatch, tmp_path) -> None:
    seen = {}
    monkeypatch.setattr(run.subprocess, "run", lambda args, **kw: seen.setdefault("args", args))
    path = tmp_path / "found.json"
    path.write_text(json.dumps([]))
    run.run_pipeline(path)
    assert seen["args"][-2:] == ["--jobs-file", str(path)] and "--easy-apply" not in seen["args"]


def test_the_bot_ignores_the_applicant_count(monkeypatch, tmp_path) -> None:
    """Applying costs the operator nothing, so a crowded posting is neither
    pre-skipped nor marked down in the bot's runs; normal runs are unchanged."""
    import types

    import src.scorer.selector as selector
    import src.tailor as tailor

    seen = []

    def fake_context(parsed, **kw):
        seen.append(kw["applicants_count"])
        raise StopIteration  # nothing after the context matters here

    monkeypatch.setattr(selector, "build_jd_context", fake_context)
    job = types.SimpleNamespace(posted_at=None, scraped_at=None, applicants_count=180, jd_text="x",
                                role="Data Engineer", site="linkedin")
    for ignore in (False, True):
        try:
            tailor.score_job(None, job, None, ignore_applicants=ignore)
        except StopIteration:
            pass
    assert seen == [180, 0]

    from src.scorer.apply_decision import applicant_multiplier
    assert applicant_multiplier(0) == applicant_multiplier(1)  # 0 = the full early-posting boost

    args = {}
    monkeypatch.setattr(run.subprocess, "run", lambda a, **kw: args.setdefault("a", a))
    run.run_pipeline(tmp_path / "found.json")
    assert "--ignore-applicants" in args["a"]
