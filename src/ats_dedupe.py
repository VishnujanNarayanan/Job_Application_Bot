"""Duplicate ATS matches (#186).

Of 1,736 ATS matches sent to 2026-11-05, 613 repeated another sent match
(same company, same title). Four kinds:

1. The same requisition, reached twice (Goldman on its own site and on three
   Oracle sites with one requisition id; one Oracle job on two sites; a board
   stored twice in different case). Applying to one is applying to all.
   Dropped before parsing (:func:`repeat_requisitions`).
2. Siblings, other city: one role, one description, an opening per city.
3. Siblings, same city: several openings for one batch hire (Amazon posts
   identical requisitions with consecutive ids).
   Kinds 2 and 3 become one message per role (:func:`cluster`,
   :func:`find_parent`, :func:`sibling_lines`).
4. Same title, different job (Accenture's "Custom Software Engineer" names a
   different skill each time; ~11% shared text). Left alone.

Kinds 2/3 are told from 4 by the description: siblings share it word for
word once their city names are taken out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

# Siblings (kinds 2/3) have the same description word for word, their own
# city names aside. A near-match is not enough: Accenture posts one template
# that differs only in "Must have skills: Machine Learning" vs "Machine
# Learning Operations", or "minimum 2 years" vs "6 years" (near-identical
# pairs, 2026-11-05), and those are different jobs. Exact: 387 of the 613
# sent copies fold into their role's message; a 0.95 overlap would fold 439
# but hide some of those different jobs.

# How long an earlier match collects late siblings: the latest seen arrived
# 17.8 days after the first. Past this a copy is a re-post or a new round.
SIBLING_DAYS = 30
# Kind 1: how far back a requisition counts as already seen.
REQUISITION_DAYS = 60
# Links listed in a grouped message before "+N more".
MAX_LISTED = 12


def base_requisition(posting_id: str | None) -> str:
    """Oracle lists one requisition several times as "153353", "153353_3"."""
    return re.sub(r"_\d+$", "", (posting_id or "").strip()).lower()


def norm_title(title: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def norm_location(location: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (location or "").lower()).strip()


@lru_cache(maxsize=4096)
def _fingerprint(text: str | None, location: str | None) -> frozenset:
    """The description's word 3-grams, its own city names left out: "Pune"
    in one posting and "Ahmedabad" in its sibling must not make them differ."""
    drop = set(re.findall(r"[a-z]{3,}", (location or "").lower()))
    words = [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in drop]
    return frozenset(tuple(words[i:i + 3]) for i in range(max(0, len(words) - 2)))


def similarity(a_text: str | None, a_loc: str | None, b_text: str | None, b_loc: str | None) -> float:
    """Overlap (Jaccard) of the two descriptions' word 3-grams (for reports;
    siblings need 1.0, :func:`are_siblings`)."""
    a, b = _fingerprint(a_text, a_loc), _fingerprint(b_text, b_loc)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def are_siblings(a_text, a_loc, b_text, b_loc) -> bool:
    """The same description word for word, each one's own city aside."""
    a = _fingerprint(a_text, a_loc)
    return bool(a) and a == _fingerprint(b_text, b_loc)


def group_key(company_key: str, title: str | None) -> tuple[str, str]:
    return (company_key, norm_title(title))


@dataclass
class Cluster:
    """One role: the job that is parsed, scored and sent (``head``) and its
    siblings, which share its verdict and its message."""
    head: object
    siblings: list = field(default_factory=list)


def cluster(jobs: list, key) -> list[Cluster]:
    """Group a run's jobs into roles, keeping order: each job joins the first
    earlier cluster with the same ``key(job)`` (company key, title) whose head
    it is a sibling of, else starts its own. Kind 4 (same title, different
    description) stays apart."""
    out: list[Cluster] = []
    by_key: dict = {}
    for job in jobs:
        for c in by_key.get(key(job), []):
            if are_siblings(c.head.jd_text, c.head.location, job.jd_text, job.location):
                c.siblings.append(job)
                break
        else:
            c = Cluster(job)
            out.append(c)
            by_key.setdefault(key(job), []).append(c)
    return out


