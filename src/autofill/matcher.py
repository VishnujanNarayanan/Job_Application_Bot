"""Match an application form's fields to the operator's answers (#154).

The userscript sends every field it found -- label, surrounding text, type,
options, required -- and gets back one decision per field:

    fill     the value is known; type it in
    review   filled, but a person should look (salary: units and ranges vary)
    resume   attach the tailored resume PDF
    skip     known and deliberately left empty (cover letter until #83)
    unknown  no answer: leave it empty and highlight it. Never guessed.

Labels are matched against config/ats_fields.yaml and the operator's own answers
in data/ats_answers.yaml. A field whose label says nothing ("Attach", "Type
here...") is matched on the text around it instead.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from src.autofill.facts import Facts

_FIELDS_PATH = Path(__file__).resolve().parents[2] / "config" / "ats_fields.yaml"

_STOP = frozenset(
    "a an and any are as at be by can do does for from have how i if in is it kindly me mention "
    "my of on or our please provide share so state tell that the this to us we what when where "
    "which will with would you your enter specify "
    # Survey filler: "What gender do you identify as?" asks what "Gender" asks.
    "identify identifies describe describes best select apply all choose one yourself following".split()
)
# Fields that are part of the page's machinery, not the application.
_MACHINERY = re.compile(r"captcha|recaptcha|honeypot|csrf|authenticity_token", re.I)
_CHOICE_TYPES = {"select", "radio", "checkbox_group", "combobox", "multiselect"}
_TEXT_TYPES = {"text", "email", "tel", "url", "number", "textarea", "search", "date", ""}
_OTHER_CURRENCY = re.compile(r"\b(usd|eur|gbp|aud|cad|sgd|aed|dollars?|euros?|pounds?)\b|[$€£]", re.I)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def norm(text: str | None) -> str:
    """Lower-case words only: "What is your Notice Period?*" -> "what is your notice period"."""
    t = (text or "").lower().replace("&", " and ")
    t = re.sub(r"[^a-z0-9+]+", " ", t)
    t = re.sub(r"\b(required|optional)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _content(text: str) -> frozenset[str]:
    return frozenset(w for w in norm(text).split() if w not in _STOP)


def _has_phrase(label: str, phrase: str) -> bool:
    """Whole-word phrase in a normalised label; a trailing ``*`` matches a word prefix."""
    p = norm(phrase.rstrip("*"))
    if not p:
        return False
    tail = r"\w*" if phrase.endswith("*") else r"\b"
    return re.search(rf"\b{re.escape(p)}{tail}", label) is not None


# ---------------------------------------------------------------------------
# The field table
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FieldSpec:
    key: str
    asks: tuple[str, ...]
    nots: tuple[str, ...]
    attrs: tuple[str, ...]


@lru_cache(maxsize=1)
def _table() -> tuple[tuple[FieldSpec, ...], frozenset[str], frozenset[str], frozenset[str]]:
    data = yaml.safe_load(_FIELDS_PATH.read_text(encoding="utf-8"))
    specs = tuple(
        FieldSpec(k, tuple(v.get("asks") or ()), tuple(v.get("not") or ()), tuple(v.get("attrs") or ()))
        for k, v in data["fields"].items()
    )
    generic = frozenset(norm(g) for g in data.get("generic_labels") or ())
    placeholders = frozenset(norm(p) for p in data.get("placeholder_options") or ())
    words = frozenset(norm(w) for w in data.get("generic_words") or ())
    return specs, generic, placeholders, words


def field_keys() -> frozenset[str]:
    return frozenset(s.key for s in _table()[0])


def is_generic(label: str) -> bool:
    n = norm(label)
    if not n or n in _table()[1] or len(n) < 2:
        return True
    return all(w in _table()[3] for w in n.split())


def _match_spec(label: str, extra: dict[str, list[str]]) -> tuple[str | None, int]:
    """(key, phrase length) of the longest phrase in ``label``; nots veto a key."""
    best: tuple[str | None, int] = (None, 0)
    for spec in _table()[0]:
        if any(_has_phrase(label, n) for n in spec.nots):
            continue
        for phrase in (*spec.asks, *extra.get(spec.key, ())):
            if _has_phrase(label, phrase) and len(norm(phrase)) > best[1]:
                best = (spec.key, len(norm(phrase)))
    return best


def _match_attrs(attrs: list[str]) -> str | None:
    for raw in attrs:
        # Attributes are identifiers: "firstName", "legalNameSection_firstName".
        a = norm(re.sub(r"([a-z])([A-Z])", r"\1 \2", raw or ""))
        if not a:
            continue
        for spec in _table()[0]:
            if any(a == norm(x) for x in spec.attrs):
                return spec.key
    return None


_AUTOCOMPLETE = {
    "name": "full_name", "given-name": "first_name", "family-name": "last_name", "email": "email",
    "tel": "phone", "tel-national": "phone", "tel-country-code": "phone_country_code",
    "address-level2": "city", "address-level1": "state", "country": "country", "country-name": "country",
    "organization": "current_company", "organization-title": "current_title", "url": "portfolio",
    "postal-code": "postal_code", "street-address": "address", "address-line1": "address_line1",
    "address-line2": "address_line2", "bday": "date_of_birth", "nickname": "preferred_name",
}


# ---------------------------------------------------------------------------
# Choosing an option
# ---------------------------------------------------------------------------

_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|to|–)\s*(\d+(?:\.\d+)?)")
_PLUS = re.compile(r"(\d+(?:\.\d+)?)\s*\+|(?:more than|over|above|at least|greater than)\s*(\d+(?:\.\d+)?)")
_UNDER = re.compile(r"(?:less than|under|below|up to|fewer than)\s*(\d+(?:\.\d+)?)")


def _numeric_option(options: list[str], value: float) -> str | None:
    for opt in options:
        low = opt.lower()
        if m := _RANGE.search(low):
            lo, hi = float(m.group(1)), float(m.group(2))
            if lo <= value <= hi:
                return opt
        elif m := _UNDER.search(low):
            if value < float(m.group(1)):
                return opt
        elif m := _PLUS.search(low):
            if value >= float(m.group(1) or m.group(2)):
                return opt
        elif re.fullmatch(r"\d+(?:\.\d+)?(?:\s*years?)?", low.strip()) and float(re.match(r"[\d.]+", low.strip()).group()) == value:
            return opt
    return None


_NEGATIVE = re.compile(r"\b(no|not|don t|dont|do not|does not|won t|will not|cannot|can t|never|without|none|"
                       r"decline|neither|unable)\b")


def _polarity_option(real: list[str], cand: str) -> str | None:
    """Yes / No against two sentences that say opposite things ("I will require
    sponsorship" / "I do not require sponsorship"): the one with the same polarity."""
    if cand not in ("yes", "no") or len(real) != 2:
        return None
    negative = [bool(_NEGATIVE.search(norm(o))) for o in real]
    if negative[0] == negative[1]:
        return None
    # Only two wordings of one statement, one negated ("Maybe later" / "Never" are not).
    words = [set(_NEGATIVE.sub(" ", norm(o)).split()) - _STOP for o in real]
    if not words[0] or not words[1] or len(words[0] & words[1]) / len(words[0] | words[1]) < 0.5:
        return None
    return real[negative.index(cand == "no")]


_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}
_QTY = re.compile(r"(\d+(?:\.\d+)?)\s*(day|week|month|year)s?\b")


