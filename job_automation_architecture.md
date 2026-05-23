# Job Application Automation System — Architecture

**Owner:** Vishnujan Narayanan
**Cost:** $0/month forever
**Status:** Locked, ready to build

---

## 1. Goal

Fully autonomous job application bot that:

- Detects new job postings within ~10 minutes of being posted
- Scores them via a multi-factor relevance model
- Builds a custom-tailored resume per JD from a master profile pool
- Submits applications automatically where possible
- Logs every scraped job and every applied resume permanently
- Generates weekly career intelligence reports
- Runs while the user sleeps
- Costs $0/month

The user sleeps. The system applies. The user wakes up to a digest.

---

## 2. Personal Configuration

```
Name                     Vishnujan Narayanan
Years of experience      1.5 years
Job type                 Fulltime only
Years required ceiling   5 years (jobs requiring more get rejected)
Location policy          Allow everything except disallowed regions
Disallowed regions       Delhi NCR (Delhi, Gurgaon, Gurugram, Noida,
                         Ghaziabad, Faridabad)
Visa policy              Open to international relocation — no filter
Resume profile source    master_profile.yaml (gitignored)

Salary
  Expected default       6 LPA (numeric: 600000)
  Expected per JD        JD's upper bound if specified, else 6 LPA
  Current text fields    Strategic redirect bank answer (no number)
  Current numeric        100000 (honest)

Upload filenames
  Resume                 Vishnujan_Narayanan_Resume.pdf (always)
  Cover letter           Vishnujan_Narayanan_Cover_Letter.pdf (always)

Question handling        Safe mode (hold for review on unknown)
Company blocklist        None
Familiar With            Max 4 adjacent gap skills, learning alert at 30%
                         frequency in weekly JDs
```

### 2.1 Target role categories

```
backend, data, ml, fullstack, devops, finance_market, quant
```

### 2.2 Search keywords (rotation order)

```yaml
search_terms:
  - "backend engineer"
  - "backend developer"
  - "software engineer python"
  - "platform engineer"
  - "API developer"
  - "data engineer"
  - "analytics engineer"
  - "data pipeline engineer"
  - "data scientist"
  - "data analyst"
  - "machine learning engineer"
  - "ML engineer"
  - "AI engineer"
  - "fullstack engineer"
  - "full stack developer"
  - "software engineer"
  - "devops engineer"
  - "SRE"
  - "quant developer"
  - "trading systems developer"
  - "financial software engineer"
  - "market data engineer"
  - "quantitative researcher"
  - "quantitative analyst"
```

### 2.3 Role acceptance clusters

Maps each search term to the broader set of acceptable role titles and Gemini categories. A search for "backend engineer" accepts jobs titled "software developer" if Gemini classifies them as backend or fullstack.

```yaml
role_clusters:
  backend:
    keywords: ["backend engineer", "backend developer", "software engineer python", "platform engineer", "API developer"]
    accept_titles: ["backend", "software engineer", "software developer", "platform", "API", "python developer"]
    accept_categories: ["backend", "fullstack"]

  data:
    keywords: ["data engineer", "analytics engineer", "data pipeline engineer", "data scientist", "data analyst"]
    accept_titles: ["data engineer", "data scientist", "data analyst", "analytics", "etl"]
    accept_categories: ["data", "ml"]

  ml:
    keywords: ["machine learning engineer", "ML engineer", "AI engineer"]
    accept_titles: ["machine learning", "ml engineer", "ai engineer", "deep learning"]
    accept_categories: ["ml", "data"]

  fullstack:
    keywords: ["fullstack engineer", "full stack developer", "software engineer"]
    accept_titles: ["fullstack", "full stack", "software engineer", "software developer", "web developer"]
    accept_categories: ["fullstack", "backend"]

  devops:
    keywords: ["devops engineer", "SRE"]
    accept_titles: ["devops", "sre", "site reliability", "platform", "infrastructure"]
    accept_categories: ["devops", "backend"]

  finance_market:
    keywords: ["quant developer", "trading systems developer", "financial software engineer", "market data engineer"]
    accept_titles: ["quant", "trading", "financial", "market data", "fintech"]
    accept_categories: ["finance_market", "quant", "backend"]

  quant:
    keywords: ["quantitative researcher", "quantitative analyst"]
    accept_titles: ["quant", "quantitative", "researcher", "analyst"]
    accept_categories: ["quant", "finance_market", "ml", "data"]
```

---

## 3. Master Profile Model

### 3.1 File: `master_profile.yaml` (gitignored)

```yaml
personal:
  name: "Vishnujan Narayanan"
  email: "narayanan.vishnujan@gmail.com"
  phone: "+91-8129-688-626"
  location: "Kochi, Kerala, India"
  github: "https://github.com/VishnujanNarayanan"
  linkedin: "https://linkedin.com/in/vishnujan-narayanan"
  certificates_link: "https://drive.google.com/..."

summaries:
  - id: "summary_001"
    text: "Backend engineer with 1.5 years building production data pipelines..."
    tags: ["backend", "data", "python"]
    role_categories: ["backend", "data", "finance_market"]
  # Any number of pre-written summaries, tagged

work_experience:
  - id: "citesert"
    company: "CiteSert"
    actual_title: "Quant Researcher"
    safe_title_aliases:
      - "Quant Researcher"
      - "Backend Engineer"
      - "Software Engineer"
      - "Data Engineer"
      - "Quantitative Analyst"
    start_date: "2025-06"
    end_date: "present"
    location: "Mumbai, India"
    bullet_pool:
      - id: "cs_b1"
        text: "Engineered a real-time stock intelligence pipeline..."
        tags: ["data engineering", "python", "scraping"]
      # As many defensible bullets as exist

projects:
  - id: "minute_stock"
    name: "Minute-Level Stock Prediction & Backtesting"
    link: "https://github.com/Vishnujan/minute-stock-prediction"
    tags: ["ml", "time series", "backtesting"]
    bullet_pool:
      - id: "ms_b1"
        text: "..."
        tags: [...]
      # Multiple bullets per project

skills_pool:
  # Flat list of all defensible skills.
  # LLM categorizes per JD; reference categories below as guidance only.
  # Reference categories: Programming, Backend & APIs, Frontend, Databases,
  # Cloud & DevOps, Data & ML, Quantitative, Build & Testing.
  - "Python"
  - "C++"
  - "JavaScript"
  - "SQL"
  - "FastAPI"
  - "Flask"
  - "PostgreSQL"
  - "MongoDB"
  - "Docker"
  - "AWS"
  - "Git"
  - "GitHub Actions"
  - "Linux"
  - "Pandas"
  - "NumPy"
  - "scikit-learn"
  - "PyTorch"
  - "Time Series"
  - "Feature Engineering"
  - "Hypothesis Testing"
  - "Regression"
  - "Backtesting"
  - "PyTest"
  - "React"
  - "REST APIs"
  - "Selenium"
  - "Playwright"
  # ... user maintains the full list

education:
  - degree: "B.Tech in Computer Science (Big Data Specialization)"
    institution: "UPES, Dehradun"
    dates: "Sep 2021 - May 2025"
    score: "CGPA: 7.3"
  - degree: "Higher Secondary (Class XII)"
    institution: "Chinmaya Vidyalaya, Kerala"
    dates: "Jun 2019 - Mar 2020"
    score: "Score: 89%"
  - degree: "Secondary School (Class X)"
    institution: "Chinmaya Vidyalaya, Kerala"
    dates: "Jun 2017 - Mar 2018"
    score: "Score: 92%"

certifications:
  - id: "nptel"
    name: "NPTEL Certification (IIT Madras)"
    verify_link: "https://nptel.ac.in/..."
    bullets:
      - "Top 5% (Elite + Silver)"
      - "Programming, Data Structures and Algorithms Using Python"
  # IBM, Google Advanced, Google Data Analytics entries
```

