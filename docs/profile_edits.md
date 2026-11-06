# Profile edits: adding a keyword (#138)

Read this before adding or changing any bullet in `master_profile.yaml`
(gitignored; `master_profile.example.yaml` shows the format).


Grouped keyword lines put several of an entry's keywords into one line, built
from that entry's `keyword_lines` (phrases + frames). A keyword with no phrase
never reaches a grouped line. **Whenever a bullet or extra bullet gains a
keyword** (a new tool, method or practice), in the same edit:

1. **Phrase:** add one to that entry's `keyword_lines.phrases`: a short clause
   naming the keyword, true to its `source` bullet (a bullet of the same entry
   that says it). Never claim more than the bullet does ("weighed Airflow",
   not "ran Airflow").
2. **Frames:** add the phrase id to the `allowed` list of **every** frame whose
   verb is true for it. If none fits, write a new frame: a clause with `{kws}`
   and its own ending (the business WHY, true for every member, not repeating
   any bullet's words). Frames are tried first; make them as large as honesty
   allows.
3. **Or skip:** if the match is not really that skill (a wrong-sense word, a
   domain word, a combined spelling covered by other phrases), list the term
   under `keyword_lines.skip` with a comment saying why.
4. **Check:** `python -m tools.keyword_lines_check` must report 0 missing.
   `python -m src.cli.reparse` and `python -m src.cli.assets push` refuse a
   profile with gaps (`--allow-keyword-gaps` overrides; don't use it to skip
   the work).

New terms also go to the skills registry (#188, weekly approval). Wording of
bullets, phrases and endings is hand-written and approved by the operator:
plain business language, no buzzwords. Tools used in the past count as evidence
(git history), worded in the past tense.
