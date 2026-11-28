"""What the autofill knows about the operator, and where each fact comes from.

Facts already in the codebase are never typed twice:

    master_profile.json   name, email, phone, location, GitHub, LinkedIn,
                          certificates link, current employer and title (the
                          ``employment`` entry still ``present``), education
    config.yaml           portfolio URL, years of experience, relocation
    data/ats_answers.yaml everything else: salary, notice period, work
                          authorisation, survey answers... (gitignored: this
                          repo is public, so no answer may live in it)

The answers file is the operator's. Code only ever APPENDS to it (new questions
under ``unanswered:``), so their comments and layout survive; see :mod:`.bank`.

What the operator types into a form and submits is kept apart, in
``data/ats_learned.yaml`` (gitignored, rewritten by code; see :mod:`.learn`).
A learned answer either answers a new question or replaces one from the
answers file (``replaces:``). It is outlined for checking until it has been
submitted unchanged twice.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.config import settings

_ROOT = Path(__file__).resolve().parents[2]
PROFILE_JSON = _ROOT / "master_profile.json"
ANSWERS_PATH = _ROOT / "data" / "ats_answers.yaml"
LEARNED_PATH = _ROOT / "data" / "ats_learned.yaml"
#: Submitted unchanged this many times, a learned answer is filled without an outline.
TRUSTED_AFTER = 2


@dataclass
class Salary:
    """CTC in LPA. ``expected_min`` is a floor, never a target."""

    current: float | None = None
    expected_min: float | None = None

    def expected(self, advert_min: float | None, advert_max: float | None) -> float | None:
        """Expected CTC for one advert: the middle of its range, never below the floor.

        Only a stated range has a middle. A lone lower bound ("8+ LPA") is taken as
        it is; a lone upper bound ("up to 20 LPA") says nothing about where the
        band sits, so the floor stands.
        """
        if advert_min and advert_max and advert_max >= advert_min:
            target = (advert_min + advert_max) / 2
        elif advert_min and not advert_max:
            target = advert_min
        else:
            target = None
        candidates = [v for v in (target, self.expected_min) if v]
        return max(candidates) if candidates else None


@dataclass
class Facts:
    """Every answer the autofill can give, keyed as in config/ats_fields.yaml."""

    values: dict[str, Any] = field(default_factory=dict)
    salary: Salary = field(default_factory=Salary)
    #: Operator answers keyed by field key, plus free-form ones matched on wording:
    #: each ``{"key", "asks", "answer", "choose", "only_if_required", "options",
    #: "company", "source", "trusted"}``. ``answer`` None: asked, not answered yet.
    answers: list[dict[str, Any]] = field(default_factory=list)


def _split_name(full: str) -> tuple[str, str]:
    parts = full.split()
    if len(parts) < 2:
        return full, ""
    return " ".join(parts[:-1]), parts[-1]


def _split_phone(phone: str) -> tuple[str, str]:
    """("+91", national digits) from "+91-98xx-xxx-xxx"; ("", digits) when no code."""
    m = re.match(r"^\s*\+(\d{1,3})[\s\-().]*(.*)$", phone or "")
    if not m:
        return "", re.sub(r"\D", "", phone or "")
    return f"+{m.group(1)}", re.sub(r"\D", "", m.group(2))


def _year(text: str) -> str:
    years = re.findall(r"(?:19|20)\d{2}", text or "")
    return years[-1] if years else ""


def _degree_parts(degree: str) -> tuple[str, str]:
    """("B.Tech", "Computer Science (Big Data)") from "B.Tech in Computer Science (Big Data)"."""
    head, sep, tail = (degree or "").partition(" in ")
    return (head.strip(), tail.strip()) if sep else (degree.strip(), "")


def profile_facts(profile: dict[str, Any]) -> dict[str, Any]:
    """Facts from the canonical profile JSON and config.yaml."""
    personal = profile.get("personal") or {}
    out: dict[str, Any] = {}
    name = personal.get("name") or settings.operator.full_name
    out["full_name"] = name
    out["first_name"], out["last_name"] = _split_name(name)
    if personal.get("email"):
        out["email"] = out["email_confirm"] = personal["email"]
    if personal.get("phone"):
        code, national = _split_phone(personal["phone"])
        out["phone"] = national or personal["phone"]
        out["phone_full"] = personal["phone"]
        if code:
            out["phone_country_code"] = code
    loc = personal.get("location") or ""
    if loc:
        out["location"] = loc
        bits = [b.strip() for b in loc.split(",") if b.strip()]
        if bits:
            out["city"] = bits[0]
        if len(bits) >= 3:
            out["state"] = bits[1]
        if len(bits) >= 2:
            out["country"] = bits[-1]
    for key in ("linkedin", "github"):
        if personal.get(key):
            out[key] = personal[key]
    if personal.get("certificates_link"):
        out["certificates"] = personal["certificates_link"]
    header = settings.operator.get("resume_header") or {}
    portfolio = header.get("portfolio_url") if hasattr(header, "get") else None
    if portfolio:
        out["portfolio"] = portfolio
    years = settings.operator.get("years_experience")
    if years is not None:
        # Whole years, rounded up, on every form (the operator's call: 2, never 1.5).
        import math

        out["years_experience"] = math.ceil(float(years))

    current = [w for w in profile.get("work_experience") or []
               if w.get("employment_type", "employment") == "employment"
               and str(w.get("end_date", "")).lower() == "present"]
    if current:
        out["current_company"] = current[0].get("company")
        out["current_title"] = current[0].get("actual_title")

    education = profile.get("education") or []
    if education:
        # Listed newest first in the profile; the first is the highest degree.
        top = education[0]
        out["degree"], study = _degree_parts(top.get("degree", ""))
        out["degree_full"] = top.get("degree", "")
        if study:
            out["field_of_study"] = study
        out["university"] = top.get("institution")
        if _year(top.get("dates", "")):
            out["graduation_year"] = _year(top.get("dates", ""))

    if settings.get("relocation", {}).get("enabled"):
        out["willing_to_relocate"] = "Yes"
    out["_work"] = work_entries(profile)
    out["_education"] = education_entries(profile)
    return {k: v for k, v in out.items() if v not in (None, "")}


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def _ym(text: Any) -> tuple[int, int | None] | None:
    """(year, month) from "2025-06", "Jun 2025", "2025"; None for "present" or nothing."""
    t = str(text or "").strip().lower()
    if m := re.fullmatch(r"((?:19|20)\d{2})-(\d{1,2})(?:-\d{1,2})?", t):
        return int(m.group(1)), int(m.group(2))
    if m := re.fullmatch(r"([a-z]{3})[a-z]*\.?\s+((?:19|20)\d{2})", t):
        return int(m.group(2)), _MONTHS.get(m.group(1))
    if m := re.fullmatch(r"(?:19|20)\d{2}", t):
        return int(t), None
    return None


def work_entries(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Jobs with dates, newest first: what a form's "Work Experience 1, 2..." asks for."""
    rows = []
    for w in profile.get("work_experience") or []:
        start = _ym(w.get("start_date"))
        if start is None:
            continue
        current = str(w.get("end_date", "")).strip().lower() == "present"
        location = str(w.get("location") or "")
        rows.append({"title": w.get("actual_title") or "", "company": w.get("company") or "",
                     "location": "" if location.lower() in ("none", "") else location,
                     "start": start, "end": None if current else _ym(w.get("end_date")), "current": current})
    rows.sort(key=lambda r: (r["current"], r["end"] or (9999, 12), r["start"]), reverse=True)
    return rows


