"""The agent HTTP service (INTERFACES §3).

Responsibilities, in the order a request meets them: hold the in-flight call,
ask the model to read one customer turn, hand the extraction to the state
machine, screen whatever the machine wants to say, persist the traces, and —
when the call ends — build the record and put it in the specialist queue.

Every outbound utterance passes
:func:`trail.agent.compliance.check_outbound_utterance` before it leaves, and
it is passed **this call's slot values** as well as the protocol. That second
half is the port's one structural addition: the approved set the gate compares
against is built by *rendering* the single customer-specific block from the
system of record, so an utterance carrying a balance that disagrees with the
record matches nothing and is refused before the words leave the service —
rather than being found later in a transcript review, which is where BLUEPRINT
§5's "wrong balance / fee / date spoken aloud" is otherwise discovered.

A violation is never dropped: it is logged at CRITICAL, added as an event on the
turn span, and the call is forced to ``transferred_to_human``. That last part is
the substantive one — a call whose output failed a safety assertion can never be
recorded as ``completed_no_callback``, so a compliance failure can never be
laundered into the primary metric.

**One turn, one implementation, two endpoints.** The pipeline above lives in
:func:`_turn_events`, an async generator that yields a frame per stage as the
stage completes. ``POST /calls/{id}/turns`` drains it and answers with the last
frame; ``POST /calls/{id}/turns/stream`` forwards every frame as a Server-Sent
Event so a demo UI can show the pipeline running rather than a spinner. There is
deliberately no second copy: two implementations of a turn would drift, and the
one that drifted would be the streamed one — the endpoint nobody's eval harness
drives, and therefore the endpoint whose divergence nothing would catch.

What streams is the **pipeline**, never the reply. The model writes no word the
customer hears (see :mod:`trail.agent.llm`), so there is no token stream to
relay: the agent's utterance is compliance-approved text that exists in full the
moment the state machine names it. A client that reveals it a character at a
time is doing a cosmetic animation over a finished string, and anything
describing this system should say so rather than let "streaming" imply the model
is composing.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from langgraph.graph.state import CompiledStateGraph
from opentelemetry.trace import Span

from trail.agent import compliance, machine
from trail.agent.llm import LLMClient
from trail.agent.machine import CallState, Turn, TurnOutcome, slots_for_call
from trail.cases import GOLDEN_SET, demo_profile
from trail.config import Settings, get_settings
from trail.db import (
    close_pool,
    get_call_record,
    init_pool,
    insert_call_record,
    insert_llm_call_trace,
    insert_turn_trace,
)
from trail.models import (
    CallRecord,
    LLMCallTrace,
    MarkUnreachableRequest,
    StartCallRequest,
    StartCallResponse,
    Step,
    TerminalState,
    TurnRequest,
    TurnResponse,
    TurnTrace,
)
from trail.protocol import Protocol, load_protocol
from trail.telemetry import (
    configure_logging,
    current_trace_id,
    flush_telemetry,
    setup_telemetry,
    span,
    trace_url,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Lifespan
# --------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Wire the protocol and the database pool, in that order.

    The protocol loads before the pool on purpose: ``load_protocol`` raises when
    the version header is missing, when any step lacks approved text, or when a
    slotted block declares a slot nobody renders, and a process that cannot say
    what it is approved to say must refuse to start rather than discover the gap
    mid-call.

    Telemetry is deliberately *not* set up here — see the call below the ``app``
    definition. ``FastAPIInstrumentor`` patches ``build_middleware_stack``,
    which Starlette calls on its way into the lifespan scope, so a call from in
    here is already too late and produces no HTTP server spans at all.
    """
    settings = get_settings()
    app.state.settings = settings
    app.state.protocol = load_protocol(settings.protocol_path)
    # The graph binds the approved text at compile time and holds every
    # in-flight call in its checkpointer, keyed by `call_id`. What it does not
    # bind is the slot values: those are per-call and are computed from the
    # profile in the state at the moment the utterance is said.
    app.state.graph = machine.build_graph(app.state.protocol)
    app.state.llm_client = LLMClient.from_settings(settings)
    await init_pool()

    logger.info(
        "agent ready: protocol_version=%s prompt_version=%s model=%s",
        app.state.protocol.version,
        settings.prompt_version,
        settings.model,
    )
    try:
        yield
    finally:
        await app.state.llm_client.aclose()
        await close_pool()


