"""The job's city in the resume header (#134).

When ``relocation.enabled`` is on and the job names a city other than the
operator's own, the header location line becomes
``Relocating to <city> by <D Mon YYYY>, willing to relocate sooner if required.`` so a
recruiter or an ATS location filter does not read the candidate as non-local.
The city comes from the job's location field, else from the advert text; only
when neither names a recognised city does the profile location stay.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

from src.config import settings

_ROOT = Path(__file__).resolve().parents[2]

# Canonical name -> spellings seen in job locations. India only: a header that
# claims a move abroad would raise a work-authorisation question the resume
# cannot answer.
CITIES: dict[str, tuple[str, ...]] = {
    "Bengaluru": ("bengaluru", "bangalore"),
    "Hyderabad": ("hyderabad", "secunderabad"),
    "Chennai": ("chennai", "madras"),
    "Pune": ("pune",),
    "Navi Mumbai": ("navi mumbai",),
    "Mumbai": ("mumbai", "bombay"),
    "Thane": ("thane",),
    "Gurugram": ("gurugram", "gurgaon"),
    "Noida": ("noida",),
    "New Delhi": ("new delhi",),
    "Delhi": ("delhi",),
    "Kolkata": ("kolkata", "calcutta"),
    "Ahmedabad": ("ahmedabad",),
    "Kochi": ("kochi", "cochin", "ernakulam"),
    # Shown as "Trivandrum": the full name pushes the header line past one line.
    "Trivandrum": ("thiruvananthapuram", "trivandrum"),
    "Coimbatore": ("coimbatore",),
    "Mysuru": ("mysuru", "mysore"),
    "Jaipur": ("jaipur",),
    "Chandigarh": ("chandigarh",),
    "Mohali": ("mohali",),
    "Indore": ("indore",),
    "Nagpur": ("nagpur",),
    "Lucknow": ("lucknow",),
    "Bhubaneswar": ("bhubaneswar",),
    "Visakhapatnam": ("visakhapatnam", "vizag"),
    "Vadodara": ("vadodara", "baroda"),
    "Surat": ("surat",),
}

# Longest spelling first, so "navi mumbai" wins over "mumbai" and "new delhi"
# over "delhi" at the same position.
_SPELLINGS = sorted(
    ((alias, city) for city, aliases in CITIES.items() for alias in aliases),
    key=lambda pair: -len(pair[0]),
)
_PATTERN = re.compile(
    r"(?<![a-z])(" + "|".join(re.escape(a) for a, _ in _SPELLINGS) + r")(?![a-z])"
)
_CANONICAL = dict(_SPELLINGS)


# Words that say where a job is without naming a city: work modes, "India",
# Indian states and their two-letter codes ("KA, IN"), "Multiple locations".
_NO_PLACE = re.compile(
    r"\b(remote|hybrid|on-?site|in-?office|wfh|work from (home|anywhere)|anywhere|"
    r"multiple|various|locations?|flexible|india|in|"
    r"karnataka|maharashtra|tamil nadu|telangana|kerala|gujarat|uttar pradesh|haryana|"
    r"west bengal|andhra pradesh|rajasthan|punjab|madhya pradesh|odisha|himachal pradesh|"
    r"uttarakhand|goa|assam|bihar|jharkhand|chhattisgarh|"
    r"ka|mh|tn|ts|tg|kl|gj|up|hr|dl|wb|ap|rj|pb|mp|od|or|hp|uk|ga|as|br|jh|cg|ct)\b",
    re.I,
)


def _names_no_place(location: str | None) -> bool:
    """True when the location field is blank or only gives a mode, a country or a state.

    Only then is the advert text read for a city: a field naming a city outside the
    list ("New York, NY") means the job is elsewhere, and a city the advert merely
    mentions must not override it.
    """
    return not re.search(r"[a-z]", _NO_PLACE.sub(" ", location or "").lower())


def cities_in(text: str | None) -> list[str]:
    """Recognised cities in ``text``, in the order the text names them, deduplicated."""
    seen: list[str] = []
    for m in _PATTERN.finditer((text or "").lower()):
        city = _CANONICAL[m.group(1)]
        if city not in seen:
            seen.append(city)
    return seen


def city_of(text: str | None) -> str | None:
    found = cities_in(text)
    return found[0] if found else None


def job_city(location: str | None, jd_text: str | None, home: str | None) -> str | None:
    """The city to show, or None to keep the profile location.

    The job's location field is read first and the advert text only when the field
    names no city, so a remote role that names an office is treated like an onsite
    one. The profile location stays when no city is found anywhere, or when the
    operator's own city is one of the job's cities (no move needed).
    """
    found = cities_in(location)
    if not found and _names_no_place(location):
        found = cities_in(jd_text)
    if not found or city_of(home) in found:
        return None
    return found[0]


def header_line(city: str, today: date, days: int) -> str:
    by = today + timedelta(days=days)
    return f"Relocating to {city} by {by.day} {by:%b %Y}, willing to relocate sooner if required."


def enabled() -> bool:
    cfg = settings.get("relocation") or {}
    return bool(cfg.get("enabled", False))


def _days() -> int:
    cfg = settings.get("relocation") or {}
    return int(cfg.get("days", 30))


def _home_location() -> str | None:
    try:
        data = json.loads((_ROOT / "master_profile.json").read_text())
    except (OSError, ValueError):
        return None
    return (data.get("personal") or {}).get("location")


def header_location_for(location: str | None, jd_text: str | None,
                        today: date | None = None) -> str | None:
    """The header line for a job, or None when the header should stay as it is."""
    if not enabled():
        return None
    city = job_city(location, jd_text, _home_location())
    if city is None:
        return None
    return header_line(city, today or date.today(), _days())