def is_new_city(location: str | None, known: list[str | None]) -> bool:
    """Kind 2 (another city) rather than kind 3 (another opening, same city)."""
    return norm_location(location) not in {norm_location(k) for k in known}


def _md(text: str | None) -> str:
    """Safe inside Telegram's legacy Markdown: no * _ ` [ ]."""
    return re.sub(r"[*_`\[\]]", "", text or "").strip()


def sibling_lines(head_location: str | None, siblings: list[tuple[str | None, str | None]]) -> list[str]:
    """The lines a grouped message adds for its siblings: ``siblings`` is
    (location, apply url) of each other opening.

    "Also open in: [Hyderabad](..) · [Mumbai](..)" for other cities, and
    "3 more openings in Bengaluru: [1](..) [2](..) [3](..)" for the same city.
    """
    if not siblings:
        return []
    other = [(loc, url) for loc, url in siblings if is_new_city(loc, [head_location])]
    same = [(loc, url) for loc, url in siblings if not is_new_city(loc, [head_location])]

    def link(label: str, url: str | None) -> str:
        label = _md(label) or "link"
        # A ")" ends a Markdown link early; such a url is listed by name only.
        return f"[{label}]({url})" if url and ")" not in url and url.startswith("http") else label

    lines = []
    if same:
        listed = " ".join(link(str(i), url) for i, (_l, url) in enumerate(same[:MAX_LISTED], 1))
        more = f" +{len(same) - MAX_LISTED} more" if len(same) > MAX_LISTED else ""
        where = f" in {_md(head_location)}" if head_location else ""
        lines.append(f"{len(same)} more opening{'s' if len(same) > 1 else ''}{where}: {listed}{more}")
    if other:
        listed = " · ".join(link(loc or "other", url) for loc, url in other[:MAX_LISTED])
        more = f" +{len(other) - MAX_LISTED} more" if len(other) > MAX_LISTED else ""
        lines.append(f"Also open in: {listed}{more}")
    return lines


# --- Kind 1: the same requisition, reached twice ------------------------------------

def repeat_requisitions(session, candidates: list, now, *, days: int = REQUISITION_DAYS) -> dict[str, str]:
    """{job_id: job_id of the first copy} for candidates whose requisition was
    already seen: the same company key, requisition id and title, on another
    board or site, in the last ``days`` days, or earlier in ``candidates``.
    ``candidates``: (posting, board) pairs, in the order they are sent on.

    The title is compared too, so two companies that share a key, or one
    company's unrelated id spaces (Greenhouse numbers, Lever uuids), never
    collide."""
    from datetime import timedelta

    from sqlalchemy import text

    if not candidates:
        return {}

    def ident(p, board) -> tuple:
        return (board.company_key or "", base_requisition(p.posting_id), norm_title(p.title))

    mine = {p.job_id for p, _b in candidates}
    keys = sorted({board.company_key for _p, board in candidates if board.company_key})
    seen: dict[tuple, str] = {}
    if keys:
        rows = session.execute(text("""
            SELECT b.company_key, p.posting_id, p.title, p.job_id
            FROM ats_postings p JOIN ats_boards b ON b.ats = p.ats AND b.board_id = p.board_id
            WHERE b.company_key = ANY(CAST(:keys AS text[])) AND p.skipped IS NULL
              AND p.first_seen_at >= :since
            ORDER BY p.first_seen_at"""), {"keys": keys, "since": now - timedelta(days=days)}).all()
        for key, pid, title, job_id in rows:
            if job_id not in mine:
                seen.setdefault((key, base_requisition(pid), norm_title(title)), job_id)
    out: dict[str, str] = {}
    for p, board in candidates:
        k = ident(p, board)
        if not k[0] or not k[1]:
            continue
        first = seen.get(k)
        if first and first != p.job_id:
            out[p.job_id] = first
        else:
            seen.setdefault(k, p.job_id)
    return out
