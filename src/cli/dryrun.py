"""CLI — Full dry run: scrape/parse/score/build, notify to test chat only."""

from src.main import main

if __name__ == "__main__":
    raise SystemExit(main(["--dry-run"]))