### 3.2 Master profile lifecycle

Bot run detects `master_profile.yaml` mtime change. On change:

1. Validate YAML schema. On failure: Telegram alert, abort run.
2. Generate canonical `master_profile.json`.
3. Diff against DB:
   - New bullets → embed via sentence-transformers, insert into `master_bullets` (`is_active=true`)
   - Changed bullets → re-embed, update
   - Removed bullets → mark `is_active=false`, set `deactivated_at=now`
   - New/changed summaries and title aliases → same pattern
4. Update `master_meta.master_profile_processed_at`.

Bullets, summaries, and title aliases are NEVER hard-deleted from DB. Deactivation only. This preserves audit trail integrity — every applied resume's `selection_json` references must resolve forever.

### 3.3 Pre-computed embeddings

Once at master profile rebuild — every bullet, summary, title alias, and skill gets a `vector(384)` via sentence-transformers, stored in DB. No runtime embedding of profile content; only the JD is embedded per run.

---

## 4. Architecture — 9 Layers

```
LAYER 1   Scheduler
LAYER 2   Scraper             (Indeed, Glassdoor)
LAYER 3   JD Parser           (Gemini Call 1a)
LAYER 4   Scoring Engine      (selection from master profile)
LAYER 5   Resume Builder      (Gemini Call 1b)
LAYER 6   Application Sender  (Gemini Call 2 if needed)
LAYER 7   State Management    (PostgreSQL on Neon)
LAYER 8   Notifications       (Telegram)
LAYER 9   Analytics           (Sheets + Docs)
```

---

### Layer 1 — Scheduler

Oracle Cloud cron (Iteration 5 onward), local cron (Iterations 1-4).

```
Peak       Every 40 min, 8am-6pm IST, weekdays
Off-peak   Every 4 hours, nights and weekends
Sunday     8am IST → Layer 9 weekly report + storage cleanup
```

Application quotas per run (cycle-aware):

```
8am-11am IST (peak freshness window): apply to top 3 from this run's batch
After 11am: apply to top 1 from this run's batch
```

---

### Layer 2 — Scraper

**Sources:** Indeed, Glassdoor (Iterations 1-4). Naukri added in Iteration 6. LinkedIn not used (account-ban risk).

**Serial search rotation with short-circuit:**

```
Each run picks up where last run left off (search_rotation_state.current_index).
For each keyword in rotation:
  Query JobSpy with India + hours_old (2 peak / 5 off-peak)
  Insert every result into all_jobs (permanent archive)
  Apply hard filters to each result
  Count passing results
  If passing count >= 20 → advance rotation index, stop searching this run
  Else → continue to next keyword
Update search_rotation_state.current_index for next run.
```

**Hard filters (applied at scrape time):**

```
years_required > 5            → reject (HARD_FILTER_LAYER_2)
location in disallowed list   → reject (HARD_FILTER_LAYER_2)
company in cooldown (10d)     → reject (COMPANY_COOLDOWN)
duplicate job_id              → reject (DUPLICATE)
```

Passed jobs go to `processing_queue` with status `queued`.

---

### Layer 3 — JD Parser (Gemini Call 1a)

**Tools:** Gemini 2.0 Flash + Instructor, spaCy validator.

```python
class JDParsed(BaseModel):
    required_skills: list[str]
    nice_to_have: list[str]
    responsibilities: list[str]
    role_summary: str
    team_or_product: str | None

    years_experience: int | None
    role_level: Literal["junior", "mid", "senior", "lead"] | None
    role_category: Literal[
        "backend", "data", "ml", "fullstack",
        "devops", "finance_market", "quant", "other"
    ]
    job_type: Literal["fulltime", "internship", "contract"]
    location_type: Literal["remote", "hybrid", "onsite"]

    salary_min_lpa: float | None
    salary_max_lpa: float | None
    salary_currency: Literal["INR_LPA", "INR_monthly", "USD", "EUR", "OTHER"] | None
```

spaCy validates extracted skills exist in JD text (catches Gemini hallucinations).

**Hard filter re-check on structured data:**

```
years_experience > 5         → reject (HARD_FILTER_LAYER_3)
```

**Role acceptance check (smart matching):**

```
Find the role cluster matching the search term that found this job.
Pass if:
  - JD role title contains any pattern in cluster.accept_titles, OR
  - Gemini's role_category is in cluster.accept_categories
Reject otherwise (HARD_FILTER_LAYER_3, detail=ROLE_MISMATCH).
```

Output stored in `all_jobs` as structured fields.

---

### Layer 4 — Scoring Engine

#### 4.1 JD embeddings

```
jd_vec_skills  = embed(required_skills + nice_to_have)
jd_vec_resp    = embed(responsibilities + role_summary)
jd_vec_role    = embed(role_summary)
jd_vec_match   = jd_vec_skills + jd_vec_resp   (for bullet scoring)
```

#### 4.2 Experience scoring & selection

```python
def score_experience(exp, jd):
    best_alias_score = max(
        cosine(embed(alias), jd_vec_role)
        for alias in exp.safe_title_aliases
    )
    bullet_scores = sorted(
        [cosine(b.embedding, jd_vec_match) for b in exp.active_bullets],
        reverse=True
    )
    top3_avg = mean(bullet_scores[:3])
    return (best_alias_score * 0.30) + (top3_avg * 0.70)
```

**Selection rules:**

```
threshold:     0.45
max_shown:     3
min_shown:     2
force-include: if fewer than 2 experiences pass, take strongest 2

Ordering:
  Position 1: highest-scoring experience IF
              (max_score - second_max_score) > 0.20
              ELSE most recent
  Position 2+: by recency, most recent first
```

#### 4.3 Experience bullet selection

```
Per selected experience: exactly 3 bullets, top by score.
Order: score-descending.
```

#### 4.4 Project scoring & selection

```python
def score_project(proj, jd):
    name_score = cosine(embed(proj.name), jd_vec_role)
    bullet_scores = sorted(
        [cosine(b.embedding, jd_vec_match) for b in proj.active_bullets],
        reverse=True
    )
    passing = [s for s in bullet_scores if s >= 0.40]
    n = min(3, max(2, len(passing)))   # 2 or 3, whichever actually displays
    topN_avg = mean(bullet_scores[:n])
    return (name_score * 0.20) + (topN_avg * 0.80)
```

Score reflects displayed bullet count. A project showing 2 bullets is scored on its 2 best.

**Selection rules:**

```
threshold:        0.50
max_shown:        3
min_shown:        2
force-include:    if fewer than 2 pass, take best 2
hide_section:     NEVER — projects section always shows
Ordering:         score-descending
```

#### 4.5 Project bullet selection

```
Per selected project:
  bullets passing 0.40 threshold:
    3+ pass  → show top 3
    2 pass   → show those 2
    <2 pass  → force-include best 2
  Order: score-descending
```