def education_entries(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Education as listed in the profile (highest first), with start and end."""
    rows = []
    for e in profile.get("education") or []:
        parts = [p.strip() for p in re.split(r"\s+[-–]\s+", str(e.get("dates") or "")) if p.strip()]
        degree, study = _degree_parts(e.get("degree", ""))
        rows.append({"institution": e.get("institution") or "", "degree": degree, "degree_full": e.get("degree", ""),
                     "field": study, "score": e.get("score") or "",
                     "start": _ym(parts[0]) if parts else None, "end": _ym(parts[-1]) if len(parts) > 1 else None})
    return rows


def load_answers_file(path: Path = ANSWERS_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def load_learned(path: Path = LEARNED_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = data.get("learned") if isinstance(data, dict) else None
    return [r for r in rows or [] if isinstance(r, dict) and r.get("id")]


def _entry(key: str | None, asks: list[str], raw: dict[str, Any], source: str) -> dict[str, Any]:
    return {"key": key, "asks": [a for a in asks if a], "answer": raw.get("answer"),
            "choose": list(raw.get("choose") or []), "only_if_required": bool(raw.get("only_if_required")),
            "options": list(raw.get("options") or []), "company": raw.get("company"),
            "source": source, "trusted": True}


def load_facts(profile_path: Path = PROFILE_JSON, answers_path: Path = ANSWERS_PATH,
               learned_path: Path | None = None) -> Facts:
    """All facts, read fresh on every call so an edit to any file applies at once."""
    profile = json.loads(profile_path.read_text(encoding="utf-8")) if profile_path.exists() else {}
    values = profile_facts(profile)
    data = load_answers_file(answers_path)
    if learned_path is None:
        learned_path = answers_path.with_name(LEARNED_PATH.name)

    salary_cfg = data.get("salary") or {}
    salary = Salary(current=salary_cfg.get("current_ctc_lpa"),
                    expected_min=salary_cfg.get("expected_min_lpa"))

    answers: list[dict[str, Any]] = []
    for key, entry in (data.get("answers") or {}).items():
        if isinstance(entry, dict):
            answers.append(_entry(key, list(entry.get("asks") or []), entry, f"answers:{key}"))
        elif entry is not None:
            # Shorthand `key: value` overrides a profile fact ("city: Bengaluru").
            values[key] = entry
    for entry in data.get("unanswered") or []:
        if isinstance(entry, dict) and entry.get("ask"):
            answers.append(_entry(None, [entry["ask"]], entry, f"unanswered:{entry['ask']}"))

    by_source = {a["source"]: a for a in answers}
    for row in load_learned(learned_path):
        trusted = int(row.get("confirms") or 0) >= TRUSTED_AFTER
        target = by_source.get(row.get("replaces") or "")
        if target is not None:
            # A correction typed into a form beats what the answers file says.
            target.update(answer=row.get("answer"), choose=[], only_if_required=False,
                          source=f"learned:{row['id']}", trusted=trusted)
            continue
        if row.get("ask") and row.get("answer") is not None:
            entry = _entry(None, [row["ask"]], row, f"learned:{row['id']}")
            entry["trusted"] = trusted
            answers.append(entry)
    return Facts(values=values, salary=salary, answers=answers)
