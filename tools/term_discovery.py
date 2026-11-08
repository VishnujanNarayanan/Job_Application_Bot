"""Find skill terms the parser does not know, across many adverts (#177, #195 §1).

Reads a corpus of adverts (``tools.advert_corpus``: live India tech adverts)
and, for each advert, runs the same cleaning, sectioning and dictionary scan
as the dictionary parse. From the advert's requirement and duty lines only
(never company intro, benefits, EEO, pay), it collects **candidates**: words
that look like or are used as a skill but that no list the parser reads knows.

- Shape (looks like a name): CamelCase (PyTorch), letters with digits or
  symbols (S3, GPT-4, C++, Node.js, CI/CD), short acronyms (RAID, SIEM), and
  capitalised multi-word names mid-sentence (Google Ads, Group Policy).
- Context (used as a skill): an item of a list that also names a known skill
  ("Kafka, RabbitMQ and NATS"), the object of an ask phrase ("experience
  with", "knowledge of", "tools such as", "e.g."), a labelled line
  ("Tools: ..."), or brackets after a known skill.
- Not candidates: anything the skills registry has (any label), any term
  the dictionary scans for, the advert's own company, places, months, job
  titles, sentence-initial words with no other signal.

Candidates are ranked by how many adverts (and boards) use them, with their
signals, how often they sit in requirement vs duty lines, example lines,
and a suggested decision for the #195 review: a variant or part of a known
skill (join its group), or a new skill.

A second list, for #177: approved registry skills that appear in an advert's
requirement lines but that the dictionary scan did not return, by count
(misses with a known term: a reading problem, not a vocabulary one).

Then gap skills from the last CSV export (``data/index/matches.csv``),
split by registry label: known skills (real gaps, #196) vs terms the
registry has not reviewed (#195 review).

Offline: the learned vocabulary comes from the benchmark snapshot, the
database is never opened.

    python -m tools.term_discovery [--corpus data/corpus/adverts.jsonl] [--top 300]

Writes ``data/corpus/discovery.md`` and ``discovery.json``.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
CORPUS = _ROOT / "data" / "corpus" / "adverts.jsonl"
VOCAB = _ROOT / "data" / "benchmark" / "v1" / "vocabulary.json"
MATCHES_CSV = _ROOT / "data" / "index" / "matches.csv"

#: Section categories whose lines are read (``src.llm.sections``).
READ = {"req", "nice", "duty"}

_CAMEL = re.compile(r"\b[A-Z]?[a-z]+[A-Z][A-Za-z0-9]*\b|\b[A-Z]{2,}[a-z]+[A-Za-z]*\b")
_SYMBOL = re.compile(r"(?<![\w/])[A-Za-z][A-Za-z0-9]*(?:[+#]+|(?:[./-][A-Za-z0-9]+)+[+#]*)(?![\w/+#])"
                     r"|(?<![\w])[A-Za-z]+[0-9]+[A-Za-z0-9]*(?![\w])")
_ACRONYM = re.compile(r"(?<![\w/.-])[A-Z]{2,6}s?(?![\w/.-])")
_NAME = re.compile(r"(?<=[a-z,;:(] )(?:[A-Z][A-Za-z0-9.+#-]*)(?: (?:[A-Z][A-Za-z0-9.+#-]*|of|for|and|&)){1,3}"
                   r"(?<![ &])(?=[\s,.;:)]|$)")
_ASK = re.compile(r"\b(?:experience (?:with|in|using|of)|hands-on (?:with|in)|knowledge of|familiar(?:ity)? with"
                  r"|proficien(?:t|cy) (?:in|with)|expertise in|exposure to|skilled in|working knowledge of"
                  r"|tools? (?:like|such as)|technologies (?:like|such as)|such as|e\.g\.?|including|like)\s+",
                  re.I)
_LABEL = re.compile(r"^\W*(?:tools?|tech(?:nology)? stack|technologies|stack|languages?|frameworks?|platforms?"
                    r"|skills?|key skills|primary skills|secondary skills|mandatory skills)\s*[:\-–]\s*", re.I)
_SPLIT = re.compile(r"\s*(?:,|;|/(?=\s)|\band\b|\bor\b|\betc\.?|\||•)\s*", re.I)
_BRACKET = re.compile(r"\(([^()]{2,80})\)")
_SENT_START = re.compile(r"(?:^|[.!?:]\s+|^\W+)$")

_MONTHS = set("january february march april may june july august september october november december "
              "jan feb mar apr jun jul aug sep sept oct nov dec monday tuesday wednesday thursday friday".split())
_TITLE_WORDS = set("engineer engineers developer developers manager lead senior junior principal staff architect "
                   "analyst consultant intern internship associate director head specialist scientist "
                   "administrator executive officer team teams role position candidate company client clients "
                   "india remote hybrid onsite office bachelor bachelors master masters degree years year "
                   "the a an we you our your this that with and or for of to in on at by as is are be "
                   "skills skill qualifications qualification requirements requirement responsibilities "
                   "responsibility preferred required desired desirable key additional information "
                   "experience knowledge ability abilities must nice good have plus bonus summary overview "
                   "duties description job about who what why how benefits perks mandatory primary secondary "
                   "basic minimum essential core".split())
_HEADINGS = set("responsibilities requirements qualifications information".split())
_VERBS = set("develop implement analyze analyse apply build design manage ensure support drive lead work create "
             "maintain understand provide collaborate partner deliver define own write review test perform "
             "conduct identify coordinate execute monitor improve establish prepare participate assist help "
             "use utilize leverage enable translate communicate document track report deploy operate "
             "configure install troubleshoot resolve mentor guide oversee plan evaluate assess research "
             "optimize optimise scale automate integrate migrate demonstrate possess hold bring "
             "coach equivalent strong excellent proven solid deep good basic advanced".split())
#: A first word that makes the candidate the rest of an ask phrase.
_LEADS = set("experience experienced knowledge ability proficiency understanding familiarity expertise exposure "
             "background bachelor bachelors bachelor’s bachelor's master masters master’s master's degree diploma "
             "graduate graduation equal".split())
#: A last word that makes it a job title, a team or a person.
_PEOPLE = set("engineer engineers owner owners manager managers analyst analysts architect architects leader leaders "
              "lead leads specialist specialists consultant consultants developer developers designer designers "
              "scientist scientists administrator administrators officer officers director directors executive "
              "executives representative representatives associate associates partner partners stakeholders "
              "team teams member members candidate candidates".split())
#: A last part that makes a hyphenated word an adjective ("AI-assisted", "API-first").
_ADJ_TAIL = re.compile(r"-(?:assisted|enabled|generated|native|first|based|powered|driven|led|focused|oriented|"
                       r"centric|ready|facing|level|grade|scale|time|end|functional|related|specific|wide|class)$", re.I)
#: Fields of work and study: the registry's ``domain`` label, not a skill.
_FIELD = re.compile(r"\b(?:engineering|management|administration|resources|science|sciences|studies|physics|"
                    r"chemistry|mathematics|economics|finance|accounting|commerce|marketing|operations)$", re.I)
_MIDDLE = set("to with from using for into across within through via by on at as per".split())
_PLACES = re.compile(r"\b(?:u\.s|eu|gcc|ist|est|pst|gmt|utc|apj|latam|india|bengaluru|bangalore|hyderabad|pune|chennai|mumbai|delhi|noida|gurgaon|gurugram"
                     r"|kolkata|ahmedabad|kochi|coimbatore|usa|us|uk|europe|emea|apac|asia|america|singapore"
                     r"|london|germany|canada|australia|dubai|uae)\b", re.I)
_JUNK = re.compile(r"^(?:[A-Z]{2,4}-\d{2,4}|\d+\+?|[ivx]+|e\.g|i\.e|etc|n/a|tbd|ok|faq|eod|asap|fyi|wfh|eeo|ctc"
                   r"|ai|ml|it|hr|pr|qa|ux|ui|ceo|cto|cfo|coo|vp|svp|ph\.?d|b\.?tech|m\.?tech|b\.?e|m\.?s|b\.?s"
                   r"|mba|mca|bca|bsc|msc|ug|pg)$", re.I)


def _setup_offline() -> None:
    """Learned vocabulary from the benchmark snapshot; no database."""
    from src import parser
    from src.scorer import keywords

    try:
        vocab = json.loads(VOCAB.read_text())
        terms = tuple(sorted(((v["term"], v["term_key"]) for v in vocab if v["is_active"]),
                             key=lambda p: len(p[1]), reverse=True))
    except (OSError, ValueError, KeyError):
        terms = ()
    parser._vocabulary = lambda: terms
    for fn in (parser._pool_terms, keywords._known_names, keywords._usual_spelling):
        fn.cache_clear()


_CONNECTOR = re.compile(r"(?:\s+(?:and|or|of|for|with|to|in|on|the|a|an|&|using|via))+$", re.I)


def _clean(term: str) -> str:
    t = re.sub(r"\s+", " ", term)
    # An unclosed bracket: keep the part before it ("Adobe Experience Manager (AEM").
    if t.count("(") != t.count(")"):
        t = t.split("(")[0] if t.count("(") > t.count(")") else t.split(")")[-1]
    t = t.strip(" \t*`'\"“”‘’.,;:!?-–—()[]{}")
    t = _CONNECTOR.sub("", t)
    return t.strip(" .,;:-")


def _plausible(t: str) -> bool:
    if not (2 <= len(t) <= 40) or len(t.split()) > 4:
        return False
    if _JUNK.match(t) or not re.search(r"[A-Za-z]", t):
        return False
    # "hands-on", "End-to-end": ordinary hyphenated words, not names.
    if re.fullmatch(r"[A-Za-z][a-z]*(?:-[a-z]+)+", t):
        return False
    words = [w.casefold() for w in re.split(r"[\s/]+", t)]
    # A clause, not a name: a sentence break, a heading, or a list item
    # that opens with a verb ("Develop", "Analyze test results").
    if ". " in t or ":" in t or any(w in _HEADINGS for w in words):
        return False
    if words[0] in _VERBS or (words[0].endswith("ing") and words[0][:-3] in _VERBS):
        return False
    if words[0] in _LEADS or words[-1] in _PEOPLE or _ADJ_TAIL.search(t):
        return False
    # "his/her", "internal/external": two ordinary words.
    if re.fullmatch(r"[A-Za-z][a-z]+/[a-z]+", t):
        return False
    # Two-letter abbreviations (PM, ID, AM, IP) are mostly not skills; a real
    # one (Go, R, C#) is in the registry already.
    if re.fullmatch(r"[A-Z]{2}", t):
        return False
    # Ordinary words joined by a preposition ("tools with coaching",
    # "maturity to stakeholders"): a phrase, not a name.
    if len(words) >= 3 and any(w in _MIDDLE for w in words[1:-1]):
        return False
    if all(w in _TITLE_WORDS or w in _MONTHS for w in words):
        return False
    if _PLACES.search(t) and len(words) <= 2:
        return False
    return True


def _known_checker():
    """A function telling whether a term is known to any list the parser
    reads (registry any label, dictionary terms), by exact and folded key."""
    from src.parsing.dictionary import Dictionary
    from src.scorer.keywords import phrase_key
    from src.state import registry

    reg = registry.load()
    dictionary = Dictionary.load()
    keys = {phrase_key(t) for t, _k in dictionary.terms()} | {k for _t, k in dictionary.terms()}
    keys |= {phrase_key(e.term) for e in reg.exact.values()}
    keys.discard("")

    def known(term: str) -> bool:
        return reg.entry(term) is not None or phrase_key(term) in keys

    return known, dictionary, reg


def _lines(text: str) -> list[tuple[str, str]]:
    """(category, line) for the requirement / nice / duty lines; the whole
    advert's lines (as "all") when it has no recognisable sections."""
    from src.llm.sections import split_sections

    preamble, secs = split_sections(text)
    if not any(c in READ for c, _b in secs):
        return [("all", ln) for ln in text.splitlines() if ln.strip()]
    out = []
    for c, body in secs:
        if c in READ:
            out += [(c, ln) for ln in body.splitlines()[1:] if ln.strip()]
    return out