#### 4.6 Summary selection (deterministic, no LLM)

```python
def select_summary(master, jd):
    matching = [s for s in master.summaries
                if jd.role_category in s.role_categories]
    if not matching:
        matching = master.summaries
    return max(matching, key=lambda s: cosine(s.embedding, jd_vec_match))
```

#### 4.7 Skills selection (hybrid: deterministic + LLM in Call 1b)

The skills section uses a hybrid pipeline: deterministic scoring picks candidate skills from the pool, the LLM names categories and groups skills into them, then deterministic ordering finalizes the layout. Distribution is flexible — the system optimizes for clean semantic grouping, not for hitting a fixed skill count.

**Step 1 — Score every skill in skills_pool (deterministic):**

```python
def score_skills_pool(skills_pool, jd):
    jd_vec_skills_section = jd_vec_skills + jd_vec_resp
    return [
        (skill, cosine(embed(skill), jd_vec_skills_section))
        for skill in skills_pool
    ]
```

**Step 2 — Take top-14 candidate skills by score (deterministic):**

```python
scored = score_skills_pool(skills_pool, jd)
scored.sort(key=lambda x: -x[1])
top_candidates = [s for s, _ in scored[:14]]
top_scores = {s: score for s, score in scored[:14]}
```

The LLM gets up to 14 candidates and decides how many actually fit. Not every candidate has to appear on the resume.

**Step 3 — Score gap skills for Familiar With (deterministic):**

```python
def identify_gaps(jd, skills_pool):
    pool_set = set(skills_pool)
    return [s for s in jd.required_skills + jd.nice_to_have
            if s not in pool_set]

gap_skills = identify_gaps(jd, skills_pool)
gap_scores = {s: cosine(embed(s), jd_vec_skills_section)
              for s in gap_skills}
```

**Step 4 — LLM names 3 categories and assigns skills (LLM in Call 1b):**

```python
class SkillCategory(BaseModel):
    name: str = Field(max_length=30)
    skills: list[str] = Field(min_length=3, max_length=5)

class SkillsSelection(BaseModel):
    familiar_with: list[str] = Field(max_length=4)
    categories: list[SkillCategory] = Field(min_length=3, max_length=3)
```

The LLM receives:
- The top-14 pool candidates (with their scores)
- The full list of identified gap skills (with their scores)
- The JD context

The LLM returns:
- Exactly 3 categories, each with 3-5 skills drawn from the top-14 candidates
- Up to 4 Familiar With gap skills (LLM picks the most credibly adjacent)
- Total pool skills shown: 10-14 (LLM optimizes for clean grouping)
- Total lines (with Familiar With): 10-18

The LLM is instructed to:
- Prioritize clean semantic grouping over hitting any fixed count
- Drop candidates that don't fit cleanly into any of the 3 categories
- Pick gap skills that are CREDIBLY adjacent to user's actual skills (not aspirational)
- Never invent skills not present in the candidates or gap list

**Step 5 — Order skills within each category (deterministic):**

```python
for category in skills_selection.categories:
    category.skills.sort(key=lambda s: -top_scores[s])
```

Within a category, the highest-scoring skill appears first.

**Step 6 — Compute aggregate score per category, INCLUDING Familiar With (deterministic):**

```python
def category_aggregate_score(skills_list, score_lookup):
    return mean(score_lookup[s] for s in skills_list)

categories_with_scores = [
    (c, category_aggregate_score(c.skills, top_scores))
    for c in skills_selection.categories
]

familiar_with_score = (
    category_aggregate_score(skills_selection.familiar_with, gap_scores)
    if skills_selection.familiar_with else None
)
```

**Step 7 — Order ALL categories (including Familiar With) by aggregate match (deterministic):**

```python
all_renderable = categories_with_scores.copy()
if familiar_with_score is not None:
    all_renderable.append(("Familiar With", skills_selection.familiar_with,
                           familiar_with_score))

all_renderable.sort(key=lambda x: -x[-1])  # descending by aggregate score
```

Familiar With is NOT pinned to first position. It competes against the 3 LLM-named categories on aggregate JD match score. The category with the highest aggregate appears first, regardless of whether it's Familiar With or LLM-named.

**Behavioral consequences:**

- If the JD requires several skills the user doesn't have AND those gap skills score high, Familiar With ranks high (potentially first)
- If gap skills are weak matches or there are none, Familiar With falls to the bottom or doesn't appear
- A high-match LLM-named category always beats a low-match Familiar With

**Final layout per JD (example outputs):**

```
Example A — User has strong pool overlap with JD, few gaps:
  [Backend & APIs]:    [4 skills]      (highest aggregate)
  [Cloud & DevOps]:    [4 skills]
  [Databases]:         [4 skills]      (lowest aggregate)
  Familiar With:       [2 skills]      (low gap scores, ranks last)

Example B — User has good pool match but JD heavily demands missing skills:
  Familiar With:       [4 skills]      (high gap scores, ranks first)
  [Backend Systems]:   [4 skills]
  [Data Engineering]:  [4 skills]
  [Tools]:             [3 skills]

Example C — No gap skills, only 11 pool candidates fit cleanly:
  [Backend & APIs]:    [4 skills]
  [Quantitative]:      [4 skills]
  [DevOps]:            [3 skills]
  (no Familiar With row)
```

**Post-validation enforced:**

```
1. Every skill in categories[].skills exists in master_profile.skills_pool
2. Every skill in categories[].skills is in the pre-computed top-14 set
3. Skills in familiar_with do NOT exist in skills_pool (gaps only)
4. Skills in familiar_with were present in JD required_skills or nice_to_have
5. No skill appears twice across categories or in Familiar With
6. Each LLM-named category has 3-5 skills (Pydantic enforces)
7. Total LLM-named categories = exactly 3
8. familiar_with has 0-4 skills (Pydantic enforces)
9. Category names not in a banned list ("Miscellaneous", "Other", 
   "Various", "Additional Skills", "Soft Skills", etc.)
10. Category names are JD-relevant nouns or noun phrases
    (e.g. "Backend & APIs", "Quantitative Methods", "Cloud Infrastructure"),
    not buzzwords or vague labels
```

If validation fails, regenerate once. If still fails, raise `BUILD_FAILURE`.

The skill pool, top-14 scores, gap scores, LLM's grouping, and final ordering are all stored in the `applied.selection_json` snapshot for audit.

#### 4.8 Section ordering

```
FIXED:
  Position 1         Summary
  Position 2         Work Experience
  Second-last        Education
  Last               Certifications

VARIABLE (placed between Work Experience and Education):
  Skills and Projects — ordered descending by aggregate JD match score
  Whichever matches the JD better appears first
```

#### 4.9 Final score

```python
fit_score = (
    best_experience_score      * 0.50 +
    selected_summary_score     * 0.20 +
    avg_skill_pool_match_score * 0.30
)

success_prob = (
    applicant_score * 0.40 +
    seniority_score * 0.35 +
    age_score       * 0.25
)
# Seniority for 1.5 yrs: junior→1.0, mid→0.80, senior→0.40, lead→0.15

recency_score = f(time_since_posted)
# <1h→1.0, 1-3h→0.8, 3-6h→0.6, 6-12h→0.4, >12h→0.2

best_project_score = highest project score (small bonus)

final_score = (
    fit_score          * 0.55 +
    success_prob       * 0.30 +
    recency_score      * 0.10 +
    best_project_score * 0.05
)
```

