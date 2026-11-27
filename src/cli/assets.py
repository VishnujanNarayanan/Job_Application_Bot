"""CLI — move operator assets between the laptop and S3.

    python -m src.cli.assets push              # laptop -> S3 (run after editing the profile)
    python -m src.cli.assets pull              # S3 -> here (run by the GitHub Actions job)
    python -m src.cli.assets push --backups    # only the backup-only files below
    python -m src.cli.assets pull --backups    # restore them on a new laptop

The pipeline needs three operator-owned files that are deliberately NOT in
git (they carry personal data):

    master_profile.yaml   the source of truth the operator edits
    master_profile.json   the canonical parsed form the scorer loads
    config/skills_registry.local.yaml
                          registry labels for pool-only terms (#188)
    <template>.docx       hashed by the builder for `template_version`

A GitHub Actions runner therefore has no way to see them. GitHub secrets
can't carry them either — the cap is 48 KB and the profile is ~142 KB. S3 is
the transport: the bucket already exists for the render cache and the
workflow already holds AWS credentials, so this adds no new service and no
new secret.

Assets live under ``assets/`` in the bucket, outside the ``*_cache/``
prefixes so the 1-month cache lifecycle rule can't expire them.

Backup-only files (``assets/backup/``) are personal files the pipeline never
reads -- the autofill's answers (#154). ``push`` uploads them too, so a WSL
reset can't lose them; the runner's ``pull`` leaves them alone, and only
``pull --backups`` brings them back. They are pushed when present and never
required. They must never go in git: the repo is public.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import structlog

from src.config import settings

log = structlog.get_logger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
_PREFIX = "assets"


def _asset_paths() -> list[tuple[Path, str]]:
    """(local path, S3 key) for every operator asset the pipeline needs.

    The template path is config-driven, so a different instance with a
    differently-named template needs no code change.
    """
    template = Path(str(settings.endpoint.template_path))
    return [
        (_ROOT / "master_profile.yaml", f"{_PREFIX}/master_profile.yaml"),
        (_ROOT / "master_profile.json", f"{_PREFIX}/master_profile.json"),
        (_ROOT / template, f"{_PREFIX}/{template.name}"),
        (_ROOT / _REGISTRY_LOCAL, f"{_PREFIX}/{_REGISTRY_LOCAL.name}"),
    ]


#: Registry entries for terms only in the operator's skills pool (#188). Optional
#: on pull: a runner without it treats those terms as approved, as before.
_REGISTRY_LOCAL = Path("config/skills_registry.local.yaml")
_OPTIONAL = {_REGISTRY_LOCAL.name}

#: Personal files kept only for safe keeping: pushed when present, pulled only on request.
_BACKUPS = (Path("data/ats_answers.yaml"), Path("data/ats_learned.yaml"))


def _backup_paths() -> list[tuple[Path, str]]:
    return [(_ROOT / p, f"{_PREFIX}/backup/{p.name}") for p in _BACKUPS]


def push_backups(s3=None) -> int:
    """Upload the backup-only files that exist. Returns how many were pushed."""
    if s3 is None:
        from src.aws.iam_session import get_session

        s3 = get_session().client("s3")
    bucket = settings.aws.s3_bucket
    pushed = 0
    for path, key in _backup_paths():
        if not path.is_file():
            print(f"no {path.name} yet; skipped")
            continue
        s3.upload_file(str(path), bucket, key, ExtraArgs={"ServerSideEncryption": "AES256"})
        log.info("backup_pushed", path=str(path), key=key, bytes=path.stat().st_size)
        print(f"pushed {path.name} -> s3://{bucket}/{key}")
        pushed += 1
    return pushed


def pull_backups() -> int:
    """Restore the backup-only files. Refuses to overwrite a local copy."""
    from src.aws.iam_session import get_session

    s3 = get_session().client("s3")
    bucket = settings.aws.s3_bucket
    failed = False
    for path, key in _backup_paths():
        if path.exists():
            print(f"kept local {path} (delete it first to restore from S3)")
            continue
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, key, str(path))
            print(f"pulled s3://{bucket}/{key} -> {path}")
        except Exception as exc:  # noqa: BLE001
            print(f"no backup at s3://{bucket}/{key}: {exc}", file=sys.stderr)
            failed = failed or path.name == "ats_answers.yaml"
    return 1 if failed else 0


def push() -> int:
    """Upload local assets to S3. Fails loudly — a silent partial push would
    leave the runner using a stale profile without anyone noticing."""
    from src.aws.iam_session import get_session

    s3 = get_session().client("s3")
    bucket = settings.aws.s3_bucket
    missing = [p for p, _ in _asset_paths() if not p.is_file()]
    if missing:
        for path in missing:
            log.error("asset_missing", path=str(path))
        print(
            "Missing asset(s); nothing pushed:\n  "
            + "\n  ".join(str(p) for p in missing),
            file=sys.stderr,
        )
        return 1

    for path, key in _asset_paths():
        s3.upload_file(str(path), bucket, key)
        log.info("asset_pushed", path=str(path), key=key, bytes=path.stat().st_size)
        print(f"pushed {path.name} -> s3://{bucket}/{key}")
    push_backups(s3)
    return 0


def pull() -> int:
    """Download assets from S3 into the working tree (used on the runner)."""
    from src.aws.iam_session import get_session

    s3 = get_session().client("s3")
    bucket = settings.aws.s3_bucket

    failed = False
    for path, key in _asset_paths():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, key, str(path))
            log.info("asset_pulled", key=key, path=str(path), bytes=path.stat().st_size)
            print(f"pulled s3://{bucket}/{key} -> {path}")
        except Exception as exc:
            if path.name in _OPTIONAL:
                log.warning("optional_asset_missing", key=key, error=str(exc)[:160])
                print(f"skipped optional s3://{bucket}/{key}: {exc}", file=sys.stderr)
                continue
            log.error("asset_pull_failed", key=key, error=str(exc))
            print(f"FAILED s3://{bucket}/{key}: {exc}", file=sys.stderr)
            failed = True
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=logging.INFO)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )

    parser = argparse.ArgumentParser(prog="src.cli.assets")
    parser.add_argument("direction", choices=("push", "pull"))
    parser.add_argument("--backups", action="store_true",
                        help="only the backup-only personal files (autofill answers)")
    parser.add_argument("--allow-keyword-gaps", action="store_true",
                        help="push even if a bullet proves a keyword with no phrase (#138)")
    args = parser.parse_args(argv)

    if args.backups:
        if args.direction == "pull":
            return pull_backups()
        push_backups()
        return 0
    if args.direction == "push" and not args.allow_keyword_gaps and keyword_gaps():
        return 1
    return push() if args.direction == "push" else pull()


def keyword_gaps() -> bool:
    """#138 gate: True (and the list printed) when an entry's bullets prove a
    keyword its keyword_lines have no phrase for. Shared with reparse."""
    from src.state import keyword_lines

    report = keyword_lines.check_yaml(_ROOT / "master_profile.yaml")
    if not report.missing:
        return False
    print(report.render(), file=sys.stderr)
    print("\nNothing pushed. " + keyword_lines.GATE_HELP, file=sys.stderr)
    return True


if __name__ == "__main__":
    raise SystemExit(main())
