# LinkedIn Easy Apply bot

Applies on its own to LinkedIn **Easy Apply** jobs the pipeline matched, each
with **that job's tailored resume**. It runs only while the laptop is on:
every 30 minutes, Monday to Friday, 09:00–18:00 IST,
every Easy Apply match of the run, at most 20 a day (`autoapply:` in `config/config.yaml`).

> LinkedIn's user agreement forbids automated applying. If LinkedIn notices,
> it can restrict the account. The bot keeps the risk down: it uses your real
> browser on your home connection, goes at a person's pace, and stays under a
> low daily cap. It can't make the risk zero.

## How a run works

1. **Scrape the last hour.** `python -m src.main --term "data engineer" --hours-old 1`
   searches LinkedIn (and the other sites) for that term, posted in the last
   hour, and scores and tailors them; you get the usual Telegram match
   messages. Output goes to `data/logs/autoapply-pipeline.log`.
2. **This scrape's LinkedIn matches only.** Every LinkedIn job this run
   matched (any title the search returned) is a candidate, best score first.
   Nothing older, nothing from elsewhere. (LinkedIn's own search page has
   become an "AI-powered job search" that drops the past-hour filter and
   hides job ids, so the scrape is the search.)
3. **Every one with Easy Apply** is applied to in the same run, up to 20 a
   day. Jobs that apply on a company site are skipped.
4. **Open it in the Job Bot Edge.**
   - This is the real Microsoft Edge on Windows, with its own profile, logged
     into LinkedIn once by hand.
   - The bot attaches over Edge's DevTools port, through a small relay it
     starts on Windows, and only clicks and types.
   - The job's own Easy Apply button is pressed (the "Easy Apply" tags on
     similar-job cards are ignored).
5. **Easy Apply** (LinkedIn's apply flow is a native `<dialog>`).
   - **Contact info:** LinkedIn's pre-fill is kept.
   - **Resume:** LinkedIn pre-selects the one used last time. The bot presses
     "Upload resume", catches the file picker before it opens, hands it this
     job's tailored PDF, and checks the new card **dated today** is the
     selected one. Otherwise it gives up on the job.
   - **Questions** are filled by the same userscript you use by hand.
     - LinkedIn pre-fills answers you gave before. Where you have a stored
       answer, yours replaces LinkedIn's (e.g. experience with a tool = your
       total years).
     - Where you don't, LinkedIn's remembered answer stays.
   - Then Next, Review, Submit. A dry run stops before Submit, with a
     screenshot.
6. **No job is skipped over a question.** A required, empty question with no
   stored answer gets a default, which is listed in the Telegram report so you
   can correct it:
   - ability or willingness → Yes;
   - sponsorship → No; authorised to work → Yes;
   - convictions, clearances, dismissals or relatives → No;
   - disability or veteran → prefer not to say;
   - "how many years" → your total experience;
   - other pick-lists → Yes, else the first option;
   - consent → ticked;
   - text → "Please refer to my resume".

   Defaults are never learned; your stored and typed answers always win.
   `never_skip: false` makes such jobs skip instead.
7. **Recorded.**
   - An applied job is marked `applied` in the database, counted for the day,
     and reported on Telegram.
   - Screenshots are in `data/autoapply/screens/` (local, gitignored).

## Setup (once)

1. **Mirrored networking**, so WSL can reach Windows' localhost.
   `%USERPROFILE%\.wslconfig` holds `networkingMode=mirrored`. Run
   `wsl --shutdown` from Windows and reopen the terminal. Edge's own port
   still refuses WSL, so the bot reaches it through a small PowerShell relay
   it starts by itself (port 9224, user-level, hidden).
2. **Playwright** (only the Python library; the bot uses your installed Edge):
   `pip install -r requirements-e2e.txt`.
3. **Log in:** `python -m src.cli.autoapply login`. This opens the Job Bot
   Edge on LinkedIn's login page. Log in, including any verification, and
   leave that Edge open; it can be minimised.
4. **Dry runs first:** `python -m src.cli.autoapply run --force`. This fills a
   real application, stops before Submit, saves a screenshot and messages
   Telegram. Check a few.
5. **Switch on:** set `autoapply.enabled: true` and `autoapply.dry_run: false`,
   then start the loop in tmux:
   `tmux new-window -t bot -n autoapply '.venv/bin/python -m src.cli.autoapply loop'`.

`python -m src.cli.autoapply status` shows today's count and the last results.
To stop, close the tmux window or set `enabled: false`.

## Tests

`tests/e2e/test_autoapply_zoo.py` runs the bot on a LinkedIn-shaped page in
the form zoo, which pre-selects an old resume:

- the dry run fills every step and stops before Submit;
- the live run submits;
- an unanswered question discards and skips the job.

`tests/test_autoapply_run.py` covers the working hours, the daily cap and the
off switch.