#### 4.10 Apply decision (cycle-aware)

```
final_score >= 0.50 → eligible for application (or queue)
final_score <  0.50 → not_applied (LOW_SCORE, with breakdown)
```

**Cycle-based top-N picking:**

```
At end of Layer 4 for the run's batch:
  cycle = "peak" if current_time in [8am-11am IST] else "regular"
  N = 3 if cycle == "peak" else 1

  Combine this run's eligible jobs with active_queue (12-hour decay queue)
  Sort by final_score descending
  Pick top N for Layer 5

  Unpicked eligible jobs:
    INSERT into application_queue with status=queued, queued_at=now
    Will be reconsidered next run, expires after 12 hours
    On expiry: not_applied (reason=STALE)
```

#### 4.11 Layer 4 output to Layer 5

```python
class SelectionResult:
    selected_experiences: list[(ExpId, [BulletId], position)]
    selected_projects: list[(ProjId, [BulletId])]
    selected_summary_id: str
    section_order: list[str]
    top_skill_candidates: list[str]             # up to 14 highest-scoring pool skills
    top_skill_scores: dict[str, float]          # candidate scores
    gap_skills: list[str]                       # all identified gaps
    gap_skill_scores: dict[str, float]          # gap candidate scores
    title_alias_candidates: dict[ExpId, list[str]]
    expected_salary_lpa: float
    final_score: float
```

`expected_salary_lpa = jd.salary_max_lpa if jd.salary_max_lpa else 6.0`

---

### Layer 5 — Resume Builder (Gemini Call 1b)

#### 5.1 Gemini Call 1b — single combined call

The LLM receives `selection_result.top_skill_candidates` (up to 14) and `selection_result.gap_skills` with their scores — NOT the full skills_pool. The LLM's job is to name 3 categories and assign 3-5 skills to each from the pool candidates, plus pick up to 4 gap skills for Familiar With. The LLM cannot introduce skills outside these provided sets. Total skill count shown is flexible (10-14 pool + 0-4 gaps) — the LLM optimizes for clean grouping over hitting a number.

```python
class ResumeBuildLLMOutput(BaseModel):
    title_choices: dict[str, str]
    # Each value validated by Literal[tuple(exp.safe_title_aliases)]

    skills_selection: SkillsSelection
    # Pydantic-enforced: exactly 3 categories, 3-5 skills each, familiar_with 0-4.
    # Post-validation enforces: all category skills must come from
    # top_skill_candidates set, familiar_with skills must come from gap_skills
    # set (i.e. NOT in skills_pool), no duplicates.
    # Flexible distribution: 10-14 pool skills total + 0-4 gaps.
    # The LLM optimizes for clean semantic grouping, NOT for hitting any
    # specific count. Candidates not assigned are dropped.

    cover_letter_text: str = Field(max_length=900)
```

`Literal[tuple(safe_title_aliases)]` makes returning an unauthorized title physically impossible.

#### 5.2 DOCX assembler — Option A (structural detection)

Approach: clone-and-fill the user's template DOCX. The assembler never rebuilds paragraphs from scratch — it clones existing styled paragraphs and swaps text content.

**Region detection:**

```
Walk the template by Heading1 paragraphs to identify sections:
  WORK EXPERIENCE, SKILLS, PROJECTS, EDUCATION, CERTIFICATES

Header (paragraphs before WORK EXPERIENCE): NEVER MODIFIED
  Preserves: name, contact line, embedded hyperlinks (GitHub,
  LinkedIn, Certificates, location)

Within each variable section, detect repeating sub-blocks by sub-heading style:
  An experience sub-block = sub-heading paragraph + following paragraphs
  until next sub-heading or section end.
```

**Build flow:**

```
1. Open template via python-docx
2. Header stays untouched (paragraphs before first Heading1)
3. Replace summary text (single paragraph after header)
4. WORK EXPERIENCE:
     Extract first experience block as template
     Delete all existing experience blocks
     For each selected experience (in order):
       Clone the template block
       Replace title text with chosen alias
       Replace company/location/dates text
       Replace bullet texts (preserving native list numId references)
       Insert into section
5. SKILLS:
     Clear existing skill rows
     If skills_selection.familiar_with is non-empty:
       Clone skill row template, set "Familiar With: gap1, gap2, ..."
     For each category in skills_selection.categories
       (already ordered by aggregate match, descending):
       Clone a skill row template
       Set "Category Name: skill1, skill2, skill3, skill4"
       (skills already sorted within category, descending by score)
6. PROJECTS:
     Same as WORK EXPERIENCE
     Update embedded hyperlink target on "Code →" link to project.link
     (modifies word/_rels/document.xml.rels for the run's r:id)
7. EDUCATION: stays untouched
8. CERTIFICATES:
     Clone cert template blocks per master_profile.certifications
     Update "Verify Here" hyperlink target per cert.verify_link
9. Reorder Skills/Projects sections per section_order
10. Diff validate (allowed regions only modified; else BUILD_FAILURE)
11. Save: resumes/applied/{job_id}_{timestamp}.docx
12. Convert: resumes/applied/{job_id}_{timestamp}.pdf via LibreOffice
13. Record in applied table:
      resume_path, selection_json (full SelectionResult snapshot),
      cover_letter_text, scores
```

**Preservation guarantees:**

- All paragraph properties (tab stops, spacing, indentation, alignment) inherited via clone
- Font, size, color, bold, italic preserved per run
- Native bullet list references (`<w:numPr>` with `numId`) preserved
- Header hyperlinks untouched (header never modified)
- Project and certificate hyperlinks updated by modifying `r:id` targets in relationships file, visible link text preserved

#### 5.3 PDF retention

```
resumes/applied/   permanent — every submitted resume kept forever
                   ~300KB/PDF, ~120MB/year, ~1.2GB at year 10
                   NEVER auto-cleaned (audit trail requirement)
```

#### 5.4 No caching

Every JD gets a fresh build (~5s, ~2000 tokens). Caching not used — LLM call is small, selection is fast, and two JDs rarely match closely enough to safely reuse a resume.

---

### Layer 6 — Application Sender

#### 6.1 Decision tree

```
Tailored PDF ready
  ↓
Indeed Easy Apply              → proceed
Glassdoor external redirect    → MANUAL_REQUIRED
CAPTCHA / unknown form         → MANUAL_REQUIRED
```

#### 6.2 Session management

```
First run (manual): user logs in to Indeed, cookies saved to
  data/sessions/indeed.json
Subsequent: load cookies, skip login
Expired: immediate Telegram alert, skip portal this run
```

No plaintext credentials stored.

#### 6.3 Multi-page Easy Apply flow

```
1. Navigate to job URL
2. Detect apply button (or MANUAL_REQUIRED)
3. Per page:
     Discover all fields
     Classify each (profile / salary / upload / question / yesno)
     Collect "question" fields into unknown_questions_batch
     Fill non-question fields immediately
     Click Next
4. If unknown_questions_batch non-empty:
     Match against answer_bank (pattern + JD context)
     Unmatched → Gemini Call 2 (batched answers)
     Safe mode: hold for review, abandon application atomically
     Bold mode: fill and continue
5. Final page:
     Verify PDF attached as "Vishnujan_Narayanan_Resume.pdf"
     Submit
6. Log to applied table
```

