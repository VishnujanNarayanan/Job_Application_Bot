"""The job's city in the resume header (#134).

When ``relocation.enabled`` is on and the job is in an Indian city other than the
operator's own, the header location line becomes
``Relocating to <city> by <D Mon YYYY>, willing to relocate sooner if required.``
so a recruiter or an ATS location filter does not read the candidate as non-local.

Where the city comes from, in order:

1. a known Indian city anywhere in the job's location field (a multi-country list
   included: "Austin, TX; Bengaluru, Karnataka, India" gives Bengaluru);
2. any other place in a segment the field marks as Indian ("Faridkot, PB, India");
3. when the field names no place at all (remote, work from home, a country, a
   state, "Multiple locations", blank), the advert: a "Location:" line first,
   then any known Indian city it mentions.

A field naming only places abroad keeps the profile location, and so does a job
that lists the operator's own city. Work modes are never shown as a place.
"""

from __future__ import annotations

import calendar
import json
import re
from datetime import date
from pathlib import Path

from src.config import settings

_ROOT = Path(__file__).resolve().parents[2]

# Canonical name -> spellings seen in job locations and adverts.
CITIES: dict[str, tuple[str, ...]] = {
    "Bengaluru": ("bengaluru", "bangalore", "banglore", "bengalore", "bangaluru", "benagaluru",
                  "bengalura", "benguluru", "bengaluru urban"),
    "Hyderabad": ("hyderabad", "secunderabad", "hyderbad", "hydrebad", "hyderabaad"),
    "Chennai": ("chennai", "madras"),
    "Pune": ("pune", "poona"),
    "Navi Mumbai": ("navi mumbai",),
    "Mumbai": ("mumbai", "bombay"),
    "Thane": ("thane",),
    "Gurugram": ("gurugram", "gurgaon"),
    "Noida": ("noida",),
    "New Delhi": ("new delhi",),
    "Delhi": ("delhi",),
    "Kolkata": ("kolkata", "calcutta"),
    "Ahmedabad": ("ahmedabad",),
    "Gandhinagar": ("gandhinagar",),
    "GIFT City": ("gift city", "gift-city", "gujarat international finance tec-city"),
    "Kochi": ("kochi", "cochin", "ernakulam"),
    # Shown as "Trivandrum": the full name pushes the header line past one line.
    "Trivandrum": ("thiruvananthapuram", "trivandrum"),
    "Kozhikode": ("kozhikode", "calicut"),
    "Coimbatore": ("coimbatore",),
    "Madurai": ("madurai",),
    "Mysuru": ("mysuru", "mysore"),
    "Mangaluru": ("mangaluru", "mangalore"),
    "Hubballi": ("hubballi", "hubli"),
    "Jaipur": ("jaipur",),
    "Chandigarh": ("chandigarh",),
    "Mohali": ("mohali", "sahibzada ajit singh nagar", "sas nagar"),
    "Indore": ("indore",),
    "Bhopal": ("bhopal",),
    "Nagpur": ("nagpur",),
    "Nashik": ("nashik", "nasik"),
    "Lucknow": ("lucknow",),
    "Kanpur": ("kanpur",),
    "Bhubaneswar": ("bhubaneswar", "bhubaneshwar"),
    "Visakhapatnam": ("visakhapatnam", "vishakhapatnam", "vishakapatnam", "vizag"),
    "Vijayawada": ("vijayawada",),
    "Vadodara": ("vadodara", "baroda"),
    "Surat": ("surat",),
    "Ghaziabad": ("ghaziabad",),
    "Faridabad": ("faridabad",),
    "Patna": ("patna",),
    "Ranchi": ("ranchi",),
    "Jamshedpur": ("jamshedpur",),
    "Raipur": ("raipur",),
    "Guwahati": ("guwahati",),
    "Dehradun": ("dehradun",),
    "Ludhiana": ("ludhiana",),
    "Jalandhar": ("jalandhar",),
    "Amritsar": ("amritsar",),
    "Rajkot": ("rajkot",),
    "Jodhpur": ("jodhpur",),
    "Udaipur": ("udaipur",),
    "Agra": ("agra",),
    "Varanasi": ("varanasi",),
    "Meerut": ("meerut",),
    "Cuttack": ("cuttack",),
    "Tiruchirappalli": ("tiruchirappalli", "trichy"),
    "Tirupati": ("tirupati",),
    "Guntur": ("guntur",),
    "Warangal": ("warangal",),
    "Hosur": ("hosur",),
    "Vellore": ("vellore",),
    "Siliguri": ("siliguri",),
    "Durgapur": ("durgapur",),
    "Gwalior": ("gwalior",),
    "Jabalpur": ("jabalpur",),
    "Aurangabad": ("aurangabad", "chhatrapati sambhajinagar", "sambhajinagar"),
    "Kolhapur": ("kolhapur",),
    "Belagavi": ("belagavi", "belgaum"),
    "Panaji": ("panaji", "panjim"),
}

