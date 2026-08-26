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

    Written on creation rather than on the first turn so that an abandoned
    thread is visible as an abandoned thread. The alternative — indexing only
    what has been used — quietly hides the case where every turn is failing.
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
    """Threads, most recently used first.

    Sorted here rather than by the store, because ``asearch`` orders by
    relevance for a semantic query and by nothing in particular without one.
    Recency is the order a sidebar means.
    """
    if store is None:
        return []
    items = await store.asearch(NAMESPACE, limit=limit, offset=offset)
    summaries = [
        ThreadSummary(
            thread_id=item.key,
            title=str(item.value.get("title") or "").strip(),
            turns=int(item.value.get("turns", 0)),
            created_at=str(item.value.get("created_at") or ""),
            updated_at=str(item.value.get("updated_at") or ""),
        )
        for item in items
    ]
    summaries.sort(key=lambda summary: summary.updated_at, reverse=True)
    return summaries


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