Page signature tracking detects loops. Max 10 pages safety limit.

#### 6.4 Field handling

```
Resume upload       → rename to "Vishnujan_Narayanan_Resume.pdf" at upload
Phone/email/name    → master_profile.personal
Work auth           → Yes
Years experience    → 1.5 (or "1-2 yrs" band)
Notice period       → Immediate

Expected salary
  text     → f"{expected_salary_lpa} LPA"
  numeric  → expected_salary_lpa * 100000
  dropdown → band containing expected value
  slider   → expected_salary_lpa * 100000

Current salary
  text     → strategic redirect (bank answer)
  numeric  → 100000
  dropdown → band containing 100000
  slider   → 100000
```

`expected_salary_lpa` comes from SelectionResult (JD upper bound or 6 LPA default).

#### 6.5 Cover letter handling

```
Detect cover letter field:
  Textarea    → fill cover_letter_text from Call 1b
  File upload → render text to PDF, upload as
                "Vishnujan_Narayanan_Cover_Letter.pdf"
  No field    → skip (cover letter still stored in applied table)
```

#### 6.6 Question handling — 4 categories

```
Category 1   Profile lookup        auto-fill from master_profile.personal
Category 2   Resume-derived facts  auto-fill from master profile data
Category 3   Judgement / strategic bank match → adapt → review
Category 4   Legally sensitive     MANUAL_REQUIRED
```

**Category 3 — 2-stage matching:**

```
Stage 1: question pattern match against answer_bank
  YES → Stage 2
  NO  → Gemini drafts fresh, hold for review

Stage 2: JD context match
  Strong (>0.80) → auto-fill from bank, no review
  Weak  (<0.80)  → Gemini adapts bank answer, hold for review
  None           → fresh draft, hold for review
```

#### 6.7 Human voice for generated content

```
BANNED words: leverage, synergy, robust, scalable, holistic
              passionate about, as a [role], "I bring a unique blend",
              "I am excited to", "I am writing to",
              "with X years of experience in"

BANNED structures: three-point symmetric lists, opening with credentials

REQUIRED: contractions (I'm, don't), varied sentence length,
          specific references from master_profile, direct tone
```

Few-shot from top 5 recent approved bank answers. Post-validation: banned-pattern regex + fabrication check (tech names and numbers must exist in master_profile). Regenerate up to 2x; if still failing, hold for review with violations highlighted.

#### 6.8 Manual-required flow

```
Tailored PDF already saved in resumes/applied/
INSERT into not_applied with reason=MANUAL_REQUIRED + specific detail
Telegram digest groups these with:
  Apply URL
  Resume PDF path
  Specific reason
```

#### 6.9 Failure handling

```
Apply button missing        MANUAL_REQUIRED
Session expired             immediate alert, skip portal
Upload failed               retry once → APPLY_FAILURE
Submission error            retry once → APPLY_FAILURE
Unknown field type          screenshot → MANUAL_REQUIRED
External redirect           MANUAL_REQUIRED
CAPTCHA                     MANUAL_REQUIRED
Category 4 question         MANUAL_REQUIRED
Network timeout             retry once after 30s
```

Hard rule: never retry submission more than once.

---

### Layer 7 — State Management (PostgreSQL on Neon)

```
Database     PostgreSQL 15+ (Neon free tier, 3GB, pgvector)
Driver       psycopg3
ORM          SQLAlchemy 2.0
Cold start   ~500ms first query per run (accepted)
Pool         pool_size=2, max_overflow=5, pool_pre_ping=true
```

#### 7.1 Schema

```sql
-- Permanent archive of every scraped job
CREATE TABLE all_jobs (
    job_id            TEXT PRIMARY KEY,
    company           TEXT NOT NULL,
    role              TEXT NOT NULL,
    site              TEXT NOT NULL,
    location          TEXT,
    job_url           TEXT,
    posted_at         TIMESTAMPTZ,
    scraped_at        TIMESTAMPTZ DEFAULT NOW(),
    jd_text           TEXT,
    required_skills   JSONB,
    nice_to_have      JSONB,
    responsibilities  JSONB,
    role_summary      TEXT,
    team_or_product   TEXT,
    years_required    INTEGER,
    role_level        TEXT,
    role_category     TEXT,
    job_type          TEXT,
    location_type     TEXT,
    salary_min_lpa    REAL,
    salary_max_lpa    REAL,
    salary_currency   TEXT,
    outcome           TEXT,         -- applied | not_applied | queued | pending
    outcome_at        TIMESTAMPTZ
);
CREATE INDEX idx_all_jobs_scraped ON all_jobs(scraped_at DESC);
CREATE INDEX idx_all_jobs_outcome ON all_jobs(outcome);
CREATE INDEX idx_all_jobs_skills  ON all_jobs USING GIN(required_skills);

-- Applied jobs with full audit trail
CREATE TABLE applied (
    job_id              TEXT PRIMARY KEY REFERENCES all_jobs(job_id),
    apply_type          TEXT,
    resume_path         TEXT,             -- permanent PDF reference
    cover_letter_text   TEXT,
    cover_letter_used   BOOLEAN DEFAULT FALSE,
    selection_json      JSONB,            -- full SelectionResult snapshot
    expected_salary_lpa REAL,
    fit_score           REAL,
    success_prob        REAL,
    recency_score       REAL,
    final_score         REAL,
    gap_skills          JSONB,
    application_status  TEXT,
    failure_reason      TEXT,
    applied_at          TIMESTAMPTZ DEFAULT NOW()
);

-- Not-applied jobs with structured reasons
CREATE TABLE not_applied (
    job_id            TEXT PRIMARY KEY REFERENCES all_jobs(job_id),
    reason_category   TEXT NOT NULL,
    reason_detail     TEXT,
    fit_score         REAL,
    success_prob      REAL,
    recency_score     REAL,
    final_score       REAL,
    gap_skills        JSONB,
    in_field          BOOLEAN,
    not_applied_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Application queue (12-hour decay)
CREATE TABLE application_queue (
    job_id        TEXT PRIMARY KEY REFERENCES all_jobs(job_id),
    final_score   REAL,
    status        TEXT,             -- queued | picked | expired
    queued_at     TIMESTAMPTZ DEFAULT NOW(),
    expires_at    TIMESTAMPTZ       -- queued_at + 12 hours
);
CREATE INDEX idx_queue_status ON application_queue(status, expires_at);

-- Processing queue (transient, per-run)
CREATE TABLE processing_queue (
    job_id     TEXT PRIMARY KEY REFERENCES all_jobs(job_id),
    status     TEXT,
    queued_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Master profile bullets (NEVER hard-deleted)
CREATE TABLE master_bullets (
    bullet_id        TEXT PRIMARY KEY,
    parent_id        TEXT NOT NULL,
    parent_type      TEXT NOT NULL,        -- "experience" | "project"
    text             TEXT NOT NULL,
    tags             JSONB,
    embedding        vector(384),
    is_active        BOOLEAN DEFAULT TRUE,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW(),
    deactivated_at   TIMESTAMPTZ NULL
);
CREATE INDEX idx_bullets_embedding ON master_bullets USING ivfflat (embedding vector_cosine_ops);
CREATE INDEX idx_bullets_parent    ON master_bullets(parent_id, parent_type);
CREATE INDEX idx_bullets_active    ON master_bullets(is_active);

-- Master summaries
CREATE TABLE master_summaries (
    summary_id       TEXT PRIMARY KEY,
    text             TEXT NOT NULL,
    tags             JSONB,
    role_categories  JSONB,
    embedding        vector(384),
    is_active        BOOLEAN DEFAULT TRUE,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_summaries_embedding ON master_summaries USING ivfflat (embedding vector_cosine_ops);

-- Master title aliases (per-experience allow-lists)
CREATE TABLE master_title_aliases (
    id            TEXT PRIMARY KEY,
    parent_id     TEXT NOT NULL,
    alias         TEXT NOT NULL,
    embedding     vector(384),
    is_active     BOOLEAN DEFAULT TRUE
);
CREATE INDEX idx_aliases_parent    ON master_title_aliases(parent_id);
CREATE INDEX idx_aliases_embedding ON master_title_aliases USING ivfflat (embedding vector_cosine_ops);

-- Master profile metadata
CREATE TABLE master_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
-- Keys: master_profile_yaml_mtime, master_profile_processed_at

-- Search rotation state
CREATE TABLE search_rotation_state (
    key             TEXT PRIMARY KEY,
    value           TEXT
);
-- Keys: current_index, last_run_at

-- Answer bank (grows over time)
CREATE TABLE answer_bank (
    id                TEXT PRIMARY KEY,
    question_patterns JSONB,
    jd_contexts       JSONB,
    answer            TEXT,
    approved_by_user  BOOLEAN DEFAULT FALSE,
    times_used        INTEGER DEFAULT 0,
    last_used         TIMESTAMPTZ,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

-- Pending review queue
CREATE TABLE pending_review (
    id                TEXT PRIMARY KEY,
    job_id            TEXT,
    company           TEXT,
    role              TEXT,
    question_text     TEXT,
    question_category INTEGER,
    bank_match_id     TEXT,
    bank_match_score  REAL,
    gemini_draft      TEXT,
    user_answer       TEXT,
    status            TEXT,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    reviewed_at       TIMESTAMPTZ
);

-- Portal health
CREATE TABLE portal_health (
    site               TEXT PRIMARY KEY,
    last_run           TIMESTAMPTZ,
    result_count       INTEGER,
    consecutive_zeros  INTEGER DEFAULT 0,
    last_error         TEXT
);

-- Company cooldown
CREATE TABLE company_cooldown (
    company         TEXT PRIMARY KEY,
    last_applied_at TIMESTAMPTZ
);
```

