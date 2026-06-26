"""Central configuration accessor.

Loads ``config/config.yaml`` once at import and exposes it as ``settings``
with attribute access, e.g.::

    from src.config import settings
    threshold = settings.scoring.experience.threshold
    bucket = settings.aws.s3_bucket            # when present
    name = settings.operator.full_name

Tunables live in ``config.yaml``; secrets live in ``.env`` (read via
``os.environ`` elsewhere, never from here).

Operator identity is config-driven — no operator literal (name, filename)
appears in source (CLAUDE.md hard rule #21 / PRD NFR-11). Resume and cover
filenames are DERIVED from ``operator.full_name`` here, never hardcoded.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
import json
from pathlib import Path
from typing import Any

import yaml


def resolve_endpoint_base_url(config_url: str) -> str:
    """Return the public endpoint URL.

    If ngrok is running locally (port 4040 API), uses its current public
    HTTPS tunnel URL so Telegram resume links work on mobile without any
    manual config edits. Falls back to config.yaml value (used in production
    or when ngrok is not running).
    """
    try:
        with urllib.request.urlopen("http://localhost:4040/api/tunnels", timeout=1) as resp:
            data = json.loads(resp.read())
        for tunnel in data.get("tunnels", []):
            if tunnel.get("proto") == "https":
                return tunnel["public_url"]
    except Exception:
        pass
    return config_url

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "config.yaml"

# Collapse any run of whitespace to a single underscore for filenames.
_WHITESPACE = re.compile(r"\s+")


class Section:
    """Recursive attribute/item view over a config mapping.

    Nested dicts are wrapped lazily so ``settings.a.b.c`` works; missing
    keys raise ``AttributeError`` (attribute access) so typos fail loudly
    rather than returning ``None``.
    """

    __slots__ = ("_data",)

    def __init__(self, data: dict[str, Any]) -> None:
        object.__setattr__(self, "_data", data)

    def __getattr__(self, name: str) -> Any:
        # Only reached when normal lookup fails (so never for _data).
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            value = self._data[name]
        except KeyError as exc:
            raise AttributeError(
                f"config has no key '{name}' (available: {sorted(self._data)})"
            ) from exc
        return Section(value) if isinstance(value, dict) else value

    def __getitem__(self, key: str) -> Any:
        value = self._data[key]
        return Section(value) if isinstance(value, dict) else value

    def get(self, key: str, default: Any = None) -> Any:
        value = self._data.get(key, default)
        return Section(value) if isinstance(value, dict) else value

    def as_dict(self) -> dict[str, Any]:
        return self._data

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Section({sorted(self._data)})"


class Settings(Section):
    """Top-level settings plus operator-identity helpers."""

    def __init__(self, path: Path = _CONFIG_PATH) -> None:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        super().__init__(data)
        self._validate()

    def _validate(self) -> None:
        full_name = self._data.get("operator", {}).get("full_name")
        if not full_name or not str(full_name).strip():
            raise RuntimeError(
                "config.operator.full_name is required and must be non-empty "
                "(it derives the resume/cover filenames; see config.yaml)."
            )

    @property
    def operator_slug(self) -> str:
        """``full_name`` with whitespace collapsed to underscores."""
        return _WHITESPACE.sub("_", self.operator.full_name.strip())

    @property
    def resume_filename(self) -> str:
        """Derived resume upload filename, e.g. ``Jane_Doe_Resume.pdf``."""
        return f"{self.operator_slug}_Resume.pdf"

    @property
    def cover_filename(self) -> str:
        """Derived cover-letter filename, e.g. ``Jane_Doe_Cover_Letter.pdf``."""
        return f"{self.operator_slug}_Cover_Letter.pdf"


settings = Settings()