def candidates_in_line(line: str, known) -> dict[str, set[str]]:
    """{candidate: signals} in one advert line."""
    found: dict[str, set[str]] = defaultdict(set)
    bare = re.sub(r"[*_`]+", "", line)
    bare = re.sub(r"\s\d{1,2}[.)]\s+", ", ", bare)

    def add(raw: str, signal: str) -> None:
        t = _clean(raw)
        if t and _plausible(t):
            found[t].add(signal)

    for rx, sig in ((_CAMEL, "camel"), (_SYMBOL, "symbol"), (_ACRONYM, "acronym")):
        for m in rx.finditer(bare):
            add(m.group(0), sig)
    for m in _NAME.finditer(bare):
        if not _SENT_START.search(bare[:m.start()]):
            add(m.group(0), "name")

    # Context: lists, ask phrases, labelled lines, brackets.
    chunks = []
    lab = _LABEL.match(bare)
    if lab:
        chunks.append((bare[lab.end():], "label"))
    for m in _ASK.finditer(bare):
        rest = re.split(r"[.;]\s|\s(?:to|in order|so that|for|which|that|who|where|across)\s", bare[m.end():], 1)[0]
        chunks.append((rest, "ask"))
    for m in _BRACKET.finditer(bare):
        if known(_clean(bare[:m.start()].split(",")[-1].split(" ")[-1] or "")) or "," in m.group(1):
            chunks.append((m.group(1), "bracket"))
    if bare.count(",") >= 2:
        chunks.append((bare, "list"))
    for chunk, sig in chunks:
        items = [_clean(x) for x in _SPLIT.split(chunk)]
        items = [x for x in items if x]
        if sig == "list" and not any(known(x) for x in items):
            continue
        for x in items:
            # A list item is a name, not a clause: at most 4 words, and not
            # an ordinary lower-case phrase unless it sits in a labelled list.
            if len(x.split()) > 4:
                continue
            if sig in ("list", "ask") and x[:1].islower() and not re.search(r"[A-Z0-9+#./]", x):
                continue
            add(x, sig)
    return found