#### 7.2 not_applied reason categories

```
HARD_FILTER_LAYER_2     Rejected at scrape on raw text
HARD_FILTER_LAYER_3     Rejected after Gemini parse on structured data
ROLE_MISMATCH           JD role doesn't match search term cluster
LOCATION_DISALLOWED     JD location in disallowed regions
LOW_SCORE               Passed filters, final_score < 0.50
STALE                   Queued but expired 12-hour decay
PARSE_FAILURE           Couldn't parse JD
BUILD_FAILURE           Layer 5 failed (template, diff check, validation)
APPLY_FAILURE           Technical failure in submission
MANUAL_REQUIRED         Can't auto-apply (redirect, CAPTCHA, etc.)
REJECTED_BY_USER        Marked skip during review
COMPANY_COOLDOWN        Same company applied <10 days ago
DUPLICATE               Already in all_jobs
```

#### 7.3 Storage management

```
DATABASE (Neon, 3GB free)        ~80MB/year — never an issue

FILE STORAGE (Oracle VM, 200GB)
  resumes/applied/   permanent, ~120MB/year
  resumes/templates/ ~50KB
  data/sessions/     ~50KB
  data/manual_queue/ screenshots, ~200KB each
  data/logs/         ~100MB/year
  Year 10 total: ~2-3GB

AUTO-CLEANUP (Sunday weekly)
  application_queue: drop rows past expires_at, mark not_applied (STALE)
  data/logs:         drop files > 1 year old
  data/manual_queue: drop screenshots > 90 days old
  resumes/applied:   NEVER cleaned
```

#### 7.4 Failure handling for remote DB

```
Network blip          → SQLAlchemy pool_pre_ping auto-recovers
Neon down             → exponential backoff 3x → buffer to data/pending_writes.jsonl
                        Next successful run drains the buffer
Cold start            → ~500ms accepted
```

---

### Layer 8 — Notifications (Telegram)

**Morning digest (8am IST daily):**

```
Applied last 24h:
  Per job: company, role, final_score, resume_path link

Skipped (top categories only):
  LOW_SCORE count, ROLE_MISMATCH count, etc.

Manual-required:
  Per job: company, role, apply URL, resume PDF path, specific reason

Failures needing attention

Portal health status

Sheets link
```

**Review requests (immediate, Category 3 questions):**

Inline approve/edit/skip commands. User responses update answer_bank.

**Critical alerts (immediate):**

```
Playwright crash
Session expired
Gemini API failure
3 consecutive zero results from a portal
Persistent database connection failure
master_profile.yaml validation failure
```

---

### Layer 9 — Analytics

**Sheets (live view from PostgreSQL):**

```
Sheet 1   Applied jobs
          Columns: date, company, role, portal, final_score,
                   gap_skills, expected_salary_lpa, status,
                   resume_path (clickable link to exact PDF), URL

Sheet 2   Relevant skipped jobs (in_field=true)

Sheet 3   Manual-required jobs (with resume paths and apply links)
```

**Google Docs (monthly):**

```
Weekly sections with per-job summaries
Sunday Report (Gemini synthesis):
  Most demanded skills this week
  Recurring gaps in profile (30%+ alert)
  Companies hiring actively
  Remote vs onsite ratio
  Salary ranges
  Pattern of weak areas
```

---

## 5. Gemini Call Strategy — 3 calls max per job

```
CALL 1a — JD parse (ALWAYS, every job)               ~800 tokens
CALL 1b — title alias + skills selection + cover     ~1800 tokens
          letter (only if final_score >= 0.50 AND
          selected for application this run)
CALL 2  — batched form questions                     ~2000 tokens
          (only if form has unknown questions)

Per-job cost:
  Rejected by score:                    1 call  (~800 tokens)
  Eligible but queued (not picked):     1 call  (~800 tokens)
  Picked for application, no Qs:        2 calls (~2600 tokens)
  Picked + form questions:              3 calls (~4600 tokens)

Note: Call 1b runs ONLY for jobs picked for application this run.
Queued jobs don't trigger Call 1b until they're picked.

Daily expected: ~600 calls — well within 1500/day free tier.
```

---

## 6. Stack

```
Language          Python 3.11+
Scraping          JobSpy (Indeed, Glassdoor)
Browser           Playwright
NLP validation    spaCy
Embeddings        sentence-transformers (all-MiniLM-L6-v2)
LLM               Gemini 2.0 Flash (free tier)
LLM structuring   Instructor + Pydantic
Database          PostgreSQL on Neon (3GB free, pgvector)
DB driver         psycopg3
ORM               SQLAlchemy 2.0
Resume building   python-docx (template manipulation)
PDF conversion    LibreOffice headless
PDF rendering     reportlab (cover letter)
Notifications     Telegram Bot API (python-telegram-bot)
Reporting         gspread + Google Docs API
Logging           structlog
Hosting iter 1-4    Local machine
Hosting iter 5+     Oracle Cloud Always Free VM
File storage      Oracle Cloud VM disk (200GB free)
Total cost        $0 forever
```

