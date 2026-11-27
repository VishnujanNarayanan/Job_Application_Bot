# Application-form autofill (#154)

Tap **Apply** on a Telegram match and the company's application form opens with
the common fields filled in and the tailored resume attached. You review it and
press Submit yourself. **Nothing is ever submitted automatically.**

Desktop only for now (Zen browser + Violentmonkey). Phone support is not set up.

## How it works

```
Telegram Apply ─→ https://jobs.lever.co/acme/123#jab=<job_id>
                      │
   laptop off ────────┴─→ an ordinary link: the posting opens as before
   laptop on
                      ▼
  userscript (Violentmonkey)
    reads the tag, then every empty field on the page: label, type, options
    POST /autofill/match ──→ endpoint (laptop, tailnet only)
                              answers from master_profile.yaml, config.yaml,
                              data/ats_answers.yaml; records new questions
    fills what is known, attaches /resume/<job_id>.pdf,
    outlines the rest, fills each later page (Workday) as it appears
```

The `#jab=` fragment never reaches the ATS's server, and the script removes it
from the address bar once it has read it. A link that already has a `#` is left
untagged (some sites route on it). Set `autofill.tag_apply_links: false` in
config.yaml to stop tagging.

On an ATS page opened without the tag, nothing is filled: an **Autofill** button
appears once a form is on screen. The Violentmonkey menu also has
"Autofill this form". Untagged pages get no tailored resume, because the script
doesn't know which match they belong to.

## Setup

1. Install Violentmonkey in Zen.
2. With the endpoint running, open
   `<endpoint.base_url>/autofill/jab.user.js` (for example
   `https://<machine>.ts.net/autofill/jab.user.js`) and confirm the install.
   Updates come from the same URL. Without Tailscale, install from
   `http://localhost:8000/autofill/jab.user.js` instead: the script then
   calls the endpoint on localhost, which works in a browser on the same
   laptop.
3. Check `data/ats_answers.yaml` (gitignored). It was created with the salary
   rule and blank answers for common questions.

## What gets filled, and from where

| Source | Fields |
| --- | --- |
| `master_profile.yaml` `personal` | name (full, first, last), email, phone (with or without the country code, depending on whether the form has a separate code field), location, city, state, country, GitHub, LinkedIn, certificates link |
| `master_profile.yaml` work & education | current company and title (the `employment` entry still `present`), degree, field of study, university, graduation year |
| `config.yaml` | portfolio / website (`operator.resume_header.portfolio_url`), years of experience (`operator.years_experience`), willing to relocate (`relocation.enabled`) |
| `data/ats_answers.yaml` `salary` | current CTC, and expected CTC: the middle of the advert's range, never below `expected_min_lpa`. If the advert gives no range, the floor is used. A lone "8+ LPA" counts as 8. |
| `data/ats_answers.yaml` `answers`, facts (`key: value`) | `notice_days` (notice period; earliest start date = today + notice), `date_of_birth` (age, age range, "at least 18"), `citizenship` and `sponsorship_abroad` (see below), `how_heard`, `preferred_name`, `address`, `address_line1`, `address_line2`, `postal_code`; any profile fact can be overridden the same way |
| `data/ats_answers.yaml` `answers`, questions | anything else, matched on wording (`asks:`) |
| `data/ats_learned.yaml` | answers learned from what you submitted (below) |

Worked out for each form:

- **Dates** follow the field: a date picker gets `2026-12-12`, a box whose
  hint shows `DD/MM/YYYY` gets that, anything else `12 December 2026`.
- **Work authorisation and sponsorship** depend on the country the question
  names ("…to work in the US?"), else the job's location. Your citizenship
  country: authorised, no sponsorship. Anywhere else: not authorised, needs
  sponsorship. No country anywhere: your own is assumed, outlined.
- **How did you hear**: a job found on a board (LinkedIn, Naukri…) names that
  board; otherwise `how_heard` (e.g. company website).
- **Years with a tool** ("years of experience with Python") get your total
  years of experience.

A question answer can carry more than one form:

```yaml
  pronouns:
    asks: ["pronouns", "preferred pronouns"]
    answer: He/him            # a text box
    only_if_required: true    # ...left empty unless required
    choose: ["Prefer not to say", "Decline to state", "He/him"]   # options, first found wins
```

In a "select all that apply" group, every `choose` item present is ticked.
`answer:` may also be a list for that. A number box gets the number in the
answer ("89%" → 89).