def _ngram_keys(line: str, phrase_key, n: int = 5) -> set[str]:
    """``phrase_key`` of every run of 1..n words in the line."""
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9+#./-]*", line)
    out = set()
    for i in range(len(words)):
        for j in range(i + 1, min(i + n, len(words)) + 1):
            out.add(phrase_key(" ".join(words[i:j])))
    out.discard("")
    return out


def suggest(term: str, reg) -> str:
    """A starting point for the #195 review, never a decision."""
    from src.state.registry import APPROVE

    approved = [e.term for e in reg.exact.values() if e.label == APPROVE and len(e.term) >= 2]
    low = term.casefold()
    if re.match(r"(certifi|certified|license|licence)", low):
        return "credential?"
    if _FIELD.search(low):
        return "domain (field of work)?"
    for e in sorted(approved, key=len, reverse=True):
        el = e.casefold()
        if len(el) >= 2 and re.search(rf"(?<![a-z0-9]){re.escape(el)}(?![a-z0-9])", low) and el != low:
            return f"part of / variant of {e}?"
    close = difflib.get_close_matches(low, [a.casefold() for a in approved], n=1, cutoff=0.88)
    if close:
        match = next(a for a in approved if a.casefold() == close[0])
        return f"spelling of {match}?"
    return "new skill?"


