"""Score hand-read skill labels against the parser and discovery (#177).

``data/corpus/gold_sample.yaml`` lists, for adverts of
``data/corpus/sample100.jsonl``, every skill a careful reader finds. This
measures, on those adverts:

- **parser recall**: share of the hand-read skills the dictionary parse
  returns (offline, learned vocabulary from the benchmark snapshot), all and
  required only, by type;
- **discovery recall**: of the skills the parser misses, the share
  ``tools.term_discovery`` would surface as a candidate from the same advert,
  which says how far its list can be trusted;
- the missed skills themselves, with whether discovery saw each.

A gold skill counts as found when the parse returns it, a spelling of it, or a
term the registry maps to it (``means``), compared by ``phrase_key``.

    python -m tools.gold_sample_score
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
GOLD = _ROOT / "data" / "corpus" / "gold_sample.yaml"
SAMPLE = _ROOT / "data" / "corpus" / "sample100.jsonl"


def _keys(term: str, reg, phrase_key) -> set[str]:
    """Every key a term may be compared by: itself, the skills the registry
    maps it to, and every registry spelling of those."""
    out = {phrase_key(term)}
    for s in reg.skills(term):
        out.add(phrase_key(s))
        out |= {phrase_key(a) for a in reg.aliases.get(s.casefold(), ())}
    # "Azure Event Grid" / "Event Grid": a leading vendor word is not a difference.
    for k in list(out):
        head, _, rest = k.partition(" ")
        if head in _VENDORS and rest:
            out.add(rest)
    out.discard("")
    return out


_VENDORS = {"azure", "aws", "amazon", "google", "gcp", "microsoft", "apache", "sap", "oracle", "ibm"}


def main() -> int:
    from tools import term_discovery as td

    td._setup_offline()
    from src.parsing.dictionary import DictionaryParser
    from src.scorer.keywords import clean_ad_text, phrase_key
    from src.state.models import AllJobs

    known, _dictionary, reg = td._known_checker()
    gold = (yaml.safe_load(GOLD.read_text()) or {}).get("jobs") or {}
    rows = {r["job_id"]: r for r in (json.loads(l) for l in SAMPLE.read_text(encoding="utf-8").split("\n") if l.strip())}
    parser = DictionaryParser()

    tot, hit, by_type, by_type_hit = 0, 0, Counter(), Counter()
    req_tot = req_hit = 0
    missed_all, disc_seen, reg_known = Counter(), Counter(), Counter()
    missed_tools = Counter()
    extra = Counter()
    for job_id, g in gold.items():
        r = rows.get(job_id)
        if r is None:
            continue
        job = AllJobs(job_id=job_id, role=r["title"], jd_text=r["description"], company=r.get("company") or "")
        parsed = parser.parse(job)
        got = [*parsed.required_skills, *parsed.nice_to_have]
        got_keys = set().union(*(_keys(x, reg, phrase_key) for x in got)) if got else set()
        text = clean_ad_text(r["description"])
        cands = set()
        for _c, line in td._lines(text):
            cands |= {c.casefold() for c in td.candidates_in_line(line, known)}
        gold_keys = set()
        for s in g.get("skills") or []:
            if s.get("type") in ("domain", "credential"):
                continue
            ks = _keys(s["name"], reg, phrase_key)
            gold_keys |= ks
            tot += 1
            by_type[s["type"]] += 1
            ok = bool(ks & got_keys)
            hit += ok
            by_type_hit[s["type"]] += ok
            if s.get("req"):
                req_tot += 1
                req_hit += ok
            if not ok:
                missed_all[s["name"]] += 1
                if s["type"] == "tool":
                    missed_tools[s["name"]] += 1
                if known(s["name"]):
                    reg_known[s["name"]] += 1
                elif s["name"].casefold() in cands:
                    disc_seen[s["name"]] += 1
        for x in got:
            if not (_keys(x, reg, phrase_key) & gold_keys):
                extra[x] += 1

    jobs = sum(1 for j in gold if j in rows)
    missed = sum(missed_all.values())
    print(f"{jobs} adverts, {tot} hand-read skills (domains and credentials apart)")
    print(f"parser recall: {hit}/{tot} = {hit / max(tot, 1):.0%}; required only {req_hit}/{req_tot} = "
          f"{req_hit / max(req_tot, 1):.0%}")
    print(f"TOOL recall (headline): {by_type_hit['tool']}/{by_type['tool']} = "
          f"{by_type_hit['tool'] / max(by_type['tool'], 1):.0%}")
    print("missed tools [registry label or discovery]: " + ", ".join(
        f"{t} [{reg.label(t) if known(t) else ('disc' if t in disc_seen else 'blind')}]" + (f" x{n}" if n > 1 else "")
        for t, n in missed_tools.most_common()))
    print("by type: " + ", ".join(f"{t} {by_type_hit[t]}/{n}" for t, n in by_type.most_common()))
    known_n, disc_n = sum(reg_known.values()), sum(disc_seen.values())
    blind = Counter({t: n for t, n in missed_all.items() if t not in reg_known and t not in disc_seen})
    print(f"missed: {missed} = known to the parser's lists but not read {known_n} ({known_n / max(missed, 1):.0%}), "
          f"unknown but discovery sees it {disc_n} ({disc_n / max(missed, 1):.0%}), "
          f"invisible to both {sum(blind.values())} ({sum(blind.values()) / max(missed, 1):.0%})")
    print("\nknown but not read, by registry label: " + ", ".join(
        f"{lab} {n}" for lab, n in Counter(reg.label(t) for t in reg_known.elements()).most_common()))
    for title, c in (("known but not read", Counter({f"{t} [{reg.label(t)}]": n for t, n in reg_known.items()})), ("unknown, discovery sees it", disc_seen),
                     ("invisible to both", blind)):
        print(f"\n{title}: " + ", ".join(f"{t} ({n})" if n > 1 else t for t, n in c.most_common()))
    print(f"\nparsed but not in the hand list ({sum(extra.values())}):")
    print(", ".join(f"{t} ({n})" for t, n in extra.most_common(60)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