app = FastAPI(
    title="Banco Aurora — early-stage collections agent",
    version="1.0.0",
    summary=(
        "Inbound 1–30 days-past-due collections call: verification, disclosure, "
        "and promise capture."
    ),
    description=(
        "Labeled intended use is verification, disclosure and promise capture — "
        "not negotiation, not settlement, and not any assessment of the "
        "customer. The agent reads compliance-approved text verbatim, speaks "
        "amounts only through slots rendered from the system of record, records "
        "what the customer says, and routes every completed record to one "
        "collections-specialist queue in `started_at` order with no risk-based "
        "ordering, filtering, or prioritisation."
    ),
    lifespan=lifespan,
)

# Module scope, with the app, and both halves matter. Without the app the
# instrumentor can only swap the `fastapi.FastAPI` class, which does nothing for
# an app already built or for a module that did `from fastapi import FastAPI` —
# the import style two lines up. And it works by patching
# `build_middleware_stack`, which Starlette calls on its way into the lifespan
# scope, so this cannot move into `lifespan` above. Get either wrong and the
# agent silently emits no HTTP server spans while the evals service does, which
# makes the cross-service trace unreadable in the one service the observability
# argument is about (BLUEPRINT §6).
configure_logging()
setup_telemetry(get_settings().service_name, app)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _protocol(request: Request) -> Protocol:
    return request.app.state.protocol


def _graph(request: Request) -> CompiledStateGraph:
    return request.app.state.graph


def _llm(request: Request) -> LLMClient:
    return request.app.state.llm_client


def _settings(request: Request) -> Settings:
    return request.app.state.settings


# --------------------------------------------------------------------------
# Frames
# --------------------------------------------------------------------------
#
# A frame is `(name, payload)`. Four names cross the boundary — `stage`, `turn`,
# `error`, `trace` — and they are the Server-Sent Event names the streaming
# endpoint writes, so this vocabulary is the wire contract rather than an
# internal convenience.
#
# `error` carries the exception object rather than a rendered payload, and that
# is what lets one pipeline serve two endpoints without either compromising:
# `submit_turn` re-raises it so FastAPI answers the same 400/409/502 it always
# did, and the streaming endpoint renders it, because a response whose body has
# already begun cannot become a 500.

#: Handed from `_read_turn_events` back to `_turn_events`, never to a client.
#: Async generators cannot return a value, so the assembled `Turn` arrives as a
#: frame with a name the endpoints do not forward.
_READ = "read"

#: Sent with the event stream, and the third one is not optional in front of
#: nginx: with proxy buffering on, nginx holds the whole response until the
#: generator finishes and the client sees every stage arrive at once, at the end
#: — a stream that is indistinguishable from a slow request.
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _stage(
    name: str,
    status: str,
    *,
    ms: int | None = None,
    detail: dict[str, Any] | None = None,
) -> tuple[str, Any]:
    """One pipeline stage frame: what ran, whether it finished, and what it cost.

    ``ms`` and ``detail`` are null on ``start`` and on ``skip`` because neither
    has happened yet or ever will. The keys are always present rather than
    omitted, so a client reads ``frame.detail`` without first asking whether the
    field exists.
    """
    return "stage", {"stage": name, "status": status, "ms": ms, "detail": detail}


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _spend(trace: LLMCallTrace) -> dict[str, Any]:
    """What one model call cost, in the frame's spelling.

    Folded through :func:`trail.agent.machine.usage`, which is where this
    system decides that an input token is the uncached remainder *plus* the
    cache read *plus* the cache write. A frame that summed those three itself
    would be a second definition of the number the record carries, and the two
    would disagree the first time the cache behaviour changed.
    """
    folded = machine.usage([trace])
    return {
        "input_tokens": folded["total_input_tokens"],
        "output_tokens": folded["total_output_tokens"],
        "cost_usd": folded["cost_usd"],
    }