def run(corpus: Path, top: int) -> dict:
    from src.parsing.dictionary import scan_skills, skill_text
    from src.llm.sections import split_sections
    from src.scorer.keywords import clean_ad_text, phrase_key
    from src.state.registry import APPROVE

    known, dictionary, reg = _known_checker()
    approved = {phrase_key(e.term): e.term for e in reg.exact.values() if e.label == APPROVE and not e.means}
    # split("\n"), not splitlines(): advert text may hold U+2028, which
    # splitlines() would break a JSON line on.
    rows = [json.loads(l) for l in corpus.read_text(encoding="utf-8").split("\n") if l.strip()]

    adverts, boards = defaultdict(set), defaultdict(set)
    signals, cats, examples = defaultdict(Counter), defaultdict(Counter), defaultdict(list)
    dropped_g = defaultdict(int)
    unread, unread_ex = Counter(), defaultdict(list)
    spellings = defaultdict(Counter)
    from src.llm.prompts import clip_jd_text
    import statistics as st
    per_src: dict[str, dict] = defaultdict(lambda: {"n": 0, "chars": [], "req": 0, "skills": [], "none": 0})
    clip = {"long": 0, "lost_any": 0, "lost": Counter()}
    for r in rows:
        text = clean_ad_text(r.get("description") or "")
        if not text:
            continue
        company = (r.get("company") or r.get("board") or "").casefold()
        sections = split_sections(text)
        found_names = scan_skills(text, dictionary, skill_text(text, sections))
        found = {phrase_key(s) for s in found_names}
        src = per_src[r["ats"]]
        src["n"] += 1
        src["chars"].append(len(text))
        src["req"] += any(c in ("req", "nice") for c, _b in sections[1])
        src["skills"].append(len(found))
        src["none"] += not found
        # Section clipping (#177 item 5): skills only in what the LLM prompt cuts.
        clipped = clip_jd_text(r.get("description") or "")
        if clipped != (r.get("description") or ""):
            clip["long"] += 1
            ct = clean_ad_text(clipped)
            kept_keys = {phrase_key(x) for x in scan_skills(ct, dictionary, skill_text(ct, split_sections(ct)))}
            lost = [n for n in found_names if phrase_key(n) not in kept_keys]
            clip["lost_any"] += bool(lost)
            clip["lost"].update(lost)
        seen_here = set()
        for cat, line in _lines(text):
            for term, sigs in candidates_in_line(line, known).items():
                if known(term) or (company and term.casefold() in company):
                    continue
                parts = [x for x in re.split(r"\s+(?:and|&|or)\s+|/|,\s*", term) if x]
                if len(parts) >= 2 and all(known(x) for x in parts):
                    continue
                key = term.casefold()
                # CAPA / CAPAs, API / APIs: one candidate.
                if re.fullmatch(r"[A-Z]{2,6}s", term):
                    key = key[:-1]
                seen_here.add(key)
                spellings[key][term] += 1
                adverts[key].add(r["job_id"])
                boards[key].add((r["ats"], r["board"]))
                signals[key].update(sigs)
                cats[key][cat] += 1
                if len(examples[key]) < 2 and len(line) < 300:
                    examples[key].append(line.strip())
                if r.get("stage_g"):
                    dropped_g[key] += 1
            # Known approved skills on a requirement line that the scan missed.
            if cat in ("req", "nice", "all"):
                for k in _ngram_keys(line, phrase_key) & approved.keys():
                    if len(k) > 2 and k not in found:
                        name = approved[k]
                        unread[name] += 1
                        if len(unread_ex[name]) < 2 and len(line) < 300:
                            unread_ex[name].append(line.strip())
        del seen_here

    # A single plain word that the corpus writes in lower case more often
    # than capitalised is an ordinary word ("similar", "Technology").
    lower = Counter()
    for r in rows:
        lower.update(w for w in re.findall(r"(?<![A-Za-z])[a-z]{3,}(?![A-Za-z])", r.get("description") or ""))
    for k in list(adverts):
        if re.fullmatch(r"[a-z]{3,}", k) and lower[k] >= len(adverts[k]):
            del adverts[k]
    ranked = sorted(adverts, key=lambda k: (-len(boards[k]), -len(adverts[k]), k))
    # The report lists terms seen on 2+ boards; the long tail is in the JSON.
    ranked = [k for k in ranked if len(boards[k]) >= 2] + [k for k in ranked if len(boards[k]) < 2]
    cands = []
    for k in ranked[:top]:
        cands.append({
            "term": spellings[k].most_common(1)[0][0], "adverts": len(adverts[k]), "boards": len(boards[k]),
            "signals": dict(signals[k]), "where": dict(cats[k]),
            "in_stage_g_dropped": dropped_g[k], "suggest": suggest(k, reg), "examples": examples[k],
        })
    sources = {a: {"adverts": v["n"], "median_chars": int(st.median(v["chars"])),
                   "with_requirements_section": round(v["req"] / v["n"], 2),
                   "median_skills_found": st.median(v["skills"]), "no_skill_found": v["none"]}
               for a, v in sorted(per_src.items(), key=lambda kv: -kv[1]["n"]) if v["n"]}
    clipping = {"long_adverts": clip["long"], "lost_any_skill": clip["lost_any"],
                "skills_lost": sum(clip["lost"].values()), "most_lost": clip["lost"].most_common(30)}
    return {"sources": sources, "clipping": clipping, "adverts": len(rows), "stage_g_dropped": sum(1 for r in rows if r.get("stage_g")),
            "candidates_total": len(adverts), "candidates": cands,
            "unread_known": [{"term": t, "lines": n, "examples": unread_ex[t]} for t, n in unread.most_common(80)]}