def _as_days(text: str) -> float | None:
    """"15 days" -> 15, "2 weeks" -> 14, "Immediate" -> 0; None without a unit."""
    t = str(text).lower()
    if re.search(r"\bimmediate(ly)?\b|\bright away\b|\bnone\b|\bno notice\b|\bserving\b", t) and not _QTY.search(t):
        return 0.0
    m = _QTY.search(t)
    return float(m.group(1)) * _UNIT_DAYS[m.group(2)] if m else None


def _duration_option(real: list[str], days: float) -> str | None:
    """A duration against options in any unit: "15 days" picks "Less than 1 month"."""
    for opt in real:
        low = opt.lower()
        unit = (_QTY.search(low) or re.search(r"(day|week|month|year)", low))
        unit_days = _UNIT_DAYS[unit.group(2) if unit and unit.re is _QTY else unit.group(1)] if unit else None
        if unit_days is None:
            if re.search(r"\bimmediate", low) and days == 0:
                return opt
            continue
        if m := _RANGE.search(low):
            if float(m.group(1)) * unit_days <= days <= float(m.group(2)) * unit_days:
                return opt
        elif m := re.search(r"\b(?:within|up to|at most|max(?:imum)?)\s*(\d+(?:\.\d+)?)", low):
            if days <= float(m.group(1)) * unit_days:
                return opt
        elif m := _UNDER.search(low):
            if days < float(m.group(1)) * unit_days or (days == 0 and "immediate" in low):
                return opt
        elif m := re.search(r"(\d+(?:\.\d+)?)\s*\w*\s*(or less|or fewer|max|maximum)", low):
            if days <= float(m.group(1)) * unit_days:
                return opt
        elif m := _PLUS.search(low):
            if days >= float(m.group(1) or m.group(2)) * unit_days:
                return opt
        elif m := re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(day|week|month|year)s?\s*", low):
            if float(m.group(1)) * unit_days == days:
                return opt
    return None


def pick_option(options: list[str], candidates: list[Any]) -> str | None:
    """The form's own option for an answer, or None. Never the nearest guess."""
    placeholders = _table()[2]
    real = [o for o in options if norm(o) not in placeholders]
    for cand in candidates:
        if cand is None or cand == "":
            continue
        if isinstance(cand, bool):
            cand = "Yes" if cand else "No"
        c = norm(str(cand))
        if not c:
            continue
        for o in real:
            if norm(o) == c:
                return o
        for o in real:
            n = norm(o)
            if n.startswith(c + " ") or c.startswith(n + " "):
                return o
        if len(c) >= 3:
            for o in real:
                n = norm(o)
                if len(n) >= 3 and (_has_phrase(n, c) or _has_phrase(c, n)):
                    return o
        if isinstance(cand, (int, float)) or re.fullmatch(r"\d+(?:\.\d+)?", str(cand)):
            if hit := _numeric_option(real, float(cand)):
                return hit
        if (days := _as_days(str(cand))) is not None and (hit := _duration_option(real, days)):
            return hit
        if hit := _polarity_option(real, c):
            return hit
    return None