def _violation_json(violation: compliance.ComplianceViolation) -> dict[str, Any]:
    """A compliance violation as a client reads it: what failed, and why it matters.

    ``rule`` carries the blueprint section and the regime, so a violation shown
    in a UI explains itself rather than sending the reader to find the citation.
    """
    return {
        "check": violation.check,
        "rule": violation.rule,
        "detail": violation.detail,
        "evidence": violation.evidence,
    }


def _sse(event: str, data: Any) -> str:
    """Render one Server-Sent Event.

    Compact JSON with no separators padding, and ``ensure_ascii`` off because
    every utterance in this system is Brazilian Portuguese and an SSE stream is
    UTF-8 by specification — escaping "pendência" into ``\\u00ea`` would cost
    bytes to make the wire harder to read. ``json.dumps`` escapes any newline
    inside a string, so the payload is always the single ``data:`` line the
    format requires.
    """
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _error_json(exc: BaseException) -> dict[str, Any]:
    """The ``error`` event's payload for whatever went wrong.

    An :class:`~fastapi.HTTPException` keeps its status and its detail: a 502 on
    an upstream model failure and a 409 on a finished call are answers the
    client can act on, and the streamed version must say the same thing the JSON
    endpoint would have. Anything else is a bug, is logged here with its
    traceback — the JSON path gets that from the ASGI server, the streaming path
    would otherwise get it from nowhere — and is reported as a bare 500 rather
    than leaking an exception message into a browser.
    """
    if isinstance(exc, HTTPException):
        return {"status": exc.status_code, "detail": str(exc.detail)}
    logger.error("streamed turn failed", exc_info=exc)
    return {
        "status": status.HTTP_500_INTERNAL_SERVER_ERROR,
        "detail": "internal error",
    }


def _require_active_call(graph: CompiledStateGraph, call_id: UUID) -> CallState:
    """The state of a call that exists and is still in flight.

    Finished calls stay in the checkpointer, so a repeated turn on a completed
    call answers 409 rather than 404 as the contract requires. Nothing expires
    them, which is the same MVP deferral as holding them in-process at all.
    """
    state = machine.state_of(graph, call_id)
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown call_id {call_id}"
        )
    if state.finished:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"call {call_id} has already finished",
        )
    return state


def _require_matching_call_id(path_call_id: UUID, body_call_id: UUID) -> None:
    if path_call_id != body_call_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="body call_id does not match the path call_id",
        )