# Neighbourhoods and tech parks a job names instead of its city: the header shows
# the city, which is what a recruiter or a location filter looks for.
_LOCALITIES: dict[str, tuple[str, ...]] = {
    "Pune": ("kharadi", "viman nagar", "hinjewadi", "hinjawadi", "magarpatta", "baner", "wakad",
             "hadapsar", "kalyani nagar", "pimpri", "chinchwad", "pimpri-chinchwad", "talegaon"),
    "Hyderabad": ("banjara hills", "madhapur", "hitech city", "hi-tech city", "gachibowli",
                  "kondapur", "bachupally", "nanakramguda", "kukatpally", "nuthankal"),
    "Bengaluru": ("indiranagar", "whitefield", "electronic city", "koramangala", "marathahalli",
                  "bellandur", "hebbal", "yelahanka", "sarjapur", "bommasandra", "hebbagodi",
                  "attibele", "anekal", "hsr layout", "manyata", "devanahalli", "hoskote"),
    "Navi Mumbai": ("airoli", "turbhe", "vashi", "belapur", "ghansoli", "rabale"),
    "Mumbai": ("kurla", "vikhroli", "andheri", "powai", "goregaon", "malad", "bandra",
               "lower parel", "worli"),
    "Chennai": ("porur", "padur", "oragadam", "sholinganallur", "guindy", "perungudi", "siruseri",
                "thoraipakkam", "velachery", "ambattur", "padappai"),
    "Kochi": ("kakkanad", "infopark"),
    "Trivandrum": ("technopark",),
    "Gurugram": ("udyog vihar", "cyber city", "dlf cyber"),
}

# Longest spelling first, so "navi mumbai" wins over "mumbai" and "new delhi"
# over "delhi" at the same position.
_SPELLINGS = sorted(
    [(alias, city) for city, aliases in CITIES.items() for alias in aliases]
    + [(alias, city) for city, aliases in _LOCALITIES.items() for alias in aliases],
    key=lambda pair: -len(pair[0]),
)
_PATTERN = re.compile(
    r"(?<![a-z])(" + "|".join(re.escape(a) for a, _ in _SPELLINGS) + r")(?![a-z])"
)
_CANONICAL = dict(_SPELLINGS)

_STATES = (
    "karnataka|maharashtra|tamil nadu|telangana|kerala|gujarat|uttar pradesh|haryana|"
    "west bengal|andhra pradesh|rajasthan|punjab|madhya pradesh|odisha|orissa|goa|"
    "himachal pradesh|uttarakhand|assam|bihar|jharkhand|chhattisgarh|jammu|kashmir|sikkim|"
    "meghalaya|tripura|manipur|mizoram|nagaland|arunachal pradesh|telengana|tamilnadu|"
    "uttarpradesh|delhi ncr|ncr"
)
_STATE_CODES = ("ka|mh|tn|ts|tg|kl|gj|up|hr|dl|wb|ap|rj|pb|mp|od|or|hp|uk|ga|as|br|jh|cg|ct|jk|ch|"
                "ml|tr|an|sk|mn|mz|nl|ar|py")
