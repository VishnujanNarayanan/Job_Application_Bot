"""CLI — move operator assets between the laptop and S3.

    python -m src.cli.assets push    # laptop -> S3 (run after editing the profile)
    python -m src.cli.assets pull    # S3 -> here (run by the GitHub Actions job)

The pipeline needs three operator-owned files that are deliberately NOT in
git (they carry personal data):

    master_profile.yaml   the source of truth the operator edits
    master_profile.json   the canonical parsed form the scorer loads
    <template>.docx       hashed by the builder for `template_version`

A GitHub Actions runner therefore has no way to see them. GitHub secrets
can't carry them either — the cap is 48 KB and the profile is ~142 KB. S3 is
the transport: the bucket already exists for the render cache and the
workflow already holds AWS credentials, so this adds no new service and no
new secret.

Assets live under ``assets/`` in the bucket, outside the ``*_cache/``
prefixes so the 1-month cache lifecycle rule can't expire them.
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
    ]


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
    args = parser.parse_args(argv)

    return push() if args.direction == "push" else pull()


if __name__ == "__main__":
    raise SystemExit(main())
