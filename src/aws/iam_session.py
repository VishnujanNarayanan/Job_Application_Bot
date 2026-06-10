"""AWS IAM session factory (Layer 7 / §5.4).

Credentials are read exclusively from environment variables
(AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, optionally AWS_DEFAULT_REGION),
never from code or config files (hard rule #18). The session is cached so
the module is import-cheap and tests can monkeypatch ``get_session``.
"""

from __future__ import annotations

import os
from functools import lru_cache

from src.config import settings


@lru_cache(maxsize=1)
def get_session():
    """Return a boto3.Session configured from env + config region.

    Lazy import keeps boto3 out of the import path for modules that never
    call AWS. The lru_cache means the session is built at most once per
    process (cleared in tests via ``get_session.cache_clear()``).
    """
    import boto3

    region = os.environ.get("AWS_DEFAULT_REGION", settings.aws.region)
    return boto3.Session(
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        region_name=region,
    )
