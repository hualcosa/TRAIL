"""The conversation index, over an in-memory store — offline, no database.

Testing this against ``InMemoryStore`` rather than Postgres is the point of the
store being a swappable slot: the index's logic is the same object either way,
so the behaviour worth asserting is assertable in milliseconds.
"""

from __future__ import annotations

import pytest
from langgraph.store.memory import InMemoryStore

from trail.runtime.threads import (
    TITLE_CHARS,
    forget,
    list_threads,
    open_thread,
    record_turn,
    title_from,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


# --------------------------------------------------------------------------
# titles
# --------------------------------------------------------------------------


def test_a_short_question_is_its_own_title() -> None:
    assert title_from("o que é o TRAIL?") == "o que é o TRAIL?"


def test_a_long_question_is_cut_and_marked() -> None:
    title = title_from("a" * 200)
    assert len(title) <= TITLE_CHARS + 1
    assert title.endswith("…")


def test_a_multiline_question_becomes_one_line() -> None:
    """A sidebar row is one line; a title with a newline breaks the layout."""
    assert "\n" not in title_from("primeira linha\nsegunda linha")


# --------------------------------------------------------------------------
# recording
# --------------------------------------------------------------------------


async def test_an_opened_thread_is_recorded_but_not_listed(
    store: InMemoryStore,
) -> None:
    """A thread nobody spoke to is a record, not a row.

    This inverts an earlier decision, and the reason is worth keeping. Listing
    abandoned threads was chosen as a diagnostic — it would show a client whose
    every turn was failing. It only works if abandonment is rare, and it is not:
    the browser opens a thread on every page load and on every "new
    conversation" click, so within an afternoon a third of the list was threads
    nobody had spoken to.

    The record survives with its timestamps, so the diagnostic is still
    answerable. It is a metric, and a metric does not belong in a navigation
    list.
    """
    await open_thread(store, "t1")
    assert await list_threads(store) == []

    stored = await store.aget(("threads",), "t1")
    assert stored is not None, "the record must survive, only the row is dropped"
    assert stored.value["turns"] == 0
    assert stored.value["created_at"]


async def test_the_first_turn_promotes_a_thread_into_the_list(
    store: InMemoryStore,
) -> None:
    await open_thread(store, "t1")
    await record_turn(store, "t1", "oi")
    assert [t.thread_id for t in await list_threads(store)] == ["t1"]


async def test_a_page_of_empty_threads_does_not_hide_the_real_ones(
    store: InMemoryStore,
) -> None:
    """The filter runs after the store's own paging.

    Twenty abandoned threads ahead of one real conversation must not produce an
    empty first page — which is what a naive `limit` on the store would do.
    """
    for index in range(20):
        await open_thread(store, f"empty-{index}")
    await record_turn(store, "real", "oi")

    listed = await list_threads(store, limit=5)
    assert [t.thread_id for t in listed] == ["real"]


async def test_the_first_message_becomes_the_title(store: InMemoryStore) -> None:
    await open_thread(store, "t1")
    await record_turn(store, "t1", "quais serviços sobem?")
    assert (await list_threads(store))[0].title == "quais serviços sobem?"


async def test_the_title_is_not_rewritten_by_later_turns(
    store: InMemoryStore,
) -> None:
    """A conversation is remembered by how it opened.

    A title that changes under the reader is a title they cannot use to find
    anything.
    """
    await record_turn(store, "t1", "primeira pergunta")
    await record_turn(store, "t1", "segunda pergunta")
    assert (await list_threads(store))[0].title == "primeira pergunta"


async def test_turns_accumulate(store: InMemoryStore) -> None:
    for _ in range(3):
        await record_turn(store, "t1", "oi")
    assert (await list_threads(store))[0].turns == 3


async def test_recording_a_turn_on_an_unopened_thread_still_indexes_it(
    store: InMemoryStore,
) -> None:
    """The index must not depend on open_thread having run.

    A thread resumed after a restart, or created by a client that skipped the
    open call, is still a thread someone will look for in the sidebar.
    """
    await record_turn(store, "t-unknown", "oi")
    assert [t.thread_id for t in await list_threads(store)] == ["t-unknown"]


# --------------------------------------------------------------------------
# listing
# --------------------------------------------------------------------------


async def test_threads_are_listed_most_recently_used_first(
    store: InMemoryStore,
) -> None:
    """Recency is the order a sidebar means.

    The store orders by relevance for a semantic query and by nothing in
    particular without one, so the sort belongs here.
    """
    await record_turn(store, "old", "primeira")
    await record_turn(store, "new", "segunda")
    await record_turn(store, "old", "terceira")  # old becomes the most recent

    assert [t.thread_id for t in await list_threads(store)] == ["old", "new"]


async def test_an_empty_index_lists_nothing_rather_than_failing(
    store: InMemoryStore,
) -> None:
    assert await list_threads(store) == []


async def test_forget_removes_a_thread_from_the_list(store: InMemoryStore) -> None:
    await record_turn(store, "t1", "oi")
    await record_turn(store, "t2", "olá")
    await forget(store, "t1")
    assert [t.thread_id for t in await list_threads(store)] == ["t2"]


async def test_every_summary_serialises(store: InMemoryStore) -> None:
    """The shape the wire depends on."""
    await record_turn(store, "t1", "oi")
    payload = (await list_threads(store))[0].as_json()
    assert set(payload) == {
        "thread_id",
        "title",
        "turns",
        "created_at",
        "updated_at",
    }


# --------------------------------------------------------------------------
# the no-store case
# --------------------------------------------------------------------------


async def test_every_operation_is_a_no_op_without_a_store() -> None:
    """An agent built with no persistence must still run.

    ``build_agent`` accepts ``persistence=None`` — the unit tier uses it — and a
    thread index that raised there would make the index a hard dependency of a
    runtime that deliberately does not have one.
    """
    await open_thread(None, "t1")
    await record_turn(None, "t1", "oi")
    await forget(None, "t1")
    assert await list_threads(None) == []