_COUNTRIES = (
    r"india|united states( of america)?|usa|u\.s\.a?\.?|us|united kingdom|uk|great britain|"
    "england|scotland|ireland|canada|mexico|brazil|argentina|chile|colombia|peru|germany|"
    "france|spain|portugal|italy|netherlands|belgium|switzerland|austria|poland|czechia|"
    "czech republic|romania|bulgaria|serbia|ukraine|greece|turkey|türkiye|sweden|norway|"
    "denmark|finland|estonia|latvia|lithuania|israel|uae|united arab emirates|saudi arabia|"
    "qatar|egypt|nigeria|kenya|south africa|pakistan|bangladesh|sri lanka|nepal|singapore|"
    "malaysia|indonesia|philippines|vietnam|thailand|japan|china|south korea|korea|taiwan|"
    "hong kong|australia|new zealand|kazakhstan|armenia|georgia|cyprus|malta|bhutan|eu|"
    "middle east|africa"
)
# Words that say where a job is without naming a city.
_NO_PLACE = re.compile(
    r"\b(remote(-friendly)?|hybrid|on-?site|in-?office|office|hq|headquarters|wfh|"
    r"work(ing)? from (home|anywhere)|anywhere|multiple|various|locations?|flexible|"
    r"preferred|based|eligible|only|global|worldwide|emea|apac|apj|amer|namer|latam|"
    r"europe|asia|americas|north america|continental|region|area|and|or|the|of|in|hub|"
    r"any|all|cities|pan|open|to|full-time|part-time|contract|permanent|fully|travel|"
    r"required|person|can|be|city|role|first|peo|"
    + _STATES + "|" + _STATE_CODES + "|" + _COUNTRIES + r")\b",
    re.I,
)
# A segment is Indian when it says India, names a state, or ends "<state code>, IN"
# ("KA, IN"). A bare ", IN" is Indiana ("Indianapolis, IN").
_INDIA_SEGMENT = re.compile(
    r"\bindia\b|\b(" + _STATES + r")\b|,\s*(" + _STATE_CODES + r")\s*,\s*in\s*$", re.I
)
# "Location: Pune", "**Job Location:** Mumbai", "Work location - Hybrid in Pune".
_LOCATION_LINE = re.compile(
    r"(?:^|\n)[\s*#_>\\-]*(?:job\s+|work\s+|office\s+)?locations?\s*[*\\]*\s*[:\-–]\s*[*\\]*\s*"
    r"([^\n]{2,90})",
    re.I,
)

# State of each known city, to prefer a city in the state a "KA, IN" field names.
_STATE_OF = {
    "KA": ("Bengaluru", "Mysuru", "Mangaluru", "Hubballi"),
    "TS": ("Hyderabad",), "TG": ("Hyderabad",),
    "TN": ("Chennai", "Coimbatore", "Madurai"),
    "MH": ("Pune", "Mumbai", "Navi Mumbai", "Thane", "Nagpur", "Nashik"),
    "HR": ("Gurugram",), "UP": ("Noida", "Lucknow", "Kanpur"), "DL": ("New Delhi", "Delhi"),
    "WB": ("Kolkata",), "GJ": ("Ahmedabad", "Gandhinagar", "GIFT City", "Vadodara", "Surat"),
    "KL": ("Kochi", "Trivandrum", "Kozhikode"), "RJ": ("Jaipur",), "CH": ("Chandigarh",),
    "PB": ("Mohali",), "MP": ("Indore", "Bhopal"), "OD": ("Bhubaneswar",),
    "AP": ("Visakhapatnam", "Vijayawada"),
}
_STATE_NAMES = {
    "karnataka": "KA", "telangana": "TS", "tamil nadu": "TN", "maharashtra": "MH", "haryana": "HR",
    "uttar pradesh": "UP", "delhi": "DL", "west bengal": "WB", "gujarat": "GJ", "kerala": "KL",
    "rajasthan": "RJ", "punjab": "PB", "madhya pradesh": "MP", "odisha": "OD", "andhra pradesh": "AP",
}