async def _read_turn_events(
    llm: LLMClient,
    graph: CompiledStateGraph,
    state: CallState,
    protocol: Protocol,
    customer_utterance: str,
) -> AsyncIterator[tuple[str, Any]]:
    """Read one customer turn with the model, accounting for it either way.

    Yields the ``extract`` and ``judge`` stage frames as they happen and ends
    with the assembled :class:`~trail.agent.machine.Turn` under :data:`_READ`.
    A generator rather than a plain call because these are the two slow stages —
    an extraction and, at one step, a judgement — and a client watching them
    finish one at a time is watching the actual pipeline. Collecting the frames
    and returning them together would report the same thing after the fact.

    The expected identifiers are given to the model only during
    ``verify_right_party``: outside that step it has no reason to hold a name, a
    CPF and a date of birth, and a prompt that carries a national identifier
    everywhere is a prompt that can echo one everywhere. Even there they buy
    only corroboration — :func:`trail.agent.machine.identity_matches` is the
    gate, and it compares what the caller actually stated against the account
    deterministically, check digits included.

    The terms verdict is a second call, made only at the step that needs it, and
    it judges against the **rendered** ``state_balance`` block — the figures this
    customer was really read, substituted from the system of record. Judging
    against ``text_for`` would be judging a restatement against the literal
    string ``"{balance}"``, which no customer can restate and which would fail
    every case in the golden set identically.

    An upstream failure that survives the SDK's retries becomes a ``502``. The
    call is left in flight — the graph is told to keep the same question open,
    so what the failed call cost is still folded into the record — and the
    caller can resubmit the same turn rather than lose the conversation. That
    side effect happens on the streamed path too: the exception raised out of
    this generator is caught one level up and rendered as an ``error`` event,
    long after ``machine.advance`` has already held the call open.
    """
    traces: list[LLMCallTrace] = []

    async def fail(detail: str) -> HTTPException:
        machine.advance(
            graph, state.call_id, Turn(override="retry", **machine.usage(traces))
        )
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)

    identity_hint = None
    if state.step is Step.VERIFY_RIGHT_PARTY:
        identity_hint = (
            "Expected customer on this account: full name "
            f"{state.profile.full_name!r}, CPF {state.profile.tax_id!r}, date of "
            f"birth {state.profile.date_of_birth.isoformat()}."
        )

    yield _stage("extract", "start")
    started = time.perf_counter()
    extraction = await llm.extract_turn(
        call_id=state.call_id,
        step=state.step,
        customer_utterance=customer_utterance,
        identity_hint=identity_hint,
    )
    await insert_llm_call_trace(extraction.trace)
    traces.append(extraction.trace)
    if extraction.value is None:
        raise await fail(f"extraction failed: {extraction.error}")
    yield _stage(
        "extract",
        "done",
        ms=_ms(started),
        detail={
            "step": state.step.value,
            "understood": extraction.value.understood,
            # Both bits, and neither carries a reason — there is nowhere in the
            # system for one. A UI may show that a turn routes to a person; it
            # can never show why, because nothing recorded why (BLUEPRINT §7).
            "needs_human": extraction.value.needs_human,
            "unresolved": extraction.value.unresolved,
            **_spend(extraction.trace),
        },
    )

    verdict: bool | None = None
    if state.step is not Step.CONFIRM_TERMS:
        # Skipped rather than omitted. A stage that vanishes from the stream at
        # seven steps out of eight looks like a stage that failed to report.
        yield _stage("judge", "skip")
    else:
        yield _stage("judge", "start")
        started = time.perf_counter()
        judged = await llm.judge_terms_restatement(
            call_id=state.call_id,
            approved_text=protocol.render(
                Step.STATE_BALANCE, slots_for_call(state.profile)
            ),
            restatement=customer_utterance,
        )
        await insert_llm_call_trace(judged.trace)
        traces.append(judged.trace)
        if judged.value is None:
            raise await fail(f"terms-restatement judgement failed: {judged.error}")
        verdict = judged.value
        yield _stage(
            "judge",
            "done",
            ms=_ms(started),
            detail={
                "terms_restated_correctly": verdict,
                **_spend(judged.trace),
            },
        )

    yield (
        _READ,
        Turn(
            extraction=extraction.value,
            terms_correct=verdict,
            **machine.usage(traces),
        ),
    )


