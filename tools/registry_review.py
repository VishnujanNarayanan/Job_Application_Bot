"""#188: the weekly review of new skill terms.

New terms reach the vocabulary from the LLM's own output (``all_jobs.
llm_skills_raw``). Until the registry labels one, it is *pending*
(``scoring.registry.pending`` decides whether it counts meanwhile). Once a
week:

    python -m tools.registry_review pending --out data/registry/pending.json
        # every term recent adverts used that the registry has no label for
        # (vocabulary, the LLM's own skills, match checklists), with advert
        # counts, example lines and suggested groupings; also writes a
        # ready-to-edit review file (read-only on the database)
    python -m tools.registry_review lines <review file> decisions.json
    python -m tools.registry_review apply data/registry/decisions.json
        # merge reviewed labels into config/skills_registry.yaml (and the
        # gitignored local file for terms only in the operator's pool)

``decisions.json`` is a list of {"term", "label", "means"?, "note"?, "by"?}
(``means``: the skill(s) the term stands for, written by hand). A label
the operator set by hand (``by: operator``) is never overwritten unless
``--force`` is given. Labels and their meaning: see src/state/registry.py.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_ORDER = ["term", "label", "means", "note", "by"]


def _suggest(term: str, reg, approved: list[str]) -> list[str]:
    """Existing skills a new term probably belongs to, best first: the skills
    of approved terms it names (longest first), then terms that match it in
    either direction. A suggestion only: the weekly review decides."""
    from src.scorer.keywords import matches, norm

    low = f" {norm(term)} "
    named = [a for a in approved if len(a) > 1 and f" {norm(a)} " in low]
    named.sort(key=len, reverse=True)
    out: list[str] = []
    for a in named:
        for s in reg.skills(a):
            if s not in out:
                out.append(s)
    if not out:
        for a in approved:
            if matches(a, norm(term)) or matches(term, norm(a)):
                for s in reg.skills(a):
                    if s not in out:
                        out.append(s)
            if len(out) >= 3:
                break
    return out[:3]


def _contains(term: str, approved: list[str]) -> list[str]:
    """Approved skills named inside ``term`` as whole words ("Azure DevOps"
    names Azure): the swaps the matcher will make for it (#191)."""
    from src.scorer.keywords import norm

    low = f" {' '.join(norm(term).split())} "
    whole = " ".join(norm(term).split())
    return [a for a in approved if len(a) > 1 and " ".join(norm(a).split()) != whole
            and f" {' '.join(norm(a).split())} " in low][:5]


def _pending(args) -> int:
    """Every term recent adverts used that the registry has no label for.

    Sources: the learned vocabulary, the LLM's own skills of the last
    ``--days`` days (all_jobs.llm_skills_raw), and the checklists of the
    matches built in that time. Writes the list (with advert counts, example
    lines and suggested groupings) and a ready-to-edit review file in the
    ``term|code`` shorthand of ``lines``."""
    from collections import Counter

    from sqlalchemy import text

    from src.scorer.keywords import clean_ad_text, tokens_of
    from src.state import registry
    from src.state.db import session_scope

    reg = registry.load()
    counts: Counter = Counter()
    spelling: dict[str, str] = {}

    def add(t, n=1):
        t = str(t).strip()
        if 1 < len(t) <= 120 and reg.entry(t) is None:
            counts[t.casefold()] += n
            spelling.setdefault(t.casefold(), t)

    with session_scope() as s:
        for t, n in s.execute(text(
            "select term, job_count from skill_vocabulary where is_active and job_count >= :n"
        ), {"n": args.min_jobs}).all():
            add(t, n)
        has_raw = s.execute(text(
            "select 1 from information_schema.columns where table_name = 'all_jobs'"
            " and column_name = 'llm_skills_raw'")).first() is not None
        if not has_raw:
            print("all_jobs.llm_skills_raw not there yet (migration 0018); skipping that source")
        for (raw,) in (s.execute(text(
            "select llm_skills_raw from all_jobs where llm_skills_raw is not null"
            " and scraped_at > now() - make_interval(days => :d)"), {"d": args.days}).all() if has_raw else []):
            for t in set(tokens_of(raw or [])):
                add(t)
        for (kws,) in s.execute(text(
            "select selection_json->'jd_keywords' from applied"
            " where built_at > now() - make_interval(days => :d)"), {"d": args.days}).all():
            for t in set(kws or []):
                add(t)
        jds = s.execute(text(
            "select jd_text from all_jobs where jd_text is not null"
            " and scraped_at > now() - make_interval(days => :d) order by random() limit 3000"),
            {"d": max(args.days, 30)}).scalars().all()
    lines = [ln.strip(" *-•\t") for jd in jds for ln in clean_ad_text(jd).splitlines()
             if 25 < len(ln.strip()) < 300]
    random.Random(188).shuffle(lines)
    low = [ln.casefold() for ln in lines]
    approved = sorted((e.term for e in reg.exact.values() if e.label in (registry.APPROVE, registry.SENSE)),
                      key=len, reverse=True)
    out = []
    for key, n in counts.most_common():
        term = spelling[key]
        rx = re.compile(r"(?<![\w+#.-])" + re.escape(key) + r"(?![\w+#-]|\.\w)")
        ex = [ln for ln, lo in zip(lines, low) if key in lo and rx.search(lo)][:3]
        out.append({"term": term, "adverts": n, "suggest": _suggest(term, reg, approved),
                    "contains": _contains(term, approved), "examples": ex})
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    review = path.with_suffix(".review.txt")
    review.write_text(
        "# Weekly registry review. One line per new term: term|code. Codes: a (approve), a=X|Y (means\n"
        "# existing skills X, Y), ~note (sense), r reject, d domain, s soft, c credential. The suggestion\n"
        "# below is a guess from shared words: check every line, then\n"
        "#   python -m tools.registry_review lines <this file> decisions.json\n"
        "#   python -m tools.registry_review apply decisions.json\n"
        "# A '# contains' line lists skills named inside the term: each counts as proven by it\n"
        "# unless the term is added under that skill in config/keyword_never.yaml (#191). A term\n"
        "# that does the same job as other tools goes in config/comparable_tools.yaml.\n"
        + "".join((f"# contains: {', '.join(o['contains'])}\n" if o["contains"] else "")
                  + f"{o['term']}|{('a=' + '|'.join(o['suggest'])) if o['suggest'] else 'a'}\n" for o in out),
        encoding="utf-8")
    print(f"{len(out)} pending terms -> {path}; review file -> {review}")
    return 0


def _read(path: Path) -> tuple[str, dict]:
    if not path.exists():
        return "", {}
    txt = path.read_text(encoding="utf-8")
    header = "".join(ln for ln in txt.splitlines(True) if ln.startswith("#"))
    return header, (yaml.safe_load(txt) or {}).get("terms") or {}


def _write(path: Path, header: str, terms: dict) -> None:
    lines = ["terms:"]
    for k in sorted(terms):
        e = terms[k]
        body = yaml.safe_dump({f: e[f] for f in _ORDER if f in e}, default_flow_style=True,
                              width=10_000, allow_unicode=True).strip()
        lines.append(f"  {json.dumps(k, ensure_ascii=False)}: {body}")
    path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


def _pool_only(term: str) -> bool:
    """A term only in the operator's skills pool (personal), not a public term."""
    from src.parser import _pool_terms, _vocabulary

    key = term.casefold()
    in_pool = key in {t.casefold() for t in _pool_terms()}
    in_vocab = key in {k for _t, k in _vocabulary()}
    return in_pool and not in_vocab


def _apply(args) -> int:
    from src.state import registry

    decisions = json.loads(Path(args.decisions).read_text())
    files = {"public": registry.PUBLIC, "local": registry.LOCAL}
    loaded = {name: _read(p) for name, p in files.items()}
    changed = {"public": 0, "local": 0}
    skipped = []
    for d in decisions:
        term, label = str(d["term"]).strip(), str(d["label"]).strip()
        if label not in registry.LABELS:
            print(f"unknown label {label!r} for {term!r}; nothing written", file=sys.stderr)
            return 1
        key = term.casefold()
        name = "local" if _pool_only(term) else "public"
        terms = loaded[name][1]
        cur = terms.get(key)
        if cur and cur.get("by") == "operator" and d.get("by") != "operator" and not args.force:
            skipped.append(term)
            continue
        entry = {"term": term, "label": label}
        if d.get("means") and [str(m) for m in d["means"]] != [term]:
            entry["means"] = [str(m) for m in d["means"]]
        if d.get("note"):
            entry["note"] = d["note"]
        if d.get("by") == "operator":
            entry["by"] = "operator"
        if cur != entry:
            terms[key] = entry
            changed[name] += 1
    # Every skill a term is mapped to must itself be an entry.
    known = {k for _h, t in loaded.values() for k in t}
    for d in decisions:
        for m in d.get("means") or ():
            if str(m).casefold() not in known:
                loaded["public"][1][str(m).casefold()] = {"term": str(m), "label": "approve"}
                known.add(str(m).casefold())
                changed["public"] += 1
    for name, (header, terms) in loaded.items():
        if changed[name]:
            _write(files[name], header, terms)
    print(f"written: {changed['public']} public, {changed['local']} local;"
          f" kept the operator's label for {len(skipped)}: {skipped[:10]}")
    return 0


_CODES = {"a": "approve", "r": "reject", "d": "domain", "s": "soft", "c": "credential"}


def _lines(args) -> int:
    """Hand-review shorthand, one term per line: ``term|code``. Codes: a
    (approve), a=X|Y (approve, means skills X and Y), ~note (sense, with its
    note), r (reject), d (domain), s (soft), c (credential)."""
    out = []
    for raw in Path(args.src).read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.startswith("#"):
            continue
        term, code = raw.split("|", 1)
        code = code.strip()
        if code.startswith("~"):
            out.append({"term": term.strip(), "label": "sense", "note": code[1:].strip()})
        elif code.startswith("a="):
            out.append({"term": term.strip(), "label": "approve",
                        "means": [m.strip() for m in code[2:].split("|") if m.strip()]})
        elif code in _CODES:
            out.append({"term": term.strip(), "label": _CODES[code]})
        else:
            print(f"bad code {code!r} for {term!r}", file=sys.stderr)
            return 1
    Path(args.out).write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(f"{len(out)} decisions -> {args.out}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("lines", help="convert 'term|code' lines to decisions JSON")
    p.add_argument("src")
    p.add_argument("out")
    p = sub.add_parser("pending")
    p.add_argument("--out", default=str(_ROOT / "data" / "registry" / "pending.json"))
    p.add_argument("--min-jobs", type=int, default=5)
    p.add_argument("--days", type=int, default=7, help="recent adverts and matches to read")
    p = sub.add_parser("apply")
    p.add_argument("decisions")
    p.add_argument("--force", action="store_true", help="also overwrite labels the operator set")
    args = ap.parse_args(argv)
    return {"pending": _pending, "apply": _apply, "lines": _lines}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