---

## 7. Repo Structure

```
job-bot/
├── master_profile.yaml             (gitignored)
├── resumes/
│   ├── templates/
│   │   ├── resume_template.docx
│   │   └── cover_template.docx
│   └── applied/                    (gitignored, permanent audit trail)
│       └── <job_id>_<timestamp>.pdf
├── data/
│   ├── sessions/                   (Playwright cookies)
│   ├── manual_queue/               (screenshots)
│   ├── logs/
│   └── pending_writes.jsonl        (DB outage buffer)
├── config/
│   ├── config.yaml
│   └── role_clusters.yaml
├── src/
│   ├── scheduler.py                # Layer 1
│   ├── scraper/                    # Layer 2
│   │   ├── __init__.py
│   │   ├── jobspy_wrapper.py
│   │   ├── rotation.py             # serial search rotation state
│   │   └── filters.py              # hard filters
│   ├── parser.py                   # Layer 3
│   ├── scorer/                     # Layer 4
│   │   ├── __init__.py
│   │   ├── embeddings.py
│   │   ├── selector.py             # selection algorithm (pure functions)
│   │   ├── ordering.py             # match-then-recency, section order
│   │   └── apply_decision.py       # cycle-aware top-N picking
│   ├── builder/                    # Layer 5
│   │   ├── __init__.py
│   │   ├── llm_call.py             # combined Call 1b
│   │   ├── skills_validator.py     # post-validate LLM skills output
│   │   ├── assembler.py            # DOCX template manipulation
│   │   ├── hyperlinks.py           # update r:id targets in rels
│   │   └── pdf_convert.py
│   ├── sender/                     # Layer 6
│   │   ├── __init__.py
│   │   ├── indeed.py
│   │   ├── glassdoor.py
│   │   ├── fields.py               # field discovery + classification
│   │   ├── questions.py            # 4-category handling
│   │   ├── bank.py                 # answer bank match + adapt
│   │   ├── cover_letter.py
│   │   ├── pdf_render.py
│   │   └── voice.py                # human-voice validation
│   ├── state/                      # Layer 7
│   │   ├── __init__.py
│   │   ├── models.py               # SQLAlchemy models
│   │   ├── migrations/             # Alembic
│   │   ├── master_profile.py       # validate, rebuild, embed
│   │   ├── queue.py                # application_queue management
│   │   └── cleanup.py
│   ├── notifications.py            # Layer 8
│   ├── analytics.py                # Layer 9
│   ├── llm/
│   │   ├── client.py
│   │   ├── schemas.py              # Pydantic schemas
│   │   └── prompts.py
│   ├── cli/                        # debug + admin commands
│   │   ├── inspect.py              # show pipeline state per job
│   │   ├── dryrun.py
│   │   └── reparse.py
│   └── main.py                     # orchestrator
├── tests/
├── .env                            # NEON_DATABASE_URL, GEMINI_API_KEY, etc.
├── .gitignore
├── requirements.txt
├── PRD.md
├── CLAUDE.md
├── CHANGELOG.md
└── README.md
```

---

## 8. Build Sequence

The build proceeds in **iterations**, not layer-by-layer. Each iteration produces an end-to-end testable system. Subsequent iterations add depth to existing layers rather than introducing new ones.

This means: after Iteration 1, you can run the full pipeline (scrape → score → build → send) and see real output. Every iteration after that just makes each layer smarter.

```
ITERATION 0 — SCAFFOLD

  1. Repo structure (Section 7) with empty __init__.py files
  2. .gitignore (master_profile.yaml, .env, resumes/applied/, sessions, logs)
  3. requirements.txt pinned per Section 6
  4. config/config.yaml — every tunable from this doc, no hardcoded values
     anywhere in code
  5. .env.example
  6. master_profile.example.yaml — full schema, REPLACE_ME values
  7. answer_bank_seed.example.yaml — same
  8. tests/conftest.py + tests/test_smoke.py (pytest runs green)
  9. src/main.py — orchestrator with docstring flow, no implementation
  10. README.md with setup instructions
  11. CHANGELOG.md — initialized with the "Unreleased" section and an
      "Iteration 0 — Scaffold" entry listing what was scaffolded. Format
      follows Keep a Changelog (see CLAUDE.md for the full rules and
      template).

  Acceptance: pytest passes. Project tree matches Section 7. CHANGELOG.md
  exists with at least one entry.

ITERATION 1 — END-TO-END SKELETON (the "hello world" pipeline)

  Goal: every layer exists as a minimal stub that returns valid data.
  The pipeline runs end-to-end with mocked or trivial implementations.
  Nothing applies to a real portal; nothing makes real LLM calls.

  Steps:
  1. Layer 7 minimal: SQLAlchemy models + Alembic migrations for ALL
     tables. Run migrations against Neon. Empty rows, no data yet.
  2. Layer 1 minimal: a Python entry point invokable as
       python -m src.main --dry-run
     that walks through all 9 layers in sequence.
  3. Layer 2 stub: returns 2-3 hardcoded fake jobs (no JobSpy call yet).
     Writes them to all_jobs and processing_queue.
  4. Layer 3 stub: a mock JD parser that returns a fixed JDParsed object
     (no Gemini call yet). spaCy validation runs but on fake data.
  5. Layer 4 stub: a minimal scorer that reads master_bullets from DB
     (which is empty — so this returns a "no match" path), records the
     decision in not_applied with reason=LOW_SCORE.
  6. Layer 5 stub: a no-op (since Layer 4 always rejects in Iteration 1).
     But the file exists with a function signature.
  7. Layer 6 stub: a no-op for the same reason.
  8. Layer 8 minimal: send a real Telegram message at end of run
       "Iteration 1 dry run complete. 0 jobs applied. 3 rejected."
     This confirms Telegram wiring works end-to-end.
  9. Layer 9 stub: a no-op write to a placeholder file.

  Acceptance:
    - python -m src.main --dry-run completes without errors
    - 3 fake jobs end up in all_jobs and not_applied
    - Telegram receives the summary message
    - All tests pass (every layer has at least one mock-based unit test)

  After this: the user writes a real master_profile.yaml. The agent
  doesn't proceed to Iteration 2 until the master_profile is in place.

ITERATION 2 — REAL DATA FLOW (no auto-applying yet)

  Goal: replace stubs with real implementations, layer by layer, but
  keep the system in DRY mode. Auto-apply still disabled.

  Steps:
  1. Layer 2 real: integrate JobSpy. Serial rotation. Short-circuit at
     20 jobs. Indeed only for now (Glassdoor follows in Iteration 4).
  2. Layer 7 extended: master_profile rebuild script. User's
     master_profile.yaml gets parsed, embedded, written to DB.
  3. Layer 3 real: Gemini Call 1a with Instructor. spaCy validation.
     Hard filter re-check. Role acceptance against clusters.
  4. Layer 4 real: full selection algorithm. Both scoring formulas.
     Cycle-aware top-N picking. Queue with 12-hour decay.
  5. Layer 5 partial: selector + Call 1b LLM call. DOCX assembler
     using the user's template. PDF conversion via LibreOffice.
     Resume gets SAVED to resumes/applied/ but NOT submitted anywhere.
  6. Layer 6 NOT YET — sender stays as a no-op.
  7. Layer 8 extended: real morning digest format with applied/skipped
     breakdown and resume_path links.
  8. Layer 9 minimal: Sheets integration with read-only views of
     applied and not_applied. No monthly Doc yet.

  Acceptance:
    - python -m src.main --dry-run scrapes real Indeed jobs
    - JDs parsed correctly (spot-check 5)
    - Resumes built and saved in resumes/applied/ — visually correct
    - Sheets shows applied (would-be) and not_applied with reasons
    - Telegram digest delivered
    - Run dry for 2-3 days, review every selection. Tune thresholds
      in config.yaml if needed.

ITERATION 3 — ENABLE SUBMISSION

  Goal: turn on auto-apply. Indeed only.

  Steps:
  1. Layer 6 real: Indeed Easy Apply via Playwright. Session cookies.
     Multi-page form handling. Page signature loop detection.
  2. Layer 6 fields: field discovery and classification (profile/salary/
     upload/question/yesno). Fixed filename upload.
  3. Layer 6 cover letter: detect cover letter field, fill or render.
  4. Layer 6 questions: 4-category classifier. answer_bank lookup. Safe
     mode = hold for review. Telegram inline approve/edit/skip.
  5. Layer 6 manual-required flow: when auto-apply impossible, log to
     not_applied with reason + surface in digest.
  6. Layer 5 Gemini Call 2 integration when unknown questions exist.

  Acceptance:
    - First successful auto-apply confirmed (verify by checking Indeed)
    - Safe mode triggers correctly on unknown questions
    - Manual-required jobs surface in digest with resume PDF path
    - applied table populated with full audit trail (resume_path,
      selection_json, scores)
    - No duplicate applications (cooldown + job_id dedup verified)

  After this: 1 week of dry observation with applying enabled, but
  user manually approves each application via Telegram before the bot
  hits submit. This is a "pseudo-bold" mode for trust-building.

ITERATION 4 — ROUND OUT FEATURES

  Goal: fill in the lower-priority pieces that were skipped earlier.

  Steps:
  1. Glassdoor scraper integration (most results MANUAL_REQUIRED due
     to external redirects — that's fine, audit trail still works)
  2. Layer 9 monthly Google Doc with Gemini-synthesized Sunday report
  3. answer_bank growth from user-approved Telegram responses
  4. Storage cleanup cron (Sunday 8am IST)
  5. Portal health monitoring with consecutive-zeros alert
  6. CLI debug commands (src/cli/inspect.py, dryrun.py, queue.py)

  Acceptance:
    - First Sunday report received in Google Doc
    - answer_bank has 10+ entries from real review approvals
    - Storage cleanup ran and pruned old screenshots/logs

ITERATION 5 — PRODUCTION DEPLOYMENT

  Goal: move from local to Oracle Cloud Always Free VM.

  Steps:
  1. Provision Oracle Cloud VM (Always Free tier)
  2. Install dependencies (Python, LibreOffice, Playwright + Chromium)
  3. Transfer code, .env, resume template
  4. Re-login to Indeed manually on the VM (save session cookies)
  5. Set up cron schedule (peak 40min, off-peak 4h, Sunday 8am)
  6. Set up systemd service or pm2 for crash recovery
  7. Mirror data/logs to a local rsync target weekly

  Acceptance:
    - Bot runs for 7 days unattended on VM
    - Telegram digest arrives every morning
    - No crashes; no manual intervention needed

ITERATION 6 — EXPANSION (optional, only when needed)

  - Naukri scraper + sender
  - Residential proxy if Indeed starts blocking
  - Anti-detection refinements

ITERATION 7 — OPTIONAL

  - Custom MCP server for Claude-as-dashboard
```