def gap_skills(path: Path = MATCHES_CSV) -> dict:
    """Gap skills of the last CSV export, by registry label."""
    from src.state import registry

    reg = registry.load()
    if not path.exists():
        return {}
    counts = Counter()
    for row in csv.DictReader(path.open()):
        for s in (row.get("Gap Skills") or "").split(","):
            if s.strip():
                counts[s.strip()] += 1
    by_label: dict[str, list] = defaultdict(list)
    for term, n in counts.most_common():
        by_label[reg.label(term)].append((term, n))
    return {"matches": sum(1 for _ in csv.DictReader(path.open())), "distinct": len(counts),
            "by_label": {k: v for k, v in by_label.items()}}


def render(d: dict, gaps: dict) -> str:
    out = [f"# Term discovery ({d['adverts']} adverts)", "",
           f"- Candidates not known to any parser list: **{d['candidates_total']}** distinct "
           f"(top {len(d['candidates'])} below, by boards then adverts).",
           f"- Adverts stage G would drop (unclear title, too few known skills): {d['stage_g_dropped']}.", "",
           "## Unknown terms", "",
           "| Term | Boards | Adverts | Where | Signals | Suggest | Example |",
           "|---|---|---|---|---|---|---|"]
    for c in d["candidates"]:
        where = ", ".join(f"{k} {v}" for k, v in c["where"].items())
        sig = ", ".join(sorted(c["signals"]))
        ex = (c["examples"][0][:140].replace("|", "/") if c["examples"] else "")
        out.append(f"| {c['term']} | {c['boards']} | {c['adverts']} | {where} | {sig} | {c['suggest']} | {ex} |")
    out += ["", "## Per source (#177 item 6)", "",
            "| ATS | Adverts | Median chars | Has requirements section | Median skills found | No skill found |",
            "|---|---|---|---|---|---|"]
    for a, v in d["sources"].items():
        out.append(f"| {a} | {v['adverts']} | {v['median_chars']} | {v['with_requirements_section']:.0%} | "
                   f"{v['median_skills_found']} | {v['no_skill_found']} |")
    c = d["clipping"]
    out += ["", "## Section clipping for the LLM prompt (#177 item 5)", "",
            f"{c['long_adverts']} adverts are long enough to be clipped; {c['lost_any_skill']} of them lose at least "
            f"one skill the full text names ({c['skills_lost']} skill mentions in all). Most lost: "
            + ", ".join(f"{t} ({n})" for t, n in c["most_lost"][:25])]
    out += ["", "## Known skills on requirement lines that the scan did not return (#177)", "",
            "| Skill | Lines | Example |", "|---|---|---|"]
    for u in d["unread_known"]:
        out.append(f"| {u['term']} | {u['lines']} | {(u['examples'][0][:140] if u['examples'] else '').replace('|', '/')} |")
    if gaps:
        out += ["", f"## Gap skills in the last export ({gaps['matches']} matches, {gaps['distinct']} distinct)", ""]
        for label, items in sorted(gaps["by_label"].items(), key=lambda kv: -sum(n for _t, n in kv[1])):
            total = sum(n for _t, n in items)
            out.append(f"**{label}**: {len(items)} terms, {total} mentions. Top: "
                       + ", ".join(f"{t} ({n})" for t, n in items[:40]))
            out.append("")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--corpus", type=Path, default=CORPUS)
    ap.add_argument("--top", type=int, default=300)
    args = ap.parse_args(argv)
    _setup_offline()
    d = run(args.corpus, args.top)
    gaps = gap_skills()
    out_dir = args.corpus.parent
    (out_dir / "discovery.json").write_text(json.dumps({"discovery": d, "gaps": gaps}, indent=1, ensure_ascii=False))
    (out_dir / "discovery.md").write_text(render(d, gaps))
    print(f"{d['adverts']} adverts, {d['candidates_total']} unknown candidates; "
          f"report: {out_dir / 'discovery.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
