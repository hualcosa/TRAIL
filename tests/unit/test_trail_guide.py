"""The shipped example's tools, and the ground truth its output gate checks.

The tools read this repository, so these tests are about the *live* repository
rather than a fixture. That is on purpose: the guide's whole claim is that it
answers from the documents actually present, and a test against a synthetic
corpus would pass on a checkout where those documents had been deleted.

The one to watch is ``known_identifiers``. It is derived from ``Settings``'
own fields, which is what makes ``no_fabricated_ids`` cheap and un-staleable —
and what would silently turn that guard into a rubber stamp if the derivation
ever broke and returned an empty set.
"""

from __future__ import annotations

import pytest

from examples.trail_guide.tools import known_identifiers, search_docs, stack_status

pytestmark = pytest.mark.unit


def test_search_finds_a_documented_phrase() -> None:
    result = search_docs("Traced Runtime")
    assert "README.md:" in result


def test_a_hit_carries_its_file_and_line() -> None:
    """Citations are the guide's contract; a passage with no source is a rumour."""
    first = search_docs("guardrail").splitlines()[0]
    file, _, line = first.partition(":")
    assert file.endswith(".md")
    assert line.isdigit()


def test_a_miss_says_so_rather_than_returning_nothing() -> None:
    """Silence reads as a tool failure; a sentence reads as an answer.

    The model is told to say a thing is undocumented when the search comes back
    empty, and it can only do that if the emptiness arrives as words.
    """
    result = search_docs("zzzznaoexistezzzz")
    assert "Nada encontrado" in result


def test_a_query_of_only_short_words_is_rejected() -> None:
    assert "vazia" in search_docs("a de o")


def test_every_word_must_appear() -> None:
    """AND, not OR: an OR search returns the whole corpus for a two-word query."""
    assert "Nada encontrado" in search_docs("Traced zzzznaoexistezzzz")


def test_stack_status_lists_the_services_that_actually_run() -> None:
    listing = stack_status()
    for service in ("agent", "postgres", "ui"):
        assert service in listing


def test_stack_status_reports_a_published_port() -> None:
    assert "porta" in stack_status()


# --------------------------------------------------------------------------
# the ground truth
# --------------------------------------------------------------------------


def test_known_identifiers_is_never_empty() -> None:
    """An empty set turns the fabrication guard into a rubber stamp.

    Every ``TRAIL_*`` name would then be unknown and every answer blocked —
    which is loud. The dangerous inverse is a *partial* set, so the two tests
    below check both ends rather than only that it has contents.
    """
    assert known_identifiers()


def test_every_setting_is_known() -> None:
    from trail.config import Settings

    known = known_identifiers()
    for field in Settings.model_fields:
        assert f"TRAIL_{field.upper()}" in known


def test_the_compose_services_are_known() -> None:
    assert {"agent", "postgres"} <= known_identifiers()


def test_an_invented_name_is_not_known() -> None:
    assert "TRAIL_TURBO_MODE" not in known_identifiers()