# ---------------------------------------------------------------------------
# Salary
# ---------------------------------------------------------------------------

def _num(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


def salary_text(lpa: float, label: str, ftype: str) -> tuple[str | None, str]:
    """The figure as this field wants it, and why: (None, reason) when unsure.

    Units follow the label: "(in LPA)" gets "6", "per month" a monthly rupee
    figure, "INR" annual rupees. A number box with no unit gets annual rupees; a
    text box gets "6 LPA", which reads right whatever the form meant.
    """
    raw = label or ""
    n = norm(raw)
    if _OTHER_CURRENCY.search(raw):
        return None, "asks in another currency"
    if re.search(r"\b(lpa|lakh|lakhs|lac|lacs|lakh per annum)\b", n):
        return _num(lpa), "in LPA, as the label asks"
    if re.search(r"\b(per month|monthly|month)\b", n):
        return str(round(lpa * 100000 / 12)), "monthly rupees, as the label asks"
    if re.search(r"\b(inr|rupees|rs)\b", n) or "₹" in raw:
        return str(round(lpa * 100000)), "annual rupees, as the label asks"
    if ftype == "number":
        return str(round(lpa * 100000)), "no unit given: annual rupees"
    return f"{_num(lpa)} LPA", "no unit given"


# ---------------------------------------------------------------------------
# Matching one form
# ---------------------------------------------------------------------------

def ats_of(url: str) -> str:
    """"lever/acme" for a Lever posting, "workday/3m" for a Workday tenant, else the host."""
    u = urlparse(url or "")
    host = (u.hostname or "").lower()
    first = next((p for p in u.path.split("/") if p), "")
    for marker, name in (("greenhouse.io", "greenhouse"), ("lever.co", "lever"), ("ashbyhq.com", "ashby"),
                         ("smartrecruiters.com", "smartrecruiters"), ("workable.com", "workable"),
                         ("rippling.com", "rippling")):
        if host.endswith(marker):
            if first in ("embed", "j", "oneclick-ui") or not first:
                return name
            return f"{name}/{first}"
    for marker, name in (("myworkdayjobs.com", "workday"), ("myworkdaysite.com", "workday"),
                         ("recruitee.com", "recruitee"), ("bamboohr.com", "bamboohr"),
                         ("eightfold.ai", "eightfold"), ("keka.com", "keka"), ("oraclecloud.com", "oracle")):
        if host.endswith(marker):
            return f"{name}/{host.split('.')[0]}"
    return host


def question_text(f: dict[str, Any]) -> str:
    """What the field asks, as a person would read it."""
    label = (f.get("label") or "").strip()
    if is_generic(label):
        label = (f.get("context") or "").strip()
    return re.sub(r"\s+", " ", label.replace("*", " ")).strip()[:300]


# "If yes, please give their name and job title": a follow-up about someone or
# something else. Never a profile fact; only the operator's own answers.
_FOLLOW_UP = re.compile(r"^(if (yes|so|applicable|any|other)|if you (have|are|answered|selected|chose|do)"
                        r"|if there|please (provide|specify|explain|give|share) (the )?details)\b")


def _key_for(f: dict[str, Any], extra: dict[str, list[str]]) -> str | None:
    if _FOLLOW_UP.match(norm(f.get("label") or "")):
        return None
    ac = (f.get("autocomplete") or "").strip().lower().split()
    if ac and ac[-1] in _AUTOCOMPLETE:
        return _AUTOCOMPLETE[ac[-1]]
    texts = [f.get("label") or ""]
    if is_generic(texts[0]):
        texts = [f.get("context") or ""]
    for t in texts:
        key, _ = _match_spec(norm(t), extra)
        if key:
            return key
    return _match_attrs([f.get("name") or "", f.get("id") or "", f.get("automation_id") or ""])


# Two questions this alike are the same question (answer filled as is) ...
SAME = 0.75
# ... and this alike, probably the same (answer filled, outlined for checking).
LIKELY = 0.7
# Options shared by unrelated questions: never evidence that two are the same.
_COMMON_OPTIONS = frozenset({"yes", "no", "other", "prefer not to say", "decline to answer",
                             "i don t wish to answer", "i do not wish to answer", "n a", "none"})


def _option_set(options: Any) -> frozenset[str]:
    return frozenset(n for n in (norm(str(o)) for o in options or []) if n)


def question_score(question: str, ask: str, options: Any = (), ask_options: Any = ()) -> float:
    """How surely a stored question ``ask`` is the same as ``question``.

    2.0 same words; >= SAME same content words, or a multi-word ``ask`` inside
    the question, or the same list of (non-yes/no) options with a shared word;
    LIKELY when one question's content words contain the other's ("Gender" vs
    "Gender identity"); below that, a different question.
    """
    # "Please state your gender (this question is voluntary...)" asks what "gender" asks.
    question = re.sub(r"\([^)]*\)", " ", question or "") or question
    q, a = norm(question), norm(ask)
    if not q or not a:
        return 0.0
    if q == a:
        return 2.0
    qc, ac = _content(question), _content(ask)
    score = 0.0
    if qc and ac:
        score = len(qc & ac) / len(qc | ac)
        if score < SAME and len(a.split()) >= 2 and _has_phrase(q, ask):
            score = SAME + len(a) / 1000
        small, big = sorted((qc, ac), key=len)
        if score < LIKELY and small <= big and len(big) - len(small) <= 2:
            score = LIKELY
    qo, ao = _option_set(options), _option_set(ask_options)
    if (len(qo) >= 3 and len(ao) >= 3 and not qo <= _COMMON_OPTIONS
            and len(qo & ao) / len(qo | ao) >= 0.8 and qc & ac):
        score = max(score, 0.9)
    return score


def _operator_answer(question: str, options: Any, facts: Facts,
                     site: str = "") -> tuple[dict[str, Any] | None, float]:
    """The operator's entry for the most alike stored question, and how alike."""
    best: tuple[dict[str, Any] | None, float] = (None, 0.0)
    for entry in facts.answers:
        if entry.get("answer") is None or entry.get("key") in field_keys():
            continue
        if entry.get("company") and entry["company"] != site:
            continue
        for ask in entry["asks"]:
            score = question_score(question, ask, options, entry.get("options"))
            if score >= LIKELY and score > best[1]:
                best = (entry, score)
    return best


# ---------------------------------------------------------------------------
# Answers worked out per form: dates, age, visas, how you heard
# ---------------------------------------------------------------------------

@dataclass
class Answer:
    """``text`` for a text box; ``choose`` the options to look for, in order of preference."""

    text: Any
    choose: list[Any] = field(default_factory=list)
    only_if_required: bool = False


_DATE_FORMATS = (("dd/mm/yyyy", "%d/%m/%Y"), ("mm/dd/yyyy", "%m/%d/%Y"), ("yyyy-mm-dd", "%Y-%m-%d"),
                 ("dd-mm-yyyy", "%d-%m-%Y"), ("mm-dd-yyyy", "%m-%d-%Y"), ("dd.mm.yyyy", "%d.%m.%Y"),
                 ("yyyy/mm/dd", "%Y/%m/%d"))


def date_text(d: date, f: dict[str, Any]) -> str:
    """A date as this field wants it: ISO for a date picker, the format its hint shows, else "9 May 2002"."""
    if (f.get("type") or "") == "date":
        return d.isoformat()
    hint = re.sub(r"\s+", "", f"{f.get('placeholder') or ''} {f.get('label') or ''}".lower())
    for pattern, fmt in _DATE_FORMATS:
        if pattern in hint:
            return d.strftime(fmt)
    return f"{d.day} {d.strftime('%B %Y')}"


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        return None


def age_on(born: date, today: date) -> int:
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


# Country names a question or a job location may carry, by the name used in the answers file.
_COUNTRIES = {
    "india": ["india"],
    "united states": ["united states", "usa", "america"],
    "united kingdom": ["united kingdom", "uk", "england", "britain", "great britain"],
    "canada": ["canada"], "germany": ["germany"], "france": ["france"], "netherlands": ["netherlands"],
    "ireland": ["ireland"], "singapore": ["singapore"], "australia": ["australia"], "japan": ["japan"],
    "united arab emirates": ["united arab emirates", "uae", "dubai", "abu dhabi"],
    "europe": ["europe", "european union", "eu", "eea"], "switzerland": ["switzerland"],
    "sweden": ["sweden"], "spain": ["spain"], "poland": ["poland"], "israel": ["israel"],
    "new zealand": ["new zealand"], "saudi arabia": ["saudi arabia"], "qatar": ["qatar"],
    "hong kong": ["hong kong"], "malaysia": ["malaysia"], "philippines": ["philippines"],
    "mexico": ["mexico"], "brazil": ["brazil"], "portugal": ["portugal"], "italy": ["italy"],
}
_US = re.compile(r"\bU\.?S\.?A?\b")


def country_in(text: str) -> str | None:
    """The one country a text names, or None (none, or several)."""
    n = norm(text)
    found = {c for c, names in _COUNTRIES.items() if any(_has_phrase(n, x) for x in names)}
    if _US.search(text or ""):
        found.add("united states")
    return found.pop() if len(found) == 1 else None


_BOARDS = {"linkedin": "LinkedIn", "naukri": "Naukri", "indeed": "Indeed", "glassdoor": "Glassdoor",
           "instahyre": "Instahyre", "wellfound": "Wellfound", "hirist": "Hirist", "cutshort": "Cutshort",
           "foundit": "Foundit", "iimjobs": "IIMJobs"}
_HEARD_DEFAULTS = ["Company website", "Company careers page", "Careers page", "Career site", "Careers website",
                   "Company site", "Website", "Job board", "Online", "Internet", "Other"]


_DEGREES = {
    "btech": ["B.Tech", "BTech", "Bachelor of Technology", "Bachelor of Engineering", "Bachelor's Degree",
              "Bachelors Degree", "Bachelor's", "Bachelors", "Bachelor", "Undergraduate"],
    "be": ["B.E.", "BE", "Bachelor of Engineering", "Bachelor of Technology", "Bachelor's Degree", "Bachelors",
           "Bachelor"],
    "bsc": ["B.Sc", "BSc", "Bachelor of Science", "Bachelor's Degree", "Bachelors", "Bachelor"],
    "bca": ["BCA", "Bachelor of Computer Applications", "Bachelor's Degree", "Bachelors", "Bachelor"],
    "mtech": ["M.Tech", "MTech", "Master of Technology", "Master's Degree", "Masters", "Master"],
    "msc": ["M.Sc", "MSc", "Master of Science", "Master's Degree", "Masters", "Master"],
    "mca": ["MCA", "Master of Computer Applications", "Master's Degree", "Masters", "Master"],
    "mba": ["MBA", "Master of Business Administration", "Master's Degree", "Masters", "Master"],
}


def degree_answer(degree: str, full: str = "") -> Answer:
    """A degree and the names forms list it under ("B.Tech" -> "Bachelor of Technology", "Bachelor's")."""
    alts = [x for x in (degree, full) if x] + _DEGREES.get(norm(degree).replace(" ", ""), [])
    return Answer(degree, list(dict.fromkeys(alts)))


def _study_answer(study: str) -> Answer:
    """"Computer Science (Big Data)": also "Computer Science", which a pick-list is likelier to hold."""
    plain = re.sub(r"\s*\([^)]*\)", "", study).strip()
    return Answer(study, list(dict.fromkeys(x for x in (study, plain) if x)))


def _ym_text(ym: tuple[int, int | None] | None, part: str | None, f: dict[str, Any],
             end: bool = False) -> str | None:
    """A (year, month) as one date box wants it: "2025", "06", "06/2025", "2025-06",
    or a whole date for a date picker (the 1st of a start month, the last day of an end month)."""
    if ym is None:
        return None
    year, month = ym
    if (f.get("type") or "") == "date" and part is None:
        import calendar
        month = month or (12 if end else 1)
        day = calendar.monthrange(year, month)[1] if end else 1
        return date(year, month, day).isoformat()
    if part == "year":
        return str(year)
    if part == "month":
        return f"{month:02d}" if month else None
    hint = norm(f"{f.get('placeholder') or ''} {f.get('label') or ''}")
    if (f.get("type") or "") == "month" and month:
        return f"{year}-{month:02d}"
    if "mm" in hint.split() and month:
        return f"{month:02d}/{year}"
    if "yyyy" in hint.split() and "mm" not in hint.split():
        return str(year)
    return f"{month:02d}/{year}" if month else str(year)


_SECTION = re.compile(r"^(work experience|experience|employment|education) (\d+)")


def _group_answer(f: dict[str, Any], v: dict[str, Any]) -> tuple[Any, str] | None:
    """A field inside "Work Experience 2" / "Education 1": that entry of the profile."""
    m = _SECTION.match(norm(f.get("group") or ""))
    if not m:
        return None
    idx = int(m.group(2)) - 1
    # An entry's boxes are short ("Job Title", "From", "To"). A sentence is a question of its own
    # ("Are you legally authorized to work in India?"), whatever section the page seems to be:
    # its "to" is no end date.
    label = f.get("label") or f.get("context") or ""
    if len(norm(label).split()) > 5:
        return None
    # The label itself: in a section, "From" / "To" are the question, not filler.
    words = set(norm(label).split())
    part = "month" if "month" in words else "year" if "year" in words else None
    is_from, is_to = bool(words & {"from", "start"}), bool(words & {"to", "end"})
    if m.group(1) == "education":
        rows = v.get("_education") or []
        if idx >= len(rows):
            return None
        e = rows[idx]
        if words & {"school", "university", "college", "institution", "institute"}:
            return (Answer(e["institution"], [e["institution"]]), "fill") if e["institution"] else None
        if words & {"field", "study", "major", "specialization", "specialisation", "discipline"}:
            return (_study_answer(e["field"]), "fill") if e["field"] else None
        if "degree" in words:
            return (degree_answer(e["degree"], e["degree_full"]), "fill") if e["degree"] else None
        if words & {"gpa", "cgpa", "grade", "result", "score", "percentage", "marks"}:
            num = _number(e["score"])
            return (num, "fill") if num else None
        if is_from or is_to:
            text = _ym_text(e["start"] if is_from else e["end"], part, f, end=not is_from)
            return (text, "fill") if text else None
        return None
    rows = v.get("_work") or []
    if idx >= len(rows):
        return None
    w = rows[idx]
    if "currently" in words or ("current" in words and words & {"work", "position", "job", "role", "employer"}):
        return ("Yes" if w["current"] else "No"), "fill"
    if "description" in words or "responsibilities" in words:
        return None
    if words & {"title", "position", "designation", "role"}:
        return w["title"], "fill"
    if words & {"company", "employer", "organization", "organisation"}:
        return w["company"], "fill"
    if "location" in words:
        return (w["location"], "fill") if w["location"] else None
    if is_from:
        text = _ym_text(w["start"], part, f)
        return (text, "fill") if text else None
    if is_to:
        if w["current"]:
            return None, "skip"  # "I currently work here" is ticked instead
        text = _ym_text(w["end"], part, f, end=True)
        return (text, "fill") if text else None
    return None


_DEVICE = ["Mobile", "Mobile Phone", "Cell", "Cell Phone", "Cellular", "Personal Mobile", "Mobile (Personal)"]


def _experience_unit(question: str) -> str:
    """"month" when the question wants the figure in months, "year" in years, "" unsaid."""
    if re.search(r"\(\s*months?\s*\)|\bin months\b|\bhow many months\b|\bmonths of\b|\bno\.? of months\b|\bnumber of months\b",
                 question, re.I):
        return "month"
    if re.search(r"\(\s*years?\s*\)|\bin years\b", question, re.I):
        return "year"
    return ""


_NUMERIC_ASK = re.compile(r"^\s*(in )?how (many|much|long)\b|\b(expected|current|desired)\s+(annual\s+)?(ctc|salary|compensation|package)\b"
                         r"|\bscale of\b|\bout of \d+|\brate (yourself|your)\b|\bnotice period\b.*\bdays\b", re.I)


def _computed(key: str, f: dict[str, Any], v: dict[str, Any], job: dict[str, Any],
              today: date) -> tuple[Any, str] | None:
    """(value, action) for answers worked out per form, or None when ``key`` isn't one."""
    question = question_text(f)
    if key == "notice_period" and v.get("notice_period") is not None:
        return v["notice_period"], "fill"  # written out in the answers file
    if key in ("notice_period", "start_date"):
        days = v.get("notice_days")
        if days is None:
            return None, "unknown"
        days = int(days)
        if key == "notice_period":
            choose = [days, f"{days} days"] + (["Immediate", "Immediately"] if days == 0 else [])
            if (f.get("type") or "") == "number":
                return Answer(str(days), choose), "fill"
            return Answer(f"{days} days", choose), "fill"
        return date_text(today + timedelta(days=days), f), "fill"
    if key in ("date_of_birth", "age", "adult"):
        born = _as_date(v.get("date_of_birth"))
        if born is None:
            return None, "unknown"
        if key == "date_of_birth":
            words = set(norm(f.get("label") or "").split())
            if "month" in words:
                return Answer(str(born.month), [born.strftime("%B"), born.strftime("%b"), f"{born.month:02d}", born.month]), "fill"
            if "day" in words:
                return Answer(str(born.day), [f"{born.day:02d}", born.day]), "fill"
            if "year" in words:
                return str(born.year), "fill"
            return date_text(born, f), "fill"
        age = age_on(born, today)
        if key == "adult":
            return ("Yes" if age >= 18 else "No"), "fill"
        return Answer(str(age), [age]), "fill"
    if key in ("work_authorization", "sponsorship"):
        home = norm(str(v.get("citizenship") or ""))
        if not home:
            return None, "unknown"
        where = country_in(question) or country_in(str(job.get("location") or ""))
        sure = where is not None
        abroad = sure and where != home
        if key == "work_authorization":
            value = "No" if abroad else "Yes"
        else:
            value = "Yes" if abroad and v.get("sponsorship_abroad", True) else "No"
        # No country in the question or the job: home is assumed, and outlined.
        return value, ("fill" if sure else "review")
    if key == "how_heard":
        site = str(job.get("site") or "").lower()
        prefs = [name for k, name in _BOARDS.items() if k in site]
        if v.get("how_heard"):
            prefs.append(v["how_heard"])
        prefs += [h for h in _HEARD_DEFAULTS if h not in prefs]
        return Answer(prefs[0], prefs), "fill"
    if key == "phone_country_code" and v.get("phone_country_code"):
        code, country = str(v["phone_country_code"]), str(v.get("country") or "")
        alts = [f"{country} ({code})", f"{country} {code}", country, code.lstrip("+")] if country else [code.lstrip("+")]
        return Answer(code, [code] + alts), "fill"
    if key == "phone_device_type":
        return Answer("Mobile", _DEVICE), "fill"
    if key == "has_work_experience":
        return ("Yes" if v.get("_work") else "No"), "fill"
    if key == "attended_university":
        degrees = [e for e in v.get("_education") or [] if re.search(r"\b(b|m)\.? ?(tech|e|sc|a|com|ca|ba)\b|bachelor|master|phd|degree",
                                                                       e.get("degree_full") or "", re.I)]
        return ("Yes" if degrees else None), ("fill" if degrees else "unknown")
    if key == "meet_requirement":
        # Only requirements the operator answered in general: work mode, relocation, shifts, travel.
        around = norm(f.get("context") or "")
        if re.search(r"\b(hybrid|onsite|on site|remote|office|working arrangement|work arrangement|work mode)\b", around):
            return "Yes", "fill"
        if re.search(r"\b(relocat\w*|shifts?|travel\w*)\b", around) and v.get("willing_to_relocate"):
            return "Yes", "review"
        return None, "unknown"
    if key == "salary_currency":
        cur = str(v.get("salary_currency") or "INR")
        names = {"INR": ["INR", "Indian Rupee", "INR - Indian Rupee", "Indian Rupee (INR)", "Rupee", "Rupees", "₹"]}
        return Answer(cur, names.get(cur.upper(), [cur])), "fill"
    if key == "self_rating":
        # "Rate your DSA skills on a scale of 1-10": the operator's rating, out of the scale's top.
        m = re.search(r"(\d+)\s*(?:-|–|—|to)\s*(\d+)", question) or re.search(r"out of\s*(\d+)", question)
        top = int(m.group(m.lastindex)) if m else 10
        low = int(m.group(1)) if m and m.lastindex == 2 else 1
        mark = v.get("self_rating")
        if mark is None:
            return None, "unknown"
        n = max(low, min(top, round(float(mark) / 10 * top)))
        return Answer(str(n), [n]), "fill"
    if key == "years_experience" and v.get("years_experience") is not None:
        # Whole years, rounded up, from whatever source (the operator's call: 2, never 1.5).
        years = float(math.ceil(float(v["years_experience"])))
        # "(Only Whole Numbers Allowed)": no decimals -- rounded UP (1.5 -> 2; the operator's call).
        if re.search(r"whole number|integer|no decimal|without decimal|digits only", question, re.I) \
                or (f.get("type") or "") == "number" and re.search(r"\bwhole\b", f.get("placeholder") or "", re.I):
            whole = math.ceil(years)
            return Answer(str(whole), [whole]), "fill"
        unit = _experience_unit(question)
        if unit == "month":
            # Beside a Years box ("Total experience (Years)" / "(Months)") it is the remainder:
            # 0 with whole years. Asked alone ("how many months of experience ..."), all of it: 24.
            months = round((years - int(years)) * 12) if f.get("_years_box") else int(years) * 12
            return Answer(str(months), [months, f"{months} months"]), "fill"
        whole = int(years)
        return Answer(str(whole), [whole, f"{whole} years", f"{whole} year"]), "fill"
    if key == "prefix":
        if not v.get("prefix"):
            return None, "unknown"
        pre = str(v["prefix"])
        return Answer(pre, list(dict.fromkeys([pre, pre.rstrip("."), pre.rstrip(".") + "."]))), "fill"
    if key == "degree" and v.get("degree"):
        return degree_answer(str(v["degree"]), str(v.get("degree_full") or "")), "fill"
    if key == "field_of_study" and v.get("field_of_study"):
        return _study_answer(str(v["field_of_study"])), "fill"
    if key == "university" and v.get("university"):
        return Answer(str(v["university"]), [str(v["university"])]), "fill"
    if key == "willing_to_relocate" and norm(str(v.get(key) or "")) == "yes":
        return Answer("Yes, I'm open to relocating.", ["Yes"]), "fill"
    return None


def _value_for(key: str, f: dict[str, Any], facts: Facts, job: dict[str, Any],
               form_has_code: bool, today: date | None = None) -> tuple[Any, str]:
    """(value, action) for a matched key; value None means unknown."""
    v = facts.values
    label = question_text(f)
    ftype = f.get("type") or ""
    worked = _computed(key, f, v, job, today or date.today())
    if worked is not None:
        return worked
    if key == "phone":
        if "phone" not in v:
            return None, "unknown"
        return (v["phone"] if form_has_code else v.get("phone_full", v["phone"])), "fill"
    if key in ("current_ctc", "expected_ctc", "ctc_both"):
        if key in v:  # an operator override wins
            return v[key], "review"
        cur = facts.salary.current
        exp = facts.salary.expected(job.get("salary_min_lpa"), job.get("salary_max_lpa"))
        if key == "ctc_both":
            if cur is None or exp is None or _OTHER_CURRENCY.search(label):
                return None, "unknown"
            return f"Current CTC: {_num(cur)} LPA; Expected CTC: {_num(exp)} LPA", "review"
        lpa = cur if key == "current_ctc" else exp
        if lpa is None:
            return None, "unknown"
        if ftype in _CHOICE_TYPES:
            return lpa, "review"
        text, _why = salary_text(lpa, label, ftype)
        return (text, "review") if text else (None, "unknown")
    if key == "resume":
        return True, "resume"
    if key == "cover_letter":
        return None, "unknown" if f.get("required") else "skip"
    if key == "ats_helper":
        return None, "skip"
    if key not in v:
        return None, "unknown"
    return v[key], "fill"


def _number(value: Any) -> str | None:
    m = re.search(r"-?\d+(?:\.\d+)?", str(value))
    return m.group() if m else None


def _shape(decision: dict[str, Any], f: dict[str, Any], value: Any, action: str, key: str | None,
           facts: Facts) -> bool:
    """Put ``value`` into ``decision`` as this field takes it. False: it doesn't fit."""
    ftype = f.get("type") or ""
    ans = value if isinstance(value, Answer) else Answer(value)
    options = [str(o) for o in f.get("options") or []]
    if ftype in _CHOICE_TYPES and options:
        text = ans.text if isinstance(ans.text, list) else [ans.text]
        wanted = list(ans.choose) + [t for t in text if t not in ans.choose]
        if key == "phone_country_code":
            wanted = [facts.values.get("phone_country_code"), facts.values.get("country")]
        if ftype in ("checkbox_group", "multiselect") and (isinstance(ans.text, list) or ans.choose):
            picks: list[str] = []
            for w in wanted:
                hit = pick_option(options, [w])
                if hit and hit not in picks:
                    picks.append(hit)
            if not picks:
                decision.update(action="unknown", reason="no answer is among the options")
                return False
            decision.update(action=action, value=picks)
            return True
        pick = pick_option(options, wanted)
        if pick is None:
            decision.update(action="unknown", reason="the answer is not among the options")
            return False
        decision.update(action=action, value=pick)
        return True
    if ftype == "checkbox":
        # A lone checkbox (consent, "I agree") is ticked only on an explicit yes.
        # The answer or any of its choices: "Yes, I'm open to relocating." chooses "Yes".
        yes = any(x is True or norm(str(x)) in ("yes", "true", "i agree", "agree") for x in [ans.text, *ans.choose])
        decision.update(action=action, value=bool(yes))
        return True
    if ftype == "file":
        decision["action"] = "unknown"
        return False
    if ans.only_if_required and not f.get("required"):
        decision["action"] = "skip"
        return True
    v = ans.text
    if isinstance(v, list):
        v = ", ".join(str(x) for x in v)
    v = str(v)
    if ftype == "number":
        v = _number(v)
        if v is None:
            decision.update(action="unknown", reason="the answer is not a number")
            return False
    decision.update(action=action, value=v)
    if ans.choose and ftype in ("combobox", "prompt", "text", "search", ""):
        # A dropdown whose options load only when opened: the page tries these in order.
        alts = [str(x) for x in ans.choose if str(x) != v]
        if alts:
            decision["alts"] = alts
    # A type-ahead location box finds "Pune" where it may not find the
    # whole "Pune, Maharashtra, India"; the userscript searches with this.
    if key == "location" and facts.values.get("city"):
        decision["search"] = str(facts.values["city"])
    return True


def match_form(fields: list[dict[str, Any]], facts: Facts, job: dict[str, Any] | None = None,
               site: str = "", today: date | None = None,
               prefilled: list[str] | None = None) -> list[dict[str, Any]]:
    """One decision per field, in the order sent.

    Each decision carries ``source``: where its answer came from ("fact:email",
    "answers:notice", "learned:l7"), which the userscript sends back at submit
    so a correction lands on the answer it corrects.
    """
    job = job or {}
    keys = field_keys()
    extra: dict[str, list[str]] = {}
    key_entry: dict[str, dict[str, Any]] = {}
    for entry in facts.answers:
        if entry.get("key") in keys:
            extra.setdefault(entry["key"], []).extend(entry["asks"])
            if entry.get("answer") is not None and entry["key"] not in facts.values:
                facts.values[entry["key"]] = entry["answer"]
                key_entry[entry["key"]] = entry

    # LinkedIn's one-line boxes for "how many ...", salaries and ratings take digits only
    # ("Invalid input" otherwise) though they are type=text: answer them as numbers.
    if "linkedin" in (site or "").lower():
        fields = [{**f, "type": "number"} if (f.get("type") or "text") == "text"
                  and _NUMERIC_ASK.search(question_text(f)) else f for f in fields]
    matched = [(f, _key_for(f, extra)) for f in fields]
    # A form with an experience box in years: its months box is the remainder, not the total.
    if any(k == "years_experience" and _experience_unit(question_text(f)) == "year" for f, k in matched):
        matched = [({**f, "_years_box": True} if k == "years_experience" else f, k) for f, k in matched]
    # A country-code field, even one the page filled itself: the phone number goes without the code.
    form_has_code = any(k == "phone_country_code" for _, k in matched) or any(
        _key_for({"label": label}, extra) == "phone_country_code" for label in prefilled or [])
    out = []
    for f, key in matched:
        ref = f.get("ref")
        if _MACHINERY.search(" ".join(str(f.get(a) or "") for a in ("name", "id", "label"))):
            out.append({"ref": ref, "action": "skip", "reason": "page machinery"})
            continue
        question = question_text(f)
        ftype = f.get("type") or ""
        value, action, source = (None, "unknown", None)
        grouped = _group_answer(f, facts.values)
        if grouped is not None:
            (value, action), source = grouped, "fact:profile"
            if action == "skip":
                out.append({"ref": ref, "key": key, "question": question, "source": source, "action": "skip"})
                continue
        elif key:
            value, action = _value_for(key, f, facts, job, form_has_code, today)
            if value is not None:
                own = key_entry.get(key)
                source = own.get("source") if own else f"fact:{key}"
                if own and not own.get("trusted", True) and action == "fill":
                    action = "review"
        if value is None and action == "unknown" and question:
            # Not a known key, or a known key with no value: the operator's own answers.
            own, score = _operator_answer(question, f.get("options"), facts, site)
            if own is not None:
                value = Answer(own["answer"], own.get("choose") or [], own.get("only_if_required", False))
                action = "fill" if score >= SAME and own.get("trusted", True) else "review"
                source = own.get("source")
        decision: dict[str, Any] = {"ref": ref, "key": key, "question": question, "source": source}
        if action in ("resume", "skip") or value is None:
            decision["action"] = action
            out.append(decision)
            continue
        ok = _shape(decision, f, value, action, key, facts)
        if not ok and key and source and source.startswith("fact:") and ftype in _CHOICE_TYPES:
            # A field the table read as a profile fact whose value is no option here
            # ("relatives working at the company?": Yes/No): try the operator's answers.
            own, score = _operator_answer(question, f.get("options"), facts, site)
            if own is not None:
                retry = {"ref": ref, "key": None, "question": question, "source": own.get("source")}
                act = "fill" if score >= SAME and own.get("trusted", True) else "review"
                if _shape(retry, f, Answer(own["answer"], own.get("choose") or [],
                                           own.get("only_if_required", False)), act, None, facts):
                    decision = retry
        out.append(decision)
    return out
