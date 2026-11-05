"""#191: print every keyword swap the matcher makes, for a by-hand check.

    python -m tools.swaps_report [--out data/registry/swaps.md]

One Markdown file, one section per kind of swap:

* same skill, both ways   -- keyword_families.yaml variants
* proves, one way         -- keyword_families.yaml members
* spellings of one skill  -- the registry's hand-written ``means``
* comparable (half credit) -- comparable_tools.yaml
* never                   -- keyword_never.yaml (a longer name that does not
                             prove the skill inside it)

A wrong line is fixed in the file it comes from; nothing is generated.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]


def build() -> str:
    from src.scorer.comparable import _groups
    from src.state import registry

    fam = yaml.safe_load((_ROOT / "config" / "keyword_families.yaml").read_text())["families"]
    never = (yaml.safe_load((_ROOT / "config" / "keyword_never.yaml").read_text()) or {}).get("never") or {}
    reg = registry.load()
    means: dict[str, list[str]] = defaultdict(list)
    for e in reg.exact.values():
        if e.label == registry.APPROVE and e.means and [m.casefold() for m in e.means] != [e.term.casefold()]:
            for m in e.means:
                means[m].append(e.term)

    out = ["# Keyword swaps (#191)", "",
           "Every substitution the matcher makes. Fix a wrong line in the file named in its section.", ""]
    out += ["## Same skill, both ways (`config/keyword_families.yaml`, variants)", ""]
    out += [f"- **{f['head']}** = {', '.join(f['variants'])}" for f in fam if f.get("variants")]
    out += ["", "## Proves, one way (`config/keyword_families.yaml`, members)", "",
            "A bullet naming the tool on the left of the arrow proves the skill on the right; never the reverse.", ""]
    out += [f"- {', '.join(f['members'])} → **{f['head']}**" for f in fam if f.get("members")]
    out += ["", "## Spellings of one skill (`config/skills_registry.yaml`, means)", ""]
    out += [f"- **{k}** ← {', '.join(sorted(set(v), key=str.casefold))}"
            for k, v in sorted(means.items(), key=lambda p: p[0].casefold())]
    out += ["", "## Comparable, half credit (`config/comparable_tools.yaml`)", "",
            "The advert's ask stays a gap; the comparable tool's bullet counts for half and is bolded.", ""]
    out += [f"- {name}: {', '.join(tools)}" for name, tools in _groups()]
    out += ["", "## Never (`config/keyword_never.yaml`)", "",
            "A longer name that does not prove the skill inside it.", ""]
    out += [f"- {', '.join(v)} ↛ **{k}**" for k, v in never.items()]
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(_ROOT / "data" / "registry" / "swaps.md"))
    args = ap.parse_args(argv)
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build(), encoding="utf-8")
    print(f"-> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