def _screen(
    graph: CompiledStateGraph,
    state: CallState,
    protocol: Protocol,
    outcome: TurnOutcome,
    active_span: Span,
) -> tuple[TurnOutcome, dict[str, Any]]:
    """Gate one candidate utterance. Returns the outcome that may actually go out.

    The second half of the pair is the frame detail: whether the gate passed,
    whether a transfer was **actually forced** — the flag is set on the branch
    that calls :func:`~trail.agent.machine.force_transfer` and nowhere else, so
    it cannot report a transfer that did not happen on a call that was already
    transferring — and every violation, so a demo shows the refusal rather than
    only its consequence.

    ``slots`` is not an optional refinement here, and leaving it out has a loud
    but thoroughly misleading failure mode. A slotted block whose slots are not
    supplied contributes *nothing* to the approved set, so a gate called without
    them refuses the agent's own correctly rendered ``state_balance`` utterance
    — and every call in the run transfers, at the same step, with a compliance
    violation that says the approved text was unapproved. Both sides build the
    mapping through :func:`~trail.agent.machine.slots_for_call`, which is the
    single call site of the four formatters, so the utterance the agent renders
    and the utterance the gate approves cannot disagree about what this
    customer's balance sounds like.

    On a violation the call is forced to ``transferred_to_human``. That is how a
    violation is recorded on the record: :class:`~trail.models.CallRecord` has
    no violations column and must not gain one, so the terminal state carries the
    consequence — a call whose output failed an assertion can never be counted
    as a clean, fully automated completion.

    The forced transfer utterance is not re-screened. It is a module constant in
    ``machine.py``, it is on the gate's own allowlist, and its safety is proved
    once, offline, by the unit tests that run the assertions over it — not
    re-proved on every call.
    """
    result = compliance.check_outbound_utterance(
        outcome.agent_utterance,
        protocol,
        profile=state.profile,
        slots=slots_for_call(state.profile),
        identity_confirmed=state.identity_confirmed,
        prior_utterances=state.agent_transcript,
    )
    detail: dict[str, Any] = {
        "passed": result.passed,
        "forced_transfer": False,
        "violations": [_violation_json(v) for v in result.violations],
    }
    if result.passed:
        return outcome, detail

    for violation in result.violations:
        logger.critical(
            "compliance violation on call_id=%s step=%s: %s",
            state.call_id,
            outcome.step,
            violation,
        )
        active_span.add_event(
            "trail.compliance.violation",
            {
                "trail.compliance.check": violation.check,
                "trail.compliance.rule": violation.rule,
                "trail.compliance.detail": violation.detail,
                "trail.compliance.evidence": violation.evidence,
            },
        )

    if outcome.terminal_state is TerminalState.TRANSFERRED_TO_HUMAN:
        return outcome, detail
    detail["forced_transfer"] = True
    return machine.force_transfer(graph, state.call_id), detail


async def _finalise(
    state: CallState, protocol: Protocol, settings: Settings
) -> CallRecord:
    """Build the record, check it, and put it in the specialist queue."""
    record = machine.build_record(
        state,
        protocol,
        prompt_version=settings.prompt_version,
        model=settings.model,
    )

    classification = compliance.assert_no_risk_classification(record)
    for violation in classification.violations:
        # Logged, and the record is written anyway. Withholding a record from
        # the specialist queue to hide a compliance failure would be the worse of
        # the two failures by a wide margin: the customer is still in arrears,
        # and whatever they said — a promise, a dispute, a reason they cannot pay
        # — is still the thing the specialist needs to read. Suppressing the row
        # protects the run's numbers at the customer's expense, which is the
        # exact trade BLUEPRINT §7 exists to refuse.
        logger.critical(
            "record-level compliance violation on call_id=%s: %s",
            state.call_id,
            violation,
        )

    await insert_call_record(record)
    return record


# --------------------------------------------------------------------------
# The turn pipeline — one implementation, drained twice
# --------------------------------------------------------------------------


