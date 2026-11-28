# LinkedIn Easy Apply bot

Applies on its own to LinkedIn **Easy Apply** jobs the pipeline matched, each
with **that job's tailored resume**. It runs only while the laptop is on:
every 30 minutes, Monday to Friday, 09:00–18:00 IST, one application per run,
at most 20 a day (`autoapply:` in `config/config.yaml`).

> LinkedIn's user agreement forbids automated applying. If LinkedIn notices,
> it can restrict the account. The bot keeps the risk down: it uses your real
> browser on your home connection, goes at a person's pace, and stays under a
> low daily cap. It can't make the risk zero.

## How a run works

1. **Pipeline.** `python -m src.main --term "data engineer"`, as usual. This
   searches, scores and tailors, and you get the usual Telegram match
   messages. Its output goes to `data/logs/autoapply-pipeline.log`.
2. **Pick a job.** The best LinkedIn match from the last 48 hours that is
   still pending, has "data engineer" in its title, and hasn't been tried.
3. **Open it in the Job Bot Edge.**
   - This is the real Microsoft Edge on Windows, with its own profile, logged
     into LinkedIn once by hand.
   - To LinkedIn it's an ordinary Edge on this laptop: its own network
     handshake and fingerprint, no automation flags.
   - The bot attaches over Edge's DevTools port and only clicks and types.
4. **Easy Apply.**
   - No Easy Apply button (an external apply link): skipped.
   - Otherwise the dialog is filled by the same userscript you use by hand,
     from your profile, answers file and learned answers.
   - The tailored PDF is uploaded. LinkedIn pre-selects the resume you used
     last time, so the bot checks the fresh upload is the selected one, and
     gives up on the job if it can't.
   - Then Next, Review, Submit.
5. **Never guessed.**
   - A question with no stored answer, or an error from LinkedIn, discards the
     application and messages Telegram with the question. Answer it (or apply
     by hand once, so it's learned) and the next job asking it goes through.
6. **Recorded.**
   - An applied job is marked `applied` in the database (like the Telegram
     "Mark applied" button), counted for the day, and reported on Telegram.
   - Screenshots are in `data/autoapply/screens/` (local, gitignored).

## Setup (once)

1. **Mirrored networking**, so WSL can reach Edge's port on Windows.
   `%USERPROFILE%\.wslconfig` holds `networkingMode=mirrored`. Run
   `wsl --shutdown` from Windows and reopen the terminal.
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
