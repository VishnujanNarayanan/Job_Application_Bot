"""LinkedIn Easy Apply bot (src/autoapply).

    python -m src.cli.autoapply login            # open the Job Bot Edge on LinkedIn's login page (once)
    python -m src.cli.autoapply run [--live] [--force]   # one run now
    python -m src.cli.autoapply loop             # every 30 min in working hours, while the laptop is on
    python -m src.cli.autoapply status           # today's count and the last outcomes

``run`` is a dry run unless --live (or autoapply.dry_run: false): it fills the
Easy Apply form, stops before Submit, saves a screenshot and messages Telegram.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time

from src.autoapply import edge, run as runner


def _login() -> int:
    edge.ensure_edge("https://www.linkedin.com/login")
    print("The Job Bot Edge is open on LinkedIn's login page. Log in there (with any verification),\n"
          "tick 'Remember me' if offered, then close nothing: the bot reuses this window.")
    return 0


def _status() -> int:
    state = runner.load_state()
    day = runner.now_local().strftime("%Y-%m-%d")
    print(f"applied today: {runner.applied_today(state, day)}/{int(runner.cfg().max_per_day)}"
          f" | in window now: {runner.in_window()} | enabled: {bool(runner.cfg().enabled)}"
          f" | dry_run: {bool(runner.cfg().dry_run)} | Edge up: {edge.is_running()}")
    rows = sorted(((v["at"], k, v) for key in ("tried", "dry_tried") for k, v in state.get(key, {}).items()),
                  reverse=True)[:10]
    for at, job_id, v in rows:
        print(f"  {at[:16]}  {v['status']:15} {v.get('company', '')[:24]:24} {v.get('role', '')[:40]}  {v.get('detail', '')[:60]}")
    return 0


def _loop() -> int:
    every = int(runner.cfg().every_minutes) * 60
    print(f"auto-apply loop: every {every // 60} min, {runner.cfg().days} {runner.cfg().hours} "
          f"{runner.cfg().timezone}; Ctrl+C to stop")
    while True:
        try:
            result = runner.run_once()
            print(time.strftime("%H:%M"), json.dumps(result)[:300], flush=True)
        except Exception as exc:  # noqa: BLE001 - the loop outlives one bad run
            print(time.strftime("%H:%M"), "run failed:", exc, flush=True)
        # Next slot on the half hour, plus up to 3 min of jitter so it isn't clockwork.
        time.sleep(every - (time.time() % every) + random.uniform(0, 180))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="src.cli.autoapply", description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=("login", "run", "loop", "status"))
    ap.add_argument("--live", action="store_true", help="really submit (default: dry run, per config)")
    ap.add_argument("--force", action="store_true", help="ignore working hours and the enabled switch")
    args = ap.parse_args(argv)
    if args.command == "login":
        return _login()
    if args.command == "status":
        return _status()
    if args.command == "loop":
        return _loop()
    result = runner.run_once(dry_run=False if args.live else None, force=args.force)
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
