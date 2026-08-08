"""AWS S3 helpers — render cache + daily selection_json backup (§5 / §7.2).

Cache layout:
  s3://{bucket}/pdf_cache/{cache_key}.pdf    (1-month TTL, S3 lifecycle rule)
  s3://{bucket}/docx_cache/{cache_key}.docx

Backup layout:
  s3://{bucket}/backups/selection_json/{YYYY-MM-DD}/applied_{timestamp}.jsonl

``cache_key`` = ``{job_id}_{template_version}``; the format suffix is
appended by the caller as part of the S3 object key.

IAM permissions needed (hard rule #19):
  s3:PutObject, s3:GetObject, s3:DeleteObject on this bucket only.
"""

from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path

import structlog

from src.aws.iam_session import get_session
from src.config import settings

log = structlog.get_logger(__name__)

_MONTH_SECONDS = 30 * 24 * 3600
# AWS SigV4 hard limit on presigned URL lifetime.
_MAX_PRESIGN_SECONDS = 7 * 24 * 3600


def _s3():
    return get_session().client("s3")


def _bucket() -> str:
    return settings.aws.s3_bucket


def _prefix(ext: str) -> str:
    return f"{ext}_cache"


def cache_put(local_path: str | Path, cache_key: str, ext: str) -> str:
    """Upload a rendered file to S3; return the S3 URI.

    On upload failure: logs ``s3_cache_failed`` and re-raises so the
    caller can decide whether to serve without caching (§7.5 degraded mode).
    """
    key = f"{_prefix(ext)}/{cache_key}.{ext}"
    bucket = _bucket()
    try:
        _s3().upload_file(str(local_path), bucket, key)
        uri = f"s3://{bucket}/{key}"
        log.info("s3_cache_put", cache_key=cache_key, ext=ext, uri=uri)
        return uri
    except Exception as exc:
        log.error("s3_cache_failed", op="put", cache_key=cache_key, error=str(exc))
        raise


def cache_get_bytes(cache_key: str, ext: str) -> bytes | None:
    """Download a cached render from S3; return bytes or None on miss/error."""
    key = f"{_prefix(ext)}/{cache_key}.{ext}"
    bucket = _bucket()
    buf = io.BytesIO()
    try:
        _s3().download_fileobj(bucket, key, buf)
        log.info("s3_cache_hit", cache_key=cache_key, ext=ext)
        return buf.getvalue()
    except Exception as exc:
        # botocore.exceptions.ClientError with 404 is a normal cache miss.
        log.debug("s3_cache_miss", cache_key=cache_key, ext=ext, error=str(exc))
        return None


def cache_presigned_url(
    cache_key: str, ext: str, expires_seconds: int
) -> str | None:
    """Return a time-limited public URL for a cached render, or None.

    Used when the pipeline runs somewhere that isn't serving HTTP (a GitHub
    Actions runner): the resume endpoint on the operator's laptop is
    unreachable, so the notification links point straight at S3 instead.

    SigV4 caps presigned URLs at 7 days; longer expiries are clamped, since
    the alternative is a URL that AWS rejects outright. Signing is a local
    computation — no network call, and no object existence check, so callers
    must only presign keys they have just uploaded.

    Needs only ``s3:GetObject``, already granted per hard rule #19.
    """
    key = f"{_prefix(ext)}/{cache_key}.{ext}"
    expires = min(int(expires_seconds), _MAX_PRESIGN_SECONDS)
    try:
        url = _s3().generate_presigned_url(
            "get_object",
            Params={"Bucket": _bucket(), "Key": key},
            ExpiresIn=expires,
        )
        log.info("s3_presigned", cache_key=cache_key, ext=ext, expires_in=expires)
        return url
    except Exception as exc:
        log.error("s3_presign_failed", cache_key=cache_key, ext=ext, error=str(exc))
        return None


def backup_selections(date_str: str, rows: list[dict]) -> None:
    """Export a list of selection dicts to S3 as a JSONL backup.

    ``date_str`` should be ``YYYY-MM-DD``. Non-fatal: logs on failure.
    """
    import json
    import time

    key = f"backups/selection_json/{date_str}/applied_{int(time.time())}.jsonl"
    payload = "\n".join(json.dumps(r) for r in rows).encode()
    try:
        _s3().put_object(Bucket=_bucket(), Key=key, Body=payload)
        log.info("s3_backup_written", date=date_str, key=key, rows=len(rows))
    except Exception as exc:
        log.error("s3_backup_failed", date=date_str, error=str(exc))
