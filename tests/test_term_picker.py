"""The search-term menu shown by `python -m src.main` in a terminal."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.cli.term_picker import ROTATION, Cancelled, Menu, _numbered_prompt

TERMS = ["backend engineer", "junior backend engineer", "data engineer", "junior data engineer"]


def test_the_rotation_is_first_and_preselected():
    menu = Menu(TERMS, ["data engineer"])
    assert menu.options()[0] == ("Next in rotation: data engineer", ROTATION)
    assert menu.choice() == ROTATION  # Enter = the old behaviour


def test_arrows_move_and_stop_at_the_ends():
    menu = Menu(TERMS, ["x"])
    menu.move(-1)
    assert menu.cursor == 0
    menu.move(2)
    assert menu.choice() == "junior backend engineer"
    menu.move(100)
    assert menu.choice() == "junior data engineer"


def test_typing_filters_case_insensitively_and_hides_the_rotation_row():
    menu = Menu(TERMS, ["x"])
    for ch in "JUNIOR D":
        menu.type(ch)
    assert [v for _, v in menu.options()] == ["junior data engineer"]
    assert menu.choice() == "junior data engineer"


def test_backspace_widens_the_filter_again():
    menu = Menu(TERMS, ["x"])
    for ch in "data":
        menu.type(ch)
    menu.backspace()
    menu.backspace()
    menu.backspace()
    menu.backspace()
    assert menu.options()[0][1] == ROTATION
    assert len(menu.options()) == len(TERMS) + 1


def test_a_filter_matching_nothing_has_no_choice():
    menu = Menu(TERMS, ["x"])
    for ch in "zzz":
        menu.type(ch)
    assert menu.options() == [] and menu.choice() is None


def test_duplicate_terms_are_listed_once():
    assert len(Menu(TERMS + TERMS, ["x"]).options()) == len(TERMS) + 1


@pytest.mark.parametrize("typed,expected", [("", ROTATION), ("0", ROTATION), ("3", "data engineer")])
def test_numbered_fallback(typed, expected):
    with patch("builtins.input", return_value=typed), patch("builtins.print"):
        assert _numbered_prompt(Menu(TERMS, ["x"])) == expected


@pytest.mark.parametrize("typed", ["99", "abc"])
def test_numbered_fallback_cancels_on_bad_input(typed):
    with patch("builtins.input", return_value=typed), patch("builtins.print"):
        with pytest.raises(Cancelled):
            _numbered_prompt(Menu(TERMS, ["x"]))


# ---------------------------------------------------------------------------
# src.main wiring
# ---------------------------------------------------------------------------

@pytest.fixture
def main_mod(monkeypatch, tmp_path):
    from src import main as m

    monkeypatch.setattr(m, "_ROOT", tmp_path)
    monkeypatch.setattr(m, "_configure_logging", lambda: None)
    calls = []
    monkeypatch.setattr(m, "_run", lambda dry, log, manual_terms=None: calls.append(manual_terms) or 0)
    return m, calls


def test_term_flag_skips_the_menu(main_mod, monkeypatch):
    m, calls = main_mod
    monkeypatch.setattr(m, "_interactive", lambda: True)
    monkeypatch.setattr(m, "_pick_terms", lambda: pytest.fail("menu shown"))
    m.main(["--term", "junior data engineer"])
    assert calls == [["junior data engineer"]]


def test_auto_and_non_interactive_runs_use_the_rotation(main_mod, monkeypatch):
    m, calls = main_mod
    monkeypatch.setattr(m, "_pick_terms", lambda: pytest.fail("menu shown"))
    monkeypatch.setattr(m, "_interactive", lambda: True)
    m.main(["--auto"])
    monkeypatch.setattr(m, "_interactive", lambda: False)
    m.main([])
    assert calls == [None, None]


def test_a_terminal_run_shows_the_menu(main_mod, monkeypatch):
    m, calls = main_mod
    monkeypatch.setattr(m, "_interactive", lambda: True)
    monkeypatch.setattr(m, "_pick_terms", lambda: ["data engineer"])
    m.main([])
    assert calls == [["data engineer"]]


def test_cancelling_the_menu_starts_no_run(main_mod, monkeypatch):
    m, calls = main_mod
    monkeypatch.setattr(m, "_interactive", lambda: True)

    def cancel():
        raise Cancelled

    monkeypatch.setattr(m, "_pick_terms", cancel)
    assert m.main([]) == 0
    assert calls == []
