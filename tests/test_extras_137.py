"""Extra bullets capped per entry, and a main exchanged for thin extras (#137)."""

from src.scorer.selector import select_entry_bullets
from tests.test_iteration_2_scorer import NOW, _block, _bullet, _cfg, _entry, _jd, _kw


def _run(bullets, *tokens, **cfg):
    with _cfg(**{"min_per_entry": 1, "max_extras": 2, "main_exchange": True, **cfg}):
        out = select_entry_bullets(_entry(blocks=[_block(bullets=bullets)]), _jd(), _kw(*tokens), now=NOW)
    return [b.id for b in out.bullets], out


SOFT = ["Communication", "Mentoring", "Data Quality", "Documentation"]


def _thin_extras(words):
    return [_bullet(f"x{i}", f"Practised {w} with the team every week.", extra=True)
            for i, w in enumerate(words, 1)]


def test_no_more_than_two_extras_carrying_methods_or_soft_skills():
    bullets = [_bullet("b0", "Summary with Python.", summary=True)] + _thin_extras(SOFT)
    ids, _ = _run(bullets, "Python", *SOFT)
    assert sum(i.startswith("x") for i in ids) == 2


def test_a_third_extra_renders_when_it_adds_a_tool():
    bullets = ([_bullet("b0", "Summary with Python.", summary=True)] + _thin_extras(SOFT[:2])
               + [_bullet("x9", "Ran the queue on Kafka.", extra=True)])
    ids, _ = _run(bullets, "Python", *SOFT[:2], "Kafka")
    assert {"x1", "x2", "x9"} <= set(ids)


def test_the_two_extras_kept_are_the_strongest():
    bullets = [
        _bullet("b0", "Summary with Python.", summary=True),
        _bullet("x1", "Practised Communication weekly.", extra=True),
        _bullet("x2", "Kept Data Quality and Documentation tidy.", extra=True),
        _bullet("x3", "Ran Mentoring and Version Control sessions.", extra=True),
    ]
    ids, _ = _run(bullets, "Python", "Communication", "Data Quality", "Documentation",
                  "Mentoring", "Version Control")
    assert set(ids) == {"b0", "x2", "x3"}, "the two-keyword extras beat the one-keyword one"


def test_no_limit_when_switched_off():
    bullets = [_bullet("b0", "Summary with Python.", summary=True)] + _thin_extras(SOFT)
    ids, _ = _run(bullets, "Python", *SOFT, max_extras=0)
    assert sum(i.startswith("x") for i in ids) == 4


def test_the_limit_holds_in_phase_two():
    bullets = [_bullet("b0", "Summary with Python.", summary=True)] + _thin_extras(SOFT)
    with _cfg(min_per_entry=1, max_extras=2, main_exchange=True, qualification_fill=True):
        out = select_entry_bullets(
            _entry(blocks=[_block(bullets=bullets, checklist=("Data Engineer",))]), _jd(),
            _kw("Python", *SOFT), now=NOW)
    assert sum(b.id.startswith("x") for b in out.bullets) <= 2


# --- the exchange ------------------------------------------------------------

def test_a_denser_extra_is_not_exchanged_for_a_thinner_main():
    """The extra brings Regression and Kafka; the main carries only Regression."""
    bullets = [
        _bullet("b0", "Summary with Python and SQL.", summary=True),
        _bullet("b1", "Ran SQL checks and Regression tests."),
        _bullet("x1", "Ran Regression suites on Kafka topics.", extra=True),
    ]
    ids, _ = _run(bullets, "Python", "SQL", "Regression", "Kafka")
    assert "x1" in ids and "b1" not in ids


def test_one_main_replaces_every_extra_it_carries():
    bullets = [
        _bullet("b0", "Summary with Python and SQL.", summary=True),
        _bullet("b1", "Ran SQL checks with Regression tests and Kafka streams."),
        _bullet("x1", "Kept a Regression suite.", extra=True),
        _bullet("x2", "Read events from Kafka.", extra=True),
    ]
    ids, out = _run(bullets, "Python", "SQL", "Regression", "Kafka")
    assert ids == ["b0", "b1"]
    assert {"Regression", "Kafka"} <= out.covered


def test_a_main_repeating_two_shown_keywords_is_not_exchanged():
    bullets = [
        _bullet("b0", "Summary with Python and SQL.", summary=True),
        _bullet("b1", "Ran Python and SQL checks with Regression tests."),
        _bullet("x1", "Kept a Regression suite.", extra=True),
    ]
    ids, _ = _run(bullets, "Python", "SQL", "Regression")
    assert "x1" in ids and "b1" not in ids


def test_the_exchange_never_loses_a_keyword():
    """x1 and x2 both carry Docker, so neither alone contributes it; the main does
    not carry Docker, so removing both would lose it."""
    bullets = [
        _bullet("b0", "Summary with Python and SQL.", summary=True),
        _bullet("b1", "Ran SQL checks with Regression and Kafka."),
        _bullet("x1", "Kept a Regression suite in Docker.", extra=True),
        _bullet("x2", "Read Kafka events in Docker.", extra=True),
    ]
    _, out = _run(bullets, "Python", "SQL", "Regression", "Kafka", "Docker")
    assert "Docker" in out.covered


def test_the_summary_stays_first_after_an_exchange():
    bullets = [
        _bullet("b0", "Summary with Python and SQL.", summary=True),
        _bullet("b1", "Ran SQL checks and Regression tests on the warehouse."),
        _bullet("x1", "Kept a Regression suite on every branch.", extra=True),
    ]
    _, out = _run(bullets, "Python", "SQL", "Regression")
    assert out.bullets[0].id == "b0" and out.bullets[0].is_summary