**Why iterative over layer-by-layer:**

Building one layer fully before starting the next produces a system that's untestable until the very end. By the time you reach Layer 6, you discover Layer 4's scoring is off — but Layers 2-5 have been built around that broken assumption.

The iterative approach guarantees a runnable system after every iteration. Bugs and design mistakes surface within hours, not weeks. Each layer gets multiple passes — first as a stub, then with real data, then with real submission, then refined.

The trade-off: more total work (each layer gets touched 2-4 times). The gain: each touch is small, focused, and verifiable. No "big bang" integration phase at the end.

---

## 9. Edge Cases & Mitigations

| Edge case | Mitigation |
|---|---|
| Laptop sleeps | Oracle Cloud VM, not local |
| Same job across portals | Dedup via all_jobs job_id |
| JobSpy HTML breaks | portal_health alert |
| Indeed IP blocks | Iteration 6 residential proxy |
| Gemini hallucinates skills | spaCy validator catches |
| Gemini exceeds Familiar With cap | Pydantic max_length=4 |
| Gemini picks unauthorized title | Literal[tuple(aliases)] enforces |
| Gemini puts skill not in skills_pool | Post-validation rejects, regenerate |
| Gemini duplicates skill across categories | Post-validation rejects, regenerate |
| master_profile YAML invalid | Validation fails, alert, abort run |
| Bullet referenced by old applied row | Stays as is_active=false, never deleted |
| User edits master_profile | Auto-detected on mtime, rebuild next run |
| Fewer than 2 experiences pass threshold | Force-include strongest 2 |
| Fewer than 2 projects pass threshold | Force-include best 2, never hide section |
| Project has <2 qualifying bullets | Force-include best 2 |
| No keyword hits 20 jobs in a run | Process what was found, continue rotation |
| All eligible jobs in run already in queue | Pick from queue, don't double-count |
| Job in queue but final_score drops on re-eval | Use original score, not re-evaluated |
| Queue grows unbounded | Sunday cleanup expires past-12hr rows |
| Apply button missing | MANUAL_REQUIRED |
| External redirect mid-flow | MANUAL_REQUIRED |
| CAPTCHA | MANUAL_REQUIRED |
| Session cookie expired | Immediate alert, skip portal |
| Unknown field type | Screenshot, MANUAL_REQUIRED |
| Unknown screening question | Gemini draft → hold for review |
| Category 4 question | MANUAL_REQUIRED |
| JD specifies salary below 6 LPA | Use JD upper bound (apply anyway) |
| JD specifies salary above 6 LPA | Use JD upper bound |
| JD silent on salary | Default 6 LPA |
| Salary in non-INR currency | Use absolute number, conversion deferred |
| Cover letter textarea | Fill text directly |
| Cover letter file upload | Render PDF, upload as fixed filename, delete temp |
| No cover letter field | Skip |
| AI-sounding answer | Banned pattern check + regenerate up to 2x |
| Fabricated content in answer | master_profile text validation, regenerate |
| LLM JSON parse fails | Instructor enforces schema |
| Neon cold start | ~500ms accepted |
| Neon down | Buffer to local jsonl, drain next run |
| PDF disk growth | resumes/applied/ permanent (audit), other dirs cleaned weekly |
| Wrong filename uploaded | Verified rename before upload |
| Multi-page form unknown Q | Atomic abandon-and-restart on approval |
| Page loop in Playwright | Page signature tracking, max 10 pages |
| DOCX template structure changes | Assembler may fail — document expected structure clearly |
| DOCX hyperlinks broken on clone | Relationship file (rels) updates explicitly maintained |

---

## 10. MCP Scope

MCPs not used in the pipeline. The bot is standalone Python.

Iteration 7 (optional): custom MCP server exposing bot stats so Claude can act as a conversational dashboard.

---

**End of architecture document.**