Salary units follow the label. A field marked "(in LPA)" gets `6`. A
"per month" field gets a monthly rupee figure, and "INR" gets annual rupees. A
number box with no unit gets annual rupees, and a text box gets `6 LPA`. A form
asking in another currency (USD, $, €…) is left empty. Salary fields are always
outlined for checking.

Label phrasings live in `config/ats_fields.yaml`. When several phrasings match
one label, the longest wins, so "last name" beats "name". A field's `not:` list
keeps "company name" and "hiring manager name" away from your name.

## What the outlines mean

| Outline | Meaning |
| --- | --- |
| none | filled |
| amber | filled, please check (salary), or a dropdown that couldn't be opened from a script; the tooltip says what to choose |
| red | required, no answer |
| dashed blue | optional, no answer (diversity surveys, Twitter…) |

Consent boxes are never ticked unless an answer says yes. Unknown questions are
never guessed. A field that already has a value is never changed.

## The answer bank grows

A question with no answer is appended to `data/ats_answers.yaml` under
`unanswered:`, with its type and options and `answer: null`. Fill `answer:` in
place (an option's text, or a list for "select all that apply"), and every
later form asking the same thing gets it. You answer each question once:

- Same question, other words ("Gender" / "What gender do you identify as?",
  "What is your ethnicity?" / "I identify my ethnicity as…"): filled.
- Different words but the same list of options (two age-range questions with
  the same brackets): filled. Yes/No lists never count as the same.
- Probably the same ("Gender" / "Gender identity", "Ethnicity" / "Race /
  Ethnicity"): filled and outlined amber for checking.
- A question already in the file in any of these wordings, answered or not, is
  not added again.

An answer that isn't one of the form's options is never forced in; the field
stays outlined. Code only ever appends to that file, so keep `unanswered:` as
its last key.

## It learns from what you submit

When you press Submit (or Next, on a multi-page form), the script reads what
each field now holds and sends it to `/autofill/learn`. It doesn't delay or
block the submit; if the endpoint is down, the report is queued and sent from
the next ATS page you open.

- A field it couldn't answer, and you filled in: learned.
- A stored answer you changed before submitting: your version replaces it, for
  every wording of that question.
- A learned answer is filled with an amber outline until you have submitted it
  unchanged twice; after that it fills like any other. A correction starts the
  count again.
- Questions about one company ("have you worked here before?", "who referred
  you?", any question naming the company) are kept for that company only.
- Never learned: profile facts and the worked-out answers above (change those
  in the files), salary, open prompts ("why us", "tell us about…", cover
  letters), anything longer than a short answer, files, consent boxes.

Learned answers are in `data/ats_learned.yaml` (gitignored), with `confirms`
and a `history` of earlier answers. Edit an `answer:` or delete an entry
freely. Matching is word rules only, so it's instant and needs no AI.

Every question seen (questions and options, never values) is also logged to
`data/ats_questions_seen.jsonl`. To add the new ones to the public catalog:

```
python -m tools.ats_catalog_sync           # preview
python -m tools.ats_catalog_sync --write   # append to docs/ats_questions.yaml
```

Opens are logged to `data/logs/autofill_opens.jsonl`.

## Checked on live forms (2026-11-27)

The userscript was run on live postings with made-up answers, through a stubbed
endpoint, with nothing submitted:

- **Lever:** all fields filled and the resume attached. The location box is a
  type-ahead whose suggestions did not load in the headless browser, so it was
  outlined amber.
- **Greenhouse:** all fields filled and the resume attached, including the
  combined "Current CTC and Expected CTC" question. Its custom dropdowns
  (react-select, e.g. Country) ignore script events, so they are outlined amber
  with the answer to choose.
- **Ashby:** all fields filled, including Yes/No buttons and the state
  dropdown, and the resume attached. Its own "Autofill from resume" upload is
  left alone.
- **Workday:** step 1 is "Create Account/Sign In", which stays yours (the
  script never handles passwords). The later steps could not be reached
  without an account, so they are untested. They use the same generic reader
  and refill on each page change, but expect more amber outlines there at
  first.

## Limits

- The tailored resume comes from `/resume/<job_id>.pdf`, which needs the
  database. While Neon is locked, the resume field is outlined red instead.
  The advert's salary range also comes from the database; without it the
  expected-CTC floor is used.
- The endpoint has no login. Its only protection is being reachable on your
  tailnet alone. `/autofill/match` sends no CORS headers and requires an
  `X-JAB` header, so another web page in the same browser can neither read
  your answers from it nor make it write to the bank.
- Embedded Greenhouse forms on a company's own site (an iframe) are filled
  without the tag, so no tailored resume is attached there.
