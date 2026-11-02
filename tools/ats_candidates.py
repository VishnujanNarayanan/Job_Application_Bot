"""Candidate ATS board ids from company lists, for tools.ats_measure (#162).

Each company becomes a few likely board ids (its slug, website domain and
squashed name) on every ATS whose board id is a company slug. Likeliest
companies first (India-tagged, then hiring, then the rest), so a partial
measuring run covers the ones that matter. Writes data/ats_probe/extra_ids.jsonl
(appends; a rerun adds only new pairs). Nothing touches the network or the
database.

USAGE
-----
    python -m tools.ats_candidates --yc path/to/yc_all.json
    python -m tools.ats_candidates --names path/to/names.txt   # one company per line
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

OUT = Path("data/ats_probe")
EXTRA = OUT / "extra_ids.jsonl"
# ATSes whose board id is a company slug. Workday (tenant + shard + site) and
# Eightfold (needs the web domain) cannot be guessed this way.
SLUG_ATSES = ("greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee", "rippling", "bamboohr")
_SUBDOMAIN = {"recruitee", "bamboohr"}
_LEGAL = re.compile(r"\b(inc|llc|ltd|limited|pvt|private|corp|corporation|co|gmbh|labs?|technologies|technology|ai|hq)\b", re.I)


def variants(name: str, slug: str | None = None, website: str | None = None) -> list[str]:
    out: list[str] = []

    def add(v: str | None) -> None:
        v = (v or "").strip().lower()
        if v and 2 <= len(v) <= 60 and v not in out:
            out.append(v)

    add(slug)
    if website:
        host = (urlsplit(website if "//" in website else f"//{website}").hostname or "").lower()
        host = re.sub(r"^(www|app|get|try|join|careers|jobs)\.", "", host)
        add(host.split(".")[0])
    add(re.sub(r"[^a-z0-9]", "", name.lower()))
    add(re.sub(r"[^a-z0-9]", "", _LEGAL.sub("", name.lower())))
    add(re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"))
    return out


def pairs(ids: list[str]) -> list[tuple[str, str]]:
    out = []
    for ats in SLUG_ATSES:
        for i in ids:
            if ats in _SUBDOMAIN and not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", i):
                continue
            out.append((ats, i))
    return out


def from_yc(path: Path) -> list[tuple[str, list[str]]]:
    data = json.loads(path.read_text())

    def rank(c: dict) -> tuple:
        india = "India" in (c.get("regions") or []) or "India" in (c.get("all_locations") or "")
        return (not india, not c.get("isHiring"), c.get("status") != "Active", -(c.get("team_size") or 0))

    out = []
    for c in sorted(data, key=rank):
        if c.get("status") in ("Inactive", "Acquired") and not c.get("isHiring"):
            continue
        out.append((c["name"], variants(c["name"], c.get("slug"), c.get("website"))))
    return out


def from_names(path: Path) -> list[tuple[str, list[str]]]:
    names = [n.strip() for n in path.read_text().splitlines() if n.strip() and not n.startswith("#")]
    return [(n, variants(n)) for n in names]


def write(companies: list[tuple[str, list[str]]], source: str) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    seen = set()
    if EXTRA.exists():
        seen = {(r["ats"], r["id"]) for r in map(json.loads, EXTRA.read_text().splitlines())}
    n = 0
    with EXTRA.open("a") as f:
        for name, ids in companies:
            for ats, i in pairs(ids):
                if (ats, i) not in seen:
                    seen.add((ats, i))
                    n += 1
                    f.write(json.dumps({"ats": ats, "id": i, "company": name, "source": source}) + "\n")
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yc", type=Path)
    ap.add_argument("--names", type=Path)
    a = ap.parse_args()
    if a.yc:
        print("yc pairs added:", write(from_yc(a.yc), "yc"))
    if a.names:
        print("name pairs added:", write(from_names(a.names), "names"))


if __name__ == "__main__":
    main()
