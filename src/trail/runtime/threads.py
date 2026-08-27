"""The conversation index: which threads exist, and what to call them.

LangGraph's checkpointer knows a thread's *contents* but offers no way to
enumerate threads, so a sidebar needs an index. This is it, and it is
deliberately built on the ``store`` — the cross-thread slot that has been open
since the runtime was written and unused until now — rather than on a table of
our own or a query against ``checkpoints``.

Not a table of our own, because the argument that emptied ``db/schema.sql``
still holds: a second store means a second thing to migrate, back up and
disagree with itself.

Not a query against ``checkpoints``, which does hold every ``thread_id``,
because that schema is LangGraph's. The library creates and migrates it, which
is exactly why this repository declares none of it — and a query written
against someone else's versioned schema breaks on their next release, silently,
in production.

The index follows the same dial as everything else: with
``TRAIL_CHECKPOINTER=memory`` it lives in memory and dies with the process,
alongside the conversations it indexes. That is not a defect to hide — it is
the same trade, and :func:`list_threads` returns the flag that lets a client say
so instead of rendering an empty list that looks like a bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

#: Where thread records live in the store. A tuple, because store namespaces
#: are hierarchical and a future index (per user, per agent) nests under this
#: rather than colliding with it.
NAMESPACE = ("threads",)

#: How much of the first question becomes the title. Long enough to tell two
#: conversations apart in a narrow sidebar, short enough not to wrap twice.
TITLE_CHARS = 60

#: How many records one listing reads before filtering. See :func:`list_threads`
#: for why this is a scan rather than a page.
SCAN_LIMIT = 1_000


def title_from(message: str) -> str:
    """A thread title, from the first thing the person asked.

    No model call. Generating a title costs a turn's tokens to produce a string
    the question already is, and a generated title can be wrong about a
    conversation the user remembers perfectly well by its opening line.
    """
    single_line = " ".join(message.split())
    if len(single_line) <= TITLE_CHARS:
        return single_line
    return single_line[:TITLE_CHARS].rstrip() + "…"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ThreadSummary:
    """One row of the sidebar."""

    thread_id: str
    title: str
    turns: int
    created_at: str
    updated_at: str

    def as_json(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "title": self.title,
            "turns": self.turns,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


async def open_thread(store: Any, thread_id: str) -> None:
    """Record a thread that has been created but not yet spoken to.

    Written on creation so the record carries a real ``created_at``, and so an
    abandoned thread exists in the store to be counted. It does **not** reach
    the sidebar — see :func:`list_threads`.
    """
    if store is None:
        return
    now = _now()
    await store.aput(
        NAMESPACE,
        thread_id,
        {"title": "", "turns": 0, "created_at": now, "updated_at": now},
    )


async def record_turn(store: Any, thread_id: str, message: str) -> None:
    """Bump a thread's turn count, and title it from its first message.

    The title is set once and never rewritten: a conversation is remembered by
    how it opened, and a title that changes under the reader is a title they
    cannot use to find anything.
    """
    if store is None:
        return
    existing = await store.aget(NAMESPACE, thread_id)
    value: dict[str, Any] = dict(existing.value) if existing else {}
    turns = int(value.get("turns", 0)) + 1
    await store.aput(
        NAMESPACE,
        thread_id,
        {
            "title": value.get("title") or title_from(message),
            "turns": turns,
            "created_at": value.get("created_at") or _now(),
            "updated_at": _now(),
        },
    )


async def list_threads(
    store: Any, *, limit: int = 50, offset: int = 0
) -> list[ThreadSummary]:
    """Conversations, most recently used first. Threads with no turns are omitted.

    Sorted here rather than by the store, because ``asearch`` orders by
    relevance for a semantic query and by nothing in particular without one.
    Recency is the order a sidebar means.

    **The zero-turn filter is a correction, and the reasoning is worth keeping.**
    Indexing on creation was chosen so an abandoned thread would be visible as
    one — a real diagnostic, if abandonment were rare. It is not: the browser
    opens a thread on every page load and on every "new conversation" click, so
    within an afternoon a third of the list was threads nobody had spoken to.
    A sidebar full of empty rows is not a diagnostic, it is a sidebar nobody can
    use.

    The records still exist and still carry their timestamps, so "how many
    threads were opened and never used" remains answerable. It is a metric, and
    a metric does not belong in a navigation list.
    """
    if store is None:
        return []
    # Read a bounded window and filter it here, rather than asking the store for
    # `limit` rows and hoping enough survive. The store cannot filter on a value
    # it does not index, so a page of records can be a page of nothing — twenty
    # abandoned threads ahead of one real conversation would return an empty
    # first page, which reads as "you have no conversations".
    #
    # Over-fetching by a multiple only moves that cliff; it does not remove it.
    # So: one bounded read, filtered, then paged.
    #
    # This means the index is not truly paged, and for a local scaffold with a
    # sidebar that is the right trade — `SCAN_LIMIT` rows is more conversations
    # than one demo produces, and the alternative is a loop that reads until it
    # has enough, which is real paging complexity bought for a case nobody has.
    # When someone does: index `turns` in the store and filter there.
    items = await store.asearch(NAMESPACE, limit=SCAN_LIMIT)
    summaries = [
        ThreadSummary(
            thread_id=item.key,
            title=str(item.value.get("title") or "").strip(),
            turns=int(item.value.get("turns", 0)),
            created_at=str(item.value.get("created_at") or ""),
            updated_at=str(item.value.get("updated_at") or ""),
        )
        for item in items
        if int(item.value.get("turns", 0)) > 0
    ]
    summaries.sort(key=lambda summary: summary.updated_at, reverse=True)
    return summaries[offset : offset + limit]


async def forget(store: Any, thread_id: str) -> None:
    """Drop a thread from the index.

    The conversation itself stays in the checkpointer. This removes it from the
    list, which is what "delete" means to someone tidying a sidebar — and it
    keeps this module from reaching into storage it does not own to do
    something irreversible.
    """
    if store is None:
        return
    await store.adelete(NAMESPACE, thread_id)
