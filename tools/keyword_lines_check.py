"""#138: list keywords an entry proves that its keyword_lines miss.

    python -m tools.keyword_lines_check [entry_id ...]

Exits 1 when anything is MISSING. The same check gates
``python -m src.cli.reparse`` and ``python -m src.cli.assets push``; see
``src/state/keyword_lines.py`` for what is checked and how to fix a gap.
"""

from __future__ import annotations

import sys
from pathlib import Path

import structlog

_ROOT = Path(__file__).resolve().parent.parent


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(40))
    from src.state import keyword_lines
    from src.state.master_profile import MasterProfile

    import yaml

    raw = yaml.safe_load((_ROOT / "master_profile.yaml").read_text())
    report = keyword_lines.check(MasterProfile.model_validate(raw), keyword_lines.skill_terms(raw),
                                 set(argv) or None)
    print(report.render())
    return 1 if report.missing else 0


if __name__ == "__main__":
    sys.exit(main())
