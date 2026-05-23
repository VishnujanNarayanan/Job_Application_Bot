"""Orchestrator entry point — invoked by the Layer 1 scheduler.

Iteration 0: docstring-only contract. No implementation.
Layer logic lands in Iterations 1+.

Usage (once implemented):
    python -m src.main             # live run
    python -m src.main --dry-run   # scrape/score/build but never submit

Pipeline sequence (architecture doc Section 4):

LAYER 1 — Scheduler
    Determine cycle: "peak" if 8am <= now_IST < 11am, else "regular".
    N = 3 (peak) or 1 (regular) — applications to send this run.
    Acquire single-run lock to prevent overlapping cron executions.

LAYER 2 — Scraper (Indeed; Glassdoor in Iteration 4)
    Load current_index from search_rotation_state table.
    For each search term in rotation order:
        JobSpy fetch: country=india, hours_old per cycle (2 peak / 5 off-peak).
        Insert every raw result into all_jobs (permanent archive).
        Apply hard filters: years_required > 5 → HARD_FILTER_LAYER_2,
            location in disallowed_regions → HARD_FILTER_LAYER_2,
            company in cooldown (10 days) → COMPANY_COOLDOWN,
            duplicate job_id → DUPLICATE.
        Count passing results. If >= short_circuit_count (20): advance
        rotation index and stop this run. Else continue to next term.
    Write new current_index to search_rotation_state.

LAYER 3 — JD Parser (Gemini Call 1a — runs for EVERY passing job)
    Instructor-enforced JDParsed Pydantic model (see src/llm/schemas.py).
    spaCy validates extracted skills appear in JD text (catches hallucinations).
    Re-check years_experience > 5 on structured output → HARD_FILTER_LAYER_3.
    Role acceptance: find cluster matching the search term that found this job;
        accept if JD title matches cluster.accept_titles OR Gemini role_category
        is in cluster.accept_categories. Else → HARD_FILTER_LAYER_3 (ROLE_MISMATCH).
    Store structured fields in all_jobs row.

LAYER 4 — Scoring & Selection (pure functions, DB reads only)
    Embed JD: jd_vec_skills (required + nice_to_have), jd_vec_resp
        (responsibilities + role_summary), jd_vec_role (role_summary).
    Score every active experience: best_alias_score * 0.30 + top3_bullet_avg * 0.70.
    Select experiences: threshold 0.45, max 3, min 2, force-include 2.
    Ordering: if (best_score - second_score) > 0.20 → best-match at position 1;
        else position 1 by recency. Positions 2+ always by recency.
    Score every active project: name_score * 0.20 + topN_bullet_avg * 0.80
        (N = bullets actually displayed: 2 or 3). threshold 0.50, max 3, min 2,
        force-include 2. Section NEVER hidden. Order: score-descending.
    Select bullets: exactly 3 per experience; 2-3 per project (threshold 0.40,
        force-include 2 if fewer pass).
    Select summary: best cosine match among summaries tagged for the JD's
        role_category (falls back to all summaries if none match).
    Skill candidates: top-14 from skills_pool by cosine score.
    Gap skills: JD required/nice_to_have not in skills_pool (for Familiar With).
    Section order: Skills vs Projects ranked by aggregate JD match score.
    final_score = fit*0.55 + success_prob*0.30 + recency*0.10 + project*0.05
        fit = best_exp*0.50 + summary*0.20 + avg_skill_pool*0.30
        success_prob = seniority_score*0.60 + recency_score*0.40
        recency bands: <1h→1.0, <3h→0.8, <6h→0.6, <12h→0.4, >12h→0.2
        seniority: junior→1.0, mid→0.80, senior→0.40, lead→0.15
    final_score < 0.50 → not_applied (LOW_SCORE with score breakdown).
    Cycle-aware top-N picking:
        Combine new eligible jobs + active application_queue entries.
        Sort by final_score descending. Pick top N.
        Unpicked eligible → INSERT into application_queue (expires_at = now+12h).

LAYER 5 — Resume Builder (Gemini Call 1b — ONLY for jobs picked this run)
    LLM receives top-14 skill candidates + gap skills + JD context.
    Returns ResumeBuildLLMOutput: title_choices (Pydantic Literal enforces
        safe_title_aliases), skills_selection (3 categories × 3-5 skills from
        candidates; 0-4 Familiar With from gaps), cover_letter_text (≤900 chars).
    Post-validation: every category skill in top-14 set, every Familiar With
        skill in gap set and NOT in skills_pool, no duplicates, no banned
        category names. Regenerate up to 2x; else BUILD_FAILURE.
    Deterministic ordering: skills within each category by score desc;
        all 4 categories (including Familiar With) by aggregate score desc.
    DOCX assembly (structural detection — Option A):
        Walk template Heading1 paragraphs to identify sections.
        Header (before first WORK EXPERIENCE Heading1): NEVER MODIFIED.
        Clone first sub-block of each section as template; delete existing;
        insert new blocks with swapped text, preserving XML (tab stops,
        spacing, numPr list references, font runs).
        Project/cert hyperlinks: update r:id target in
        word/_rels/document.xml.rels to project.link / cert.verify_link;
        visible "Code →" / "Verify Here" text unchanged.
    Diff check: reject if any paragraph outside permitted regions changed.
    Save DOCX → LibreOffice headless → PDF →
        resumes/applied/{job_id}_{timestamp}.pdf (permanent, never deleted).
    Record in applied table: resume_path, selection_json (full snapshot), scores.

LAYER 6 — Application Sender (Gemini Call 2 — only if unknown form questions)
    Playwright loads data/sessions/indeed.json cookies; detects expired session
        → immediate Telegram alert, skip portal this run.
    Indeed Easy Apply multi-page form:
        Per page: discover fields, classify (profile/salary/upload/question/yesno).
        Fill profile fields from master_profile.personal.
        Salary: expected = jd.salary_max_lpa or 6.0 LPA; current numeric = 100000.
        Upload: resume as "Vishnujan_Narayanan_Resume.pdf".
        Cover letter: textarea → fill text; file upload → render PDF.
        Track page signatures (detect loops); abort if > max_form_pages (10).
    Question handling (4 categories):
        Cat 1 (profile lookup): auto-fill from master_profile.personal.
        Cat 2 (resume-derived): auto-fill from master profile data.
        Cat 3 (judgement): answer_bank stage-1 pattern match → stage-2 JD
            context match (threshold 0.80). Strong → auto-fill. Weak → Gemini
            adapt + hold for review. No match → Gemini draft + hold for review.
            Safe mode: abandon atomically on any held question; Telegram alert.
        Cat 4 (legally sensitive): MANUAL_REQUIRED immediately.
    Gemini Call 2: batched unknown questions → validated answers (banned-word
        check + fabrication check against master_profile text). Regenerate ≤2x.
    Submit. Retry at most ONCE on failure. Never twice.
    On any unrecoverable obstacle: PDF already saved; log MANUAL_REQUIRED
        with specific detail; surface in next Telegram digest.

LAYER 7 — State Management (always running, not a discrete step)
    PostgreSQL on Neon free tier (psycopg3 + SQLAlchemy 2.0 async).
    all_jobs: permanent archive of every scraped job (never deleted).
    applied / not_applied: structured outcomes with 13 reason categories.
    application_queue: 12-hour decay; expired rows → not_applied (STALE).
    master_bullets / master_summaries / master_title_aliases: NEVER hard-deleted.
        Removed from YAML → is_active=false, deactivated_at=now.
    Neon outage: buffer writes to data/pending_writes.jsonl; drain next run.

LAYER 8 — Notifications (Telegram)
    Morning digest (8am IST): applied last 24h with resume_path links,
        skipped counts by reason category, manual-required jobs with apply URL
        and PDF path, portal health status, Sheets link.
    Immediate alerts: Playwright crash, session expired, Gemini failure,
        DB connection failure, master_profile.yaml validation failure,
        3 consecutive zero-result runs from a portal.
    Inline review requests for Category 3 question approvals (approve/edit/skip).

LAYER 9 — Analytics (Google Sheets + Docs)
    Sheets (live, from Postgres): Applied, Skipped, ManualRequired tabs.
        Applied tab includes clickable resume_path link to exact submitted PDF.
    Sunday 8am IST: Gemini-synthesised weekly report written to Google Doc.
        Covers skill demand (30%+ threshold alert), recurring gaps,
        companies hiring actively, remote/onsite ratio, salary ranges.
"""

def main() -> None:
    """Entry point. Iteration 0 — not implemented."""
    raise NotImplementedError(
        "Iteration 0 is scaffold-only. Implementation begins in Iteration 1."
    )


if __name__ == "__main__":
    main()
