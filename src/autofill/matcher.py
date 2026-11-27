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

import re
from dataclasses import dataclass
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
    "which will with would you your enter specify".split()
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
def _table() -> tuple[tuple[FieldSpec, ...], frozenset[str], frozenset[str]]:
    data = yaml.safe_load(_FIELDS_PATH.read_text(encoding="utf-8"))
    specs = tuple(
        FieldSpec(k, tuple(v.get("asks") or ()), tuple(v.get("not") or ()), tuple(v.get("attrs") or ()))
        for k, v in data["fields"].items()
    )
    generic = frozenset(norm(g) for g in data.get("generic_labels") or ())
    placeholders = frozenset(norm(p) for p in data.get("placeholder_options") or ())
    return specs, generic, placeholders


def field_keys() -> frozenset[str]:
    return frozenset(s.key for s in _table()[0])


def is_generic(label: str) -> bool:
    n = norm(label)
    return not n or n in _table()[1] or len(n) < 2


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


def _key_for(f: dict[str, Any], extra: dict[str, list[str]]) -> str | None:
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


def _operator_answer(question: str, facts: Facts) -> Any:
    """A free-form answer whose wording matches this question, else None."""
    q = norm(question)
    qc = _content(question)
    best: tuple[Any, float] = (None, 0.0)
    for entry in facts.answers:
        for ask in entry["asks"]:
            ac = _content(ask)
            score = 0.0
            if norm(ask) == q:
                score = 2.0
            elif ac and qc:
                score = len(ac & qc) / len(ac | qc)
                if score < 0.75 and len(norm(ask).split()) >= 2 and _has_phrase(q, ask):
                    score = 0.75 + len(norm(ask)) / 1000
            if score >= 0.75 and score > best[1]:
                best = (entry["answer"], score)
    return best[0]


def _value_for(key: str, f: dict[str, Any], facts: Facts, job: dict[str, Any],
               form_has_code: bool) -> tuple[Any, str]:
    """(value, action) for a matched key; value None means unknown."""
    v = facts.values
    label = question_text(f)
    ftype = f.get("type") or ""
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


def match_form(fields: list[dict[str, Any]], facts: Facts, job: dict[str, Any] | None = None,
               ) -> list[dict[str, Any]]:
    """One decision per field, in the order sent."""
    job = job or {}
    keys = field_keys()
    extra: dict[str, list[str]] = {}
    for entry in facts.answers:
        if entry.get("key") in keys:
            extra.setdefault(entry["key"], []).extend(entry["asks"])
            facts.values.setdefault(entry["key"], entry["answer"])

    matched = [(f, _key_for(f, extra)) for f in fields]
    form_has_code = any(k == "phone_country_code" for _, k in matched)
    out = []
    for f, key in matched:
        ref = f.get("ref")
        if _MACHINERY.search(" ".join(str(f.get(a) or "") for a in ("name", "id", "label"))):
            out.append({"ref": ref, "action": "skip", "reason": "page machinery"})
            continue
        question = question_text(f)
        ftype = f.get("type") or ""
        value, action = (None, "unknown")
        if key:
            value, action = _value_for(key, f, facts, job, form_has_code)
        if value is None and action == "unknown" and question:
            # Not a known key, or a known key with no value: the operator's own answers.
            own = _operator_answer(question, facts)
            if own is not None:
                value, action = own, "fill"
        decision: dict[str, Any] = {"ref": ref, "key": key, "question": question}
        if action in ("resume", "skip") or value is None:
            decision["action"] = action
            out.append(decision)
            continue
        options = [str(o) for o in f.get("options") or []]
        if ftype in _CHOICE_TYPES and options:
            wanted = value if isinstance(value, list) else [value]
            if key == "phone_country_code":
                wanted = [facts.values.get("phone_country_code"), facts.values.get("country")]
            if ftype in ("checkbox_group", "multiselect") and isinstance(value, list):
                picks = [pick_option(options, [w]) for w in wanted]
                if not all(picks):
                    decision["action"] = "unknown"
                    decision["reason"] = "an answer is not among the options"
                else:
                    decision.update(action=action, value=picks)
            else:
                pick = pick_option(options, wanted)
                if pick is None:
                    decision["action"] = "unknown"
                    decision["reason"] = "the answer is not among the options"
                else:
                    decision.update(action=action, value=pick)
        elif ftype == "checkbox":
            # A lone checkbox (consent, "I agree") is ticked only on an explicit yes.
            yes = value is True or norm(str(value)) in ("yes", "true", "i agree", "agree")
            decision.update(action=action, value=bool(yes))
        elif ftype == "file":
            decision["action"] = "unknown"
        else:
            if isinstance(value, list):
                value = ", ".join(str(x) for x in value)
            decision.update(action=action, value=str(value))
            # A type-ahead location box finds "Pune" where it may not find the
            # whole "Pune, Maharashtra, India"; the userscript searches with this.
            if key == "location" and facts.values.get("city"):
                decision["search"] = str(facts.values["city"])
        out.append(decision)
    return out
