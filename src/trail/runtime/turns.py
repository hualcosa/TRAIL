"""One turn, as a sequence of frames.

This is the single implementation of "run the agent and narrate what happened",
and both endpoints drain it: the streaming one renders each frame as it
arrives, the JSON one keeps the last. Two endpoints over one generator is what
stops the streamed answer and the buffered answer from being able to disagree.

The frame vocabulary is ``events.py``'s. What this module adds is the order:

1. ``stage`` frames from the graph's ``custom`` channel, as they happen — in
   pipeline order, including the skips for gates the dial left out, which the
   trace middleware emits from the hook where each would have run. Arrival
   order is the true order, so a client renders the rail without sorting it.
2. one ``turn`` frame with the finished answer.
3. one ``error`` frame, if something failed.
4. one ``trace`` frame, always last, even after an error — a failed turn is the
   one you most want the trace link for.

Two telemetry invariants, both of which fail silently when broken:
``current_trace_id()`` must be read *inside* the span, and ``flush_telemetry()``
must be awaited *after* it closes. An unended span is not exportable, and a
trace id read outside one is ``None``.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

from trail.config import Settings
from trail.runtime.events import StageEvent, ms_since, stage_from_chunk
from trail.telemetry import current_trace_id, flush_telemetry, span, trace_url

#: Frame names that cross the wire. ``turn`` carries the answer; ``error``
#: carries the exception object rather than a rendered payload, so the JSON
#: endpoint can re-raise it and answer 400/409/502 while the streaming endpoint
#: renders it — a response whose body has already begun cannot become a 500.
STAGE = "stage"
TURN = "turn"
ERROR = "error"
TRACE = "trace"

Frame = tuple[str, Any]


def _final_text(messages: list[Any]) -> str:
    """The last assistant message's text, or empty if there is none."""
    for message in reversed(messages):
        if getattr(message, "type", None) != "ai":
            continue
        content = message.content
        if isinstance(content, str):
            return content
        return " ".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


async def run_turn(
    agent: Any,
    *,
    thread_id: str,
    message: str,
    settings: Settings,
) -> AsyncIterator[Frame]:
    """Drive one turn and yield its frames.

    ``stream_mode`` asks for three channels and uses two of them today.
    ``custom`` is the rail. ``values`` is how the finished message is recovered
    without a second read of the checkpointer. ``messages`` is requested even
    though nothing consumes it yet: it is the token channel, and asking for it
    now means adding token streaming later is a change to the client and not to
    this contract.
    """
    started = time.perf_counter()
    config = {"configurable": {"thread_id": thread_id}}
    failure: BaseException | None = None
    trace_id: str | None = None
    final: list[Any] = []

    with span("trail.turn", **{"trail.thread_id": thread_id}):
        trace_id = current_trace_id()
        try:
            async for mode, chunk in agent.astream(
                {"messages": [{"role": "user", "content": message}]},
                config,
                stream_mode=["custom", "values", "messages"],
            ):
                if mode == "custom":
                    event = stage_from_chunk(chunk)
                    if event is not None:
                        yield STAGE, event.model_dump(mode="json")
                elif mode == "values" and isinstance(chunk, dict):
                    final = chunk.get("messages") or final
        except Exception as exc:
            # Caught, not swallowed: it leaves as an `error` frame so the
            # streaming endpoint can report it in a body that has already begun.
            failure = exc

    # After the span closes, never inside it: an unended span cannot be
    # exported, so flushing early ships an incomplete trace or none at all.
    await flush_telemetry()

    if failure is None:
        yield (
            TURN,
            {
                "thread_id": thread_id,
                "text": _final_text(final),
                "ms": ms_since(started),
            },
        )
    else:
        yield ERROR, failure

    yield TRACE, {"trace_id": trace_id, "trace_url": trace_url(trace_id)}


def greeting_frames(thread_id: str, text: str) -> list[Frame]:
    """The opening line, as frames, with no model call behind it.

    A greeting is not an answer and charging a turn's tokens for one would put
    a cost on the scoreboard that bought nothing. It is emitted as a ``turn``
    frame with ``ms`` absent rather than zero, for the same reason an unpriced
    model costs ``None``: a measurement that was never taken is not a
    measurement of zero.
    """
    if not text:
        return []
    return [
        (
            STAGE,
            StageEvent(
                name="greeting", kind="io", label="abertura", status="done"
            ).model_dump(mode="json"),
        ),
        (TURN, {"thread_id": thread_id, "text": text, "ms": None}),
    ]
