"""CLI — arrow-key menu for choosing the search term a run uses.

Shown by ``python -m src.main`` when it runs in a terminal. The first option
is the rotation's next term, already highlighted, so Enter does exactly what
a run did before the menu existed. Picking any other term searches just that
term and leaves the rotation where it was.

    ↑/↓ PgUp/PgDn Home/End   move
    type                     filter the list
    Backspace                edit the filter
    Enter                    run with the highlighted term
    Esc / Ctrl+C             cancel the run

``Menu`` holds all the behaviour and knows nothing about the terminal, so it
can be tested directly; ``pick_term`` draws it with curses.
"""

from __future__ import annotations

import curses
from collections.abc import Sequence

# Returned by pick_term when the operator keeps the rotation's choice.
ROTATION = "<rotation>"


class Cancelled(Exception):
    """The operator backed out of the menu; the run should not start."""


class Menu:
    """The picker's state: a filter, a list of options, a highlighted row."""

    def __init__(self, terms: Sequence[str], rotation_terms: Sequence[str]):
        self.terms = list(dict.fromkeys(terms))
        self.rotation_label = "Next in rotation: " + ", ".join(rotation_terms)
        self.query = ""
        self.cursor = 0

    def options(self) -> list[tuple[str, str]]:
        """``(label, value)`` rows matching the filter, rotation first."""
        q = self.query.casefold()
        rows = [(self.rotation_label, ROTATION)] if not q else []
        rows += [(t, t) for t in self.terms if q in t.casefold()]
        return rows

    def move(self, step: int) -> None:
        count = len(self.options())
        self.cursor = max(0, min(self.cursor + step, count - 1)) if count else 0

    def type(self, char: str) -> None:
        self.query += char
        self.cursor = 0

    def backspace(self) -> None:
        self.query = self.query[:-1]
        self.cursor = 0

    def choice(self) -> str | None:
        """The highlighted row's value, or None when the filter matches nothing."""
        rows = self.options()
        return rows[self.cursor][1] if rows else None


def pick_term(terms: Sequence[str], rotation_terms: Sequence[str]) -> str:
    """Show the menu; return a term, or ``ROTATION``. Raises ``Cancelled``."""
    menu = Menu(terms, rotation_terms)
    try:
        return curses.wrapper(_loop, menu)
    except curses.error:
        # No usable terminal capabilities (odd TERM, tiny window): fall back
        # to a numbered prompt rather than refusing to run.
        return _numbered_prompt(menu)
    except KeyboardInterrupt:
        raise Cancelled from None


def _loop(screen, menu: Menu) -> str:
    curses.curs_set(0)
    # Esc is also the first byte of every arrow-key sequence, so curses waits
    # to tell them apart -- a full second by default, which makes Esc feel
    # broken. 25 ms is plenty for a local terminal.
    curses.set_escdelay(25)
    screen.keypad(True)
    while True:
        _draw(screen, menu)
        key = screen.get_wch()
        if key in (curses.KEY_UP,):
            menu.move(-1)
        elif key in (curses.KEY_DOWN,):
            menu.move(1)
        elif key == curses.KEY_PPAGE:
            menu.move(-_page(screen))
        elif key == curses.KEY_NPAGE:
            menu.move(_page(screen))
        elif key == curses.KEY_HOME:
            menu.move(-len(menu.options()))
        elif key == curses.KEY_END:
            menu.move(len(menu.options()))
        elif key in (curses.KEY_ENTER, "\n", "\r"):
            choice = menu.choice()
            if choice is not None:
                return choice
        elif key == "\x1b":
            raise Cancelled
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            menu.backspace()
        elif isinstance(key, str) and key.isprintable():
            menu.type(key)


def _page(screen) -> int:
    return max(1, screen.getmaxyx()[0] - 5)


def _draw(screen, menu: Menu) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    put = lambda y, text, attr=0: screen.addnstr(y, 0, text, max(0, width - 1), attr)  # noqa: E731

    put(0, "Pick a search term   ↑/↓ move · type to filter · Enter run · Esc cancel",
        curses.A_BOLD)
    put(1, f"Filter: {menu.query}")
    rows = menu.options()
    visible = max(1, height - 3)
    top = max(0, menu.cursor - visible + 1)  # scroll just enough to show the cursor
    if not rows:
        put(3, "  (no term matches the filter)")
    for i, (label, _value) in enumerate(rows[top:top + visible]):
        index = top + i
        marker = "▶ " if index == menu.cursor else "  "
        put(3 + i, marker + label, curses.A_REVERSE if index == menu.cursor else 0)
    screen.refresh()


def _numbered_prompt(menu: Menu) -> str:
    rows = menu.options()
    for i, (label, _value) in enumerate(rows):
        print(f"{i:3d}. {label}")
    try:
        raw = input("Number to run (Enter = 0, rotation): ").strip()
    except (EOFError, KeyboardInterrupt):
        raise Cancelled from None
    if not raw:
        return ROTATION
    if not raw.isdigit() or int(raw) >= len(rows):
        raise Cancelled
    return rows[int(raw)][1]