async def _turn_events(
    call_id: UUID, body: TurnRequest, request: Request
) -> AsyncIterator[tuple[str, Any]]:
    """Run one customer turn, yielding a frame as each stage completes.

    The single implementation of a turn. ``submit_turn`` drains it and keeps the
    ``turn`` frame; the streaming endpoint forwards every frame as an SSE. The
    ordering below is the contract and none of it is incidental: the model reads
    the turn, the machine decides what to say, the compliance gate decides
    whether it may be said, the turn trace is written, and only then — if the
    call ended — is the record built and queued. Reordering the last two would
    put a record in the specialist queue for a turn with no trace behind it.

    **Failures leave through a frame, not an exception.** Everything is caught
    here and yielded as ``("error", exc)``, for one reason: the streaming
    response has already sent its headers and its first events by the time most
    of these can happen, so it cannot become a 500 — the status line is long
    gone. ``submit_turn`` re-raises what it receives, so the JSON endpoint's
    statuses are unchanged: 400 on a mismatched body, 409 on a finished call,
    502 on an upstream model failure.

    **The trace frame is always last and always emitted**, error or not, because
    it is what a demo shows to prove the run is inspectable — and a turn that
    failed is the one you most want the trace for. ``trace_id`` is read *inside*
    the span, where there is a current span to read; the flush happens *after*
    the ``with`` block, because an unended span is not exportable and a flush
    from inside ships a trace missing its own root.
    """
    graph = _graph(request)
    protocol = _protocol(request)

    trace_id: str | None = None
    failure: BaseException | None = None

    try:
        _require_matching_call_id(call_id, body.call_id)
        state = _require_active_call(graph, call_id)

        # The step the call is at when this turn arrives. It is the step the
        # customer just spoke during, and the step recorded on the turn trace —
        # so a trace row and the extraction it carries always agree about where
        # in the conversation they belong, whatever the agent says next.
        step = state.step

        with span(
            "trail.turn",
            **{"trail.call_id": str(call_id), "trail.step": step.value},
        ) as active_span:
            trace_id = current_trace_id()
            started = time.perf_counter()

            turn: Turn | None = None
            async for name, payload in _read_turn_events(
                _llm(request), graph, state, protocol, body.customer_utterance
            ):
                if name == _READ:
                    turn = payload
                else:
                    yield name, payload
            assert turn is not None  # `_read_turn_events` raises or yields one

            yield _stage("advance", "start")
            began = time.perf_counter()
            outcome = machine.advance(graph, call_id, turn)
            # Screened against the state the turn produced: the identity gate may
            # have opened on this very turn, and the transcript now carries the
            # question this reply answered.
            state = machine.state_of(graph, call_id) or state
            yield _stage(
                "advance",
                "done",
                ms=_ms(began),
                detail={
                    "from_step": step.value,
                    "to_step": outcome.step.value,
                    "finished": outcome.finished,
                    "terminal_state": (
                        outcome.terminal_state.value if outcome.terminal_state else None
                    ),
                },
            )

            yield _stage("screen", "start")
            began = time.perf_counter()
            outcome, screened = _screen(graph, state, protocol, outcome, active_span)
            yield _stage("screen", "done", ms=_ms(began), detail=screened)

            yield _stage("persist", "start")
            began = time.perf_counter()
            await insert_turn_trace(
                TurnTrace(
                    call_id=call_id,
                    step=step,
                    agent_utterance=outcome.agent_utterance,
                    customer_utterance=body.customer_utterance,
                    extraction=turn.extraction,
                    # Measured from before the model call, not from this stage:
                    # this is the turn's latency as the customer experienced it,
                    # and it is what the p95 on the scorecard is computed from.
                    latency_ms=_ms(started),
                    created_at=_utcnow(),
                )
            )
            yield _stage("persist", "done", ms=_ms(began), detail={})

            record: CallRecord | None = None
            if outcome.finished and outcome.terminal_state is not None:
                active_span.set_attribute(
                    "trail.terminal_state", outcome.terminal_state.value
                )
                yield _stage("finalise", "start")
                began = time.perf_counter()
                state = machine.state_of(graph, call_id) or state
                record = await _finalise(state, protocol, _settings(request))
                yield _stage(
                    "finalise",
                    "done",
                    ms=_ms(began),
                    detail={"terminal_state": outcome.terminal_state.value},
                )

            yield (
                "turn",
                TurnResponse(
                    call_id=call_id,
                    step=outcome.step,
                    agent_utterance=outcome.agent_utterance,
                    finished=outcome.finished,
                    terminal_state=outcome.terminal_state,
                    record=record,
                    trace_id=trace_id,
                    trace_url=trace_url(trace_id),
                ),
            )
    except Exception as exc:
        # Broad on purpose, and nothing is swallowed: the span above has already
        # recorded the exception and marked itself errored on its way out, and
        # the object is handed on intact for `submit_turn` to raise.
        failure = exc

    await flush_telemetry()
    if failure is not None:
        yield "error", failure
    yield "trace", {"trace_id": trace_id, "trace_url": trace_url(trace_id)}


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@app.post(
    "/calls", response_model=StartCallResponse, status_code=status.HTTP_201_CREATED
)
async def start_call(body: StartCallRequest, request: Request) -> StartCallResponse:
    """Start a call and return the agent's opening utterance.

    The response carries the trace this request produced, so a client can offer
    the Jaeger link from the first screen rather than only once a turn has been
    submitted.
    """
    protocol = _protocol(request)
    call_id, outcome = machine.open_call(
        _graph(request), body.profile, case_id=body.case_id
    )

    with span(
        "trail.call.start",
        **{"trail.call_id": str(call_id), "trail.step": outcome.step.value},
    ) as active_span:
        # Inside the block, where there is a current span to read. Outside it
        # this is None, and the link silently disappears.
        trace_id = current_trace_id()
        if body.case_id:
            active_span.set_attribute("trail.case_id", body.case_id)

        # No `slots` here, and that is a deliberate asymmetry with `_screen`.
        # The opening utterance is the approved `verify_right_party` block, which
        # declares no slots, so supplying them would widen the approved set by
        # exactly one member: the rendered balance — at the one moment in the
        # call when identity is still unproven and disclosing it would be
        # BLUEPRINT §5's first zero-tolerance failure. Fail closed on the cheap
        # side: nothing that needs slots can be spoken here.
        screened = compliance.check_outbound_utterance(
            outcome.agent_utterance,
            protocol,
            profile=body.profile,
            identity_confirmed=False,
            prior_utterances=(),
        )
        if not screened.passed:
            # Unreachable unless the protocol file itself is unsafe, which is
            # precisely when the process must refuse rather than improvise.
            for violation in screened.violations:
                logger.critical("opening utterance failed compliance: %s", violation)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="approved protocol text failed the compliance gate",
            )

        await insert_turn_trace(
            TurnTrace(
                call_id=call_id,
                step=outcome.step,
                agent_utterance=outcome.agent_utterance,
                customer_utterance="",
                extraction=None,
                latency_ms=0,
                created_at=_utcnow(),
            )
        )

    # After the block, never inside it: an unended span is not exportable, so a
    # flush from in there ships a trace missing the very span this link names.
    await flush_telemetry()
    return StartCallResponse(
        call_id=call_id,
        step=outcome.step,
        agent_utterance=outcome.agent_utterance,
        finished=False,
        terminal_state=None,
        trace_id=trace_id,
        trace_url=trace_url(trace_id),
    )