def _state_in(location: str | None) -> str | None:
    """The Indian state a location field names, as its code, if any."""
    low = (location or "").lower()
    for name, code in _STATE_NAMES.items():
        if re.search(rf"\b{name}\b", low):
            return code
    m = re.search(r"\b([a-z]{2})\s*,\s*in\s*$", low)
    return m.group(1).upper() if m and m.group(1).upper() in _STATE_OF else None


def _india_hint(location: str | None) -> bool:
    """The field marks the job as Indian: India, a state, or "Remote, IN" style codes."""
    loc = location or ""
    return bool(_INDIA_SEGMENT.search(loc)) or (
        bool(re.search(r",\s*in\s*$", loc, re.I)) and _names_no_place(loc)
    )


def cities_in(text: str | None) -> list[str]:
    """Known cities in ``text``, in the order the text names them, deduplicated."""
    seen: list[str] = []
    for m in _PATTERN.finditer((text or "").lower()):
        city = _CANONICAL[m.group(1)]
        if city not in seen:
            seen.append(city)
    return seen


def city_of(text: str | None) -> str | None:
    found = cities_in(text)
    return found[0] if found else None


def _names_no_place(text: str | None) -> bool:
    """True when ``text`` is blank or only gives modes, regions, countries or states."""
    rest = _NO_PLACE.sub(" ", text or "")
    return not re.search(r"[a-z]{2}", rest.lower())


def _segments(location: str) -> list[str]:
    """One location per segment: multi-location fields join them with ; / | or "or"."""
    return [s.strip() for s in re.split(r"\s*(?:;|/|\||\bor\b|\bOR\b)\s*", location) if s.strip()]


_MAX_PLACE = 15


def _clean_place(part: str) -> str | None:
    """A city-like name from one comma part, or None if it is not a place."""
    part = re.sub(r"\([^)]*\)", " ", part).strip()
    # "India" is its own comma part in a real location; inside one ("IRIS India",
    # "PAN India") it names a company or a region, not a place.
    if not part or re.search(r"\bindia\b", part, re.I) or _names_no_place(part):
        return None
    part = re.sub(r"^(near|pen)?\s*(dist\.?|district)?\s+", "", part, flags=re.I)
    part = re.sub(r"\b(office|hq|headquarters|division|urban|rural|district|city|east|west|"
                  r"north|south|central|metropolitan|metro|area|region|greater)\b", " ", part, flags=re.I)
    part = re.sub(r"\s+[A-Za-z]$", "", re.sub(r"\s+", " ", part)).strip(" .-–")
    if len(part) < 4:
        return None
    if not part or _names_no_place(part) or not re.fullmatch(r"[A-Za-z][A-Za-z .'-]{2,30}", part):
        return None
    # Longer than this and the header line would wrap onto a second row.
    if len(part.split()) > 3 or len(part) > _MAX_PLACE:
        return None
    return part.title() if part.islower() or part.isupper() else part


def _indian_place(location: str) -> str | None:
    """A place from a segment marked as Indian when no known city matched."""
    for seg in _segments(location):
        if not _INDIA_SEGMENT.search(seg):
            continue
        for part in seg.split(","):
            place = _clean_place(part)
            if place:
                return place
    return None


