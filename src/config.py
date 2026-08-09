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

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Load .env here, in the one module every entrypoint imports, so secrets are
# present no matter how the process was started. Previously only db.py,
# notifications.py and aws_check.py loaded it, which meant a CLI that touched
# none of them silently saw no keys at all — src.cli.llm_check, whose entire
# job is verifying keys, reported "NO KEY" for keys that were set.
#
# load_dotenv does not overwrite existing variables, so a real environment
# (GitHub Actions secrets) still wins over the file.
load_dotenv()


def resolve_endpoint_base_url(config_url: str) -> str:
    """Return the base URL that resume links should point at.

    Now a straight pass-through of ``endpoint.base_url``. It used to probe
    the local ngrok API (``localhost:4040``) for a live tunnel, because the
    free ngrok URL changed on every restart. That is gone with ngrok itself:
    the endpoint is reached over Tailscale, whose hostname is stable, so
    there is nothing to discover at runtime.

    Kept as a function rather than inlined because the caller in
    ``src/main.py`` reads better this way, and because the next hosting
    change (a real domain) lands here rather than in the orchestrator.
    """
    return config_url.rstrip("/")

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "config.yaml"

_ENV_REF = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _expand_env(value: Any) -> Any:
    """Substitute ``${VAR}`` in config strings from the environment.

    For settings that are machine-specific but not secret, and so belong in
    neither the committed YAML nor a hardcoded literal — the local Ollama
    endpoint is reached on WSL's default-route host, which is reassigned every
    boot.

    An unset variable is left as-is rather than blanked: a base_url of ""
    fails somewhere far away, whereas a literal "${OLLAMA_BASE_URL}" names
    exactly what is missing.
    """
    if isinstance(value, str):
        return _ENV_REF.sub(
            lambda m: os.environ.get(m.group(1), m.group(0)), value
        )
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value

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
        super().__init__(_expand_env(data))
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