@app.post("/calls/{call_id}/turns", response_model=TurnResponse)
async def submit_turn(
    call_id: UUID, body: TurnRequest, request: Request
) -> TurnResponse:
    """Submit one customer utterance and return the agent's reply.

    Drains :func:`_turn_events` and answers with its ``turn`` frame. The stage
    frames are dropped here — they exist for the streaming endpoint — and the
    ``error`` frame is re-raised, so this endpoint's statuses are exactly what
    they were before the pipeline was made streamable: 400, 409, 502, and
    whatever an unexpected exception has always produced.

    The stream is drained to the end rather than abandoned on the first error,
    because the frames after it are not decoration: the telemetry flush runs
    there, and abandoning the generator mid-flight would leave the trace this
    response is about unexported.
    """
    response: TurnResponse | None = None
    failure: BaseException | None = None
    async for name, payload in _turn_events(call_id, body, request):
        if name == "turn":
            response = payload
        elif name == "error":
            failure = payload

    if failure is not None:
        raise failure
    if response is None:  # pragma: no cover - the pipeline yields one or errors
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="the turn pipeline produced no response",
        )
    return response


@app.post("/calls/{call_id}/turns/stream", response_class=StreamingResponse)
async def stream_turn(
    call_id: UUID, body: TurnRequest, request: Request
) -> StreamingResponse:
    """The same turn as ``POST /calls/{call_id}/turns``, as Server-Sent Events.

    Identical body, identical pipeline, identical side effects — the traces, the
    record and the compliance gate are the same code, not a parallel path. What
    differs is that the client sees each stage land as it lands: the extraction,
    the terms judgement, the state transition, the compliance screen, the writes.

    **This is not a token stream and must never be described as one.** The agent
    speaks compliance-approved text it never composed, so there is nothing to
    stream word by word; the reply arrives whole, in the ``turn`` event. A
    typewriter reveal on top of it is a client-side animation over a finished
    string, and calling that "the model writing" would misrepresent the one
    property this system is built to have.

    Always answers 200. A turn that fails sends an ``error`` event carrying the
    status the JSON endpoint would have returned, then the ``trace`` event, then
    ends — the response has already begun by then, so there is no status line
    left to change.
    """

    async def frames() -> AsyncIterator[str]:
        async for name, payload in _turn_events(call_id, body, request):
            if name == "turn":
                yield _sse(name, payload.model_dump(mode="json"))
            elif name == "error":
                yield _sse(name, _error_json(payload))
            else:
                yield _sse(name, payload)

    return StreamingResponse(
        frames(), media_type="text/event-stream", headers=_SSE_HEADERS
    )