def _advert_cities(jd_text: str | None, location: str | None = None) -> list[str]:
    """Cities from the advert: its "Location:" lines first, then any known city.

    A place off the known list is taken from a "Location:" line only when it is
    marked Indian, or the job's own field already marks the job as Indian, so a US
    advert's "Location: Tampa, FL" never becomes a relocation line.
    """
    found: list[str] = []
    india = _india_hint(location)
    for line in _LOCATION_LINE.findall(jd_text or ""):
        place = _indian_place(line) or (
            _clean_place(re.split(r"[,(|/]", line)[0]) if india else None
        )
        for city in cities_in(line) or ([place] if place else []):
            if city not in found:
                found.append(city)
    for city in cities_in(jd_text):
        if city not in found:
            found.append(city)
    state = _state_in(location)
    if state:
        # "KA, IN" with an advert naming Chennai and Bengaluru: the city in the state wins.
        in_state = [c for c in found if c in _STATE_OF.get(state, ())]
        found = in_state + [c for c in found if c not in in_state]
    return found


def job_cities(location: str | None, jd_text: str | None) -> list[str]:
    """Candidate cities for a job, best first."""
    found = cities_in(location)
    if found:
        return found
    place = _indian_place(location or "")
    if place:
        return [place]
    if _names_no_place(location) or any(
        _INDIA_SEGMENT.search(seg) and _names_no_place(seg) for seg in _segments(location or "")
    ):
        # No place named, or India offered without a city ("San Francisco / India").
        return _advert_cities(jd_text, location)
    return []


def job_city(location: str | None, jd_text: str | None, home: str | None) -> str | None:
    """The city to show, or None to keep the profile location."""
    found = job_cities(location, jd_text)
    if not found or city_of(home) in found:
        return None
    return found[0]


def add_months(day: date, months: int) -> date:
    """``day`` moved by whole calendar months, clamped to the last day of the month
    (31 Jan + 1 month is 28 or 29 Feb)."""
    m = day.month - 1 + months
    year, month = day.year + m // 12, m % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def header_line(city: str, today: date, months: int = 1) -> str:
    by = add_months(today, months)
    return f"Relocating to {city} by {by.day} {by:%b %Y}, willing to relocate sooner if required."


# Arial advance widths in thousandths of an em, for the characters a header line
# can hold. The template sets the location line in Arial 12 across 6.5in (468pt).
_ARIAL = dict(zip(
    "abcdefghijklmnopqrstuvwxyz",
    (556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333,
     500, 278, 556, 500, 722, 500, 500, 500),
)) | dict(zip(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    (667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722,
     667, 611, 722, 667, 944, 667, 667, 611),
)) | {" ": 278, ",": 278, ".": 278, "-": 333, "'": 191}
_LINE_PT = 468.0
_FONT_PT = 12.0


def fits_one_line(line: str) -> bool:
    """True when ``line`` stays on one header row in Arial 12."""
    return sum(_ARIAL.get(ch, 556) for ch in line) / 1000 * _FONT_PT <= _LINE_PT


def enabled() -> bool:
    cfg = settings.get("relocation") or {}
    return bool(cfg.get("enabled", False))


def _months() -> int:
    cfg = settings.get("relocation") or {}
    return int(cfg.get("months", 1))


def _home_location() -> str | None:
    try:
        data = json.loads((_ROOT / "master_profile.json").read_text())
    except (OSError, ValueError):
        return None
    return (data.get("personal") or {}).get("location")


def printed_location(location: str | None, jd_text: str | None,
                     today: date | None = None) -> str | None:
    """What the resume header will show, for the match views (Telegram, dashboard,
    CSV, tailor report); None while ``relocation`` is off, when the header never
    changes and showing it would only repeat the profile location."""
    if not enabled():
        return None
    return header_location_for(location, jd_text, today) or _home_location()


def header_location_for(location: str | None, jd_text: str | None,
                        today: date | None = None) -> str | None:
    """The header line for a job, or None when the header should stay as it is.

    The date is counted from today, the day the resume is built.
    """
    if not enabled():
        return None
    city = job_city(location, jd_text, _home_location())
    if city is None:
        return None
    line = header_line(city, today or date.today(), _months())
    # A line that wraps would push the whole page down a row: keep the profile location.
    return line if fits_one_line(line) else None