@app.get("/demo/cases")
async def demo_cases() -> dict[str, Any]:
    """The accounts a demo client may open a call with. Synthetic, all of them.

    Namespaced under ``/demo/`` because that is what it is: this endpoint serves
    the golden-set fixtures and the built-in demo account, which are invented
    customers with invented CPFs that live in this repository. It reads nothing
    from the database, exposes no real account, and takes no parameter that
    could name one — a production deployment would drop the route rather than
    have to reason about what it might return.

    ``default`` is the account ``trail chat`` opens with, from
    :func:`trail.cases.demo_profile`, and its ``case_id`` is null because it
    belongs to no case: it has no scripted turns and no fixed expectation, so a
    call against it is driven by whoever is typing. Every other entry is a
    :class:`~trail.models.SyntheticCase`, labelled with its own description so
    a picker reads "o cliente cooperativo" rather than ``canonical_cooperative``
    — and the ids come from :data:`~trail.cases.GOLDEN_SET` itself, so a case
    added to the fixture is offered here on the same commit.
    """
    return {
        "default": {
            "case_id": None,
            "label": "Conta de demonstração",
            "profile": demo_profile().model_dump(mode="json"),
        },
        "cases": [
            {
                "case_id": case.case_id,
                "label": case.description,
                "profile": case.profile.model_dump(mode="json"),
            }
            for case in GOLDEN_SET
        ],
    }


@app.post("/calls/{call_id}/unreachable", response_model=CallRecord)
async def mark_unreachable(
    call_id: UUID, body: MarkUnreachableRequest, request: Request
) -> CallRecord:
    """End an in-flight call as ``not_reached`` and return the persisted record.

    ``reason`` is operational — no answer, disconnected number, voicemail — and
    is recorded on the span rather than the record, because it is a telephony
    fact rather than a collections one, and because a free-text sentence about
    the customer on a collections record is exactly where a classification would
    eventually be written. The account stays in the primary metric's
    denominator: a voice agent cannot fix a wrong phone number, and a rate
    computed over answered calls only would quietly delete the accounts this
    models — v0 is inbound so non-answer is rare today, and outbound arrives at
    a ~28% connection rate (BLUEPRINT §4).
    """
    _require_matching_call_id(call_id, body.call_id)
    graph = _graph(request)
    state = _require_active_call(graph, call_id)

    with span(
        "trail.call.unreachable",
        **{
            "trail.call_id": str(call_id),
            "trail.step": state.step.value,
            "trail.terminal_state": TerminalState.NOT_REACHED.value,
            "trail.unreachable_reason": body.reason,
        },
    ):
        machine.advance(graph, call_id, Turn(override="not_reached"))
        state = machine.state_of(graph, call_id) or state
        return await _finalise(state, _protocol(request), _settings(request))


@app.get("/calls/{call_id}", response_model=CallRecord)
async def read_call(call_id: UUID) -> CallRecord:
    """Read a finished record from Postgres.

    The agent is stateless by design (BLUEPRINT §8), so this reads the system
    of record rather than the in-process session: a call that has not finished
    has no record yet and answers 404.
    """
    record = await get_call_record(call_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no finished record for call_id {call_id}",
        )
    return record


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
