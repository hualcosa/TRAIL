"""Drives the golden set against the agent over HTTP.

The harness speaks to the agent through the agent's public HTTP contract, the
same one the CLI speaks (INTERFACES §3), with ``httpx``, over the network,
against ``settings.agent_base_url``. It uses a superset of what the CLI touches
— ``POST /calls`` and ``POST /calls/{id}/turns`` are shared, and only the
harness has any use for ``POST /calls/{id}/unreachable`` — but nothing outside
the published contract, and it never imports agent internals. That is a
deliberate cost — the eval cannot reach inside and inspect state, so a failure
here can be a serialisation bug, a timeout, or a genuine quality problem, and
telling them apart is work. The alternative is worse: an eval that calls
internal functions tests the code, while one that calls the interface tests the
*system*, including the contract the real client sees (BLUEPRINT §6).

Three case shapes are driven differently, and the difference matters to the
primary metric:

* ``reachable=True`` — scripted customer turns are fed in order, one per agent
  turn, until the agent reports ``finished``.
* ``reachable=False`` (or ``answering_party="none"``) — the call is opened and
  then closed through ``POST /calls/{id}/unreachable``. **These cases still
  count**: they produce a real :class:`~trail.models.CallRecord` with
  ``TerminalState.NOT_REACHED`` and they stay in the denominator of
  ``fully_automated_rate``. v0 is inbound so nobody-answered is rare today, but
  outbound connects at ~28% (BLUEPRINT §4) and a voice agent cannot fix a wrong
  phone number. Dropping those accounts is the same self-flattery as reporting
  promise-to-pay and calling it money.
* ``answering_party="other"`` — someone who is not the customer answers, and the
  golden set scripts that person's words. The harness drives the call exactly as
  it drives a customer call: the right-party gate is the agent's to enforce, and
  the eval's job is to observe whether it held, not to help it.

Customer turns are **scripted, never LLM-generated** (BLUEPRINT §6):
deterministic, reproducible, free, and free of simulator collusion — an LLM
customer and an LLM agent share biases and conspire toward success. This module
therefore authors no dialogue of its own, not even a fallback. Every word a
customer or a wrong party says in an eval run is reviewable in
:mod:`trail.cases.golden_v1`, which is the only place it can be reviewed as
test data rather than as code.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID

import httpx
from pydantic import BaseModel, ValidationError

from trail.config import get_settings
from trail.models import (
    CallRecord,
    MarkUnreachableRequest,
    StartCallRequest,
    StartCallResponse,
    SyntheticCase,
    TerminalState,
    TurnRequest,
    TurnResponse,
)
from trail.telemetry import span

logger = logging.getLogger(__name__)


REQUEST_TIMEOUT_SECONDS: Final[float] = 120.0
"""Per-request timeout.

Generous on purpose. ``gpt-5.6-luna`` runs with reasoning disabled (INTERFACES
§6) and a single capture-commitment turn — two model calls when the customer is
restating terms — can take tens of seconds. A tight timeout here would turn a
latency observation into a spurious harness error and hide the number the
economics post is actually interested in.
"""

DEFAULT_CONCURRENCY: Final[int] = 4
"""Bound on in-flight calls.

Cases run concurrently because a serial golden set is minutes of dead waiting,
but the bound is low: this is one local agent process against one model-provider
account, and an unbounded fan-out measures the rate limiter rather than the
agent.
"""

UNREACHABLE_REASON: Final[str] = "no answer: scripted dial attempts did not connect"
"""Reason recorded when a case models a customer the agent never reaches.

Operational, never a statement about the customer or the debt — it describes the
telephony outcome and nothing else (INTERFACES §3), which is why it is carried
on a span and never onto the record.
"""


@dataclass(frozen=True)
class CaseOutcome:
    """Everything one driven case produced, before any scoring.

    Kept deliberately raw: this is the observation, and
    :mod:`trail.evals.metrics` is the interpretation. ``record`` is ``None``
    exactly when the call did not reach a terminal state — an HTTP failure, a
    contract violation, or a script that ran out before the agent finished — and
    ``error`` then says why.
    """

    case: SyntheticCase
    record: CallRecord | None
    agent_utterances: tuple[str, ...] = ()
    turn_latencies_ms: tuple[float, ...] = ()
    error: str | None = None

    @property
    def terminal_state(self) -> TerminalState | None:
        """The terminal state reached, or ``None`` if the call never landed."""
        return self.record.terminal_state if self.record is not None else None


@dataclass
class _Transcript:
    """Mutable accumulator for one call, so a failure mid-call keeps what it had."""

    agent_utterances: list[str] = field(default_factory=list)
    turn_latencies_ms: list[float] = field(default_factory=list)


def _is_unreachable(case: SyntheticCase) -> bool:
    """True when the case models a call that never connects.

    ``answering_party="none"`` and ``reachable=False`` describe the same
    telephony outcome from two directions; a case that sets either is driven
    through the unreachable endpoint.
    """
    return not case.reachable or case.answering_party == "none"


def _describe(exc: Exception) -> str:
    """A one-line, debuggable rendering of a transport or contract failure."""
    if isinstance(exc, httpx.HTTPStatusError):
        body = exc.response.text.strip().replace("\n", " ")[:300]
        return f"HTTP {exc.response.status_code} from {exc.request.url.path}: {body}"
    if isinstance(exc, ValidationError):
        return (
            f"response did not match the contract: {exc.error_count()} error(s); "
            f"{exc.errors()[:2]}"
        )
    return f"{type(exc).__name__}: {exc}"


async def _request[T: BaseModel](
    client: httpx.AsyncClient,
    url: str,
    payload: BaseModel,
    response_model: type[T],
) -> tuple[T, float]:
    """POST ``payload``, validate the response against ``response_model``.

    Returns the parsed model and the wall-clock milliseconds the round trip
    took. ``model_dump(mode="json")`` is what makes UUIDs, enums, dates and
    ``Decimal`` balances survive the wire (INTERFACES §1); validating the
    response rather than reading raw dicts is the point of driving the agent
    over HTTP at all — a contract drift shows up here, in the eval, and not in
    production.
    """
    started = time.perf_counter()
    response = await client.post(url, json=payload.model_dump(mode="json"))
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    response.raise_for_status()
    return response_model.model_validate(response.json()), elapsed_ms


async def _preflight(client: httpx.AsyncClient) -> None:
    """Fail the run loudly if the agent is not up.

    Without this, an agent that is simply down produces fifteen per-case
    transport errors, a ``fully_automated_rate`` of 0.0, and a scorecard that
    reads like a catastrophic quality regression. An infrastructure failure and
    a quality failure must not look alike.
    """
    try:
        response = await client.get("/healthz")
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"agent is not reachable at {client.base_url} ({_describe(exc)}); "
            "the run was abandoned rather than reported as a quality failure"
        ) from exc


async def run_case(
    client: httpx.AsyncClient, case: SyntheticCase, run_id: UUID
) -> CaseOutcome:
    """Drive one synthetic case end to end and return what it produced.

    Never raises for a call that goes wrong: a transport error, a non-2xx
    response, a response that violates the contract, or a script that runs out
    before the agent finishes all resolve to a :class:`CaseOutcome` with
    ``record=None`` and a populated ``error``. Those cases remain in the
    denominator of every rate — a case the harness could not complete is not a
    case that did not happen.
    """
    transcript = _Transcript()
    with span("eval.case", run_id=run_id, case_id=case.case_id) as case_span:
        try:
            start, _ = await _request(
                client,
                "/calls",
                StartCallRequest(profile=case.profile, case_id=case.case_id),
                StartCallResponse,
            )
            transcript.agent_utterances.append(start.agent_utterance)
            call_id = start.call_id
            case_span.set_attribute("trail.call_id", str(call_id))

            if _is_unreachable(case):
                record = await _mark_unreachable(client, call_id)
                case_span.set_attribute(
                    "trail.terminal_state", record.terminal_state.value
                )
                return CaseOutcome(
                    case=case,
                    record=record,
                    agent_utterances=tuple(transcript.agent_utterances),
                )

            record, error = await _play_script(client, case, call_id, transcript)
            if record is not None:
                case_span.set_attribute(
                    "trail.terminal_state", record.terminal_state.value
                )
            return CaseOutcome(
                case=case,
                record=record,
                agent_utterances=tuple(transcript.agent_utterances),
                turn_latencies_ms=tuple(transcript.turn_latencies_ms),
                error=error,
            )

        except (httpx.HTTPError, ValidationError) as exc:
            # The exception is swallowed into the outcome so one broken case
            # cannot abort the run, so it has to be put on the span by hand —
            # otherwise the case looks healthy in Jaeger and wrong in the report.
            case_span.record_exception(exc)
            detail = _describe(exc)
            logger.warning("case %s failed against the agent: %s", case.case_id, detail)
            return CaseOutcome(
                case=case,
                record=None,
                agent_utterances=tuple(transcript.agent_utterances),
                turn_latencies_ms=tuple(transcript.turn_latencies_ms),
                error=detail,
            )


async def _mark_unreachable(client: httpx.AsyncClient, call_id: UUID) -> CallRecord:
    """End an opened call as :attr:`~trail.models.TerminalState.NOT_REACHED`.

    Modelling non-answer through the real endpoint, rather than by skipping the
    case, is what keeps unreached accounts in the primary metric's denominator.
    The MVP models non-answer on purpose so the honest number looks bad on the
    first run (BLUEPRINT §6) — which is the whole disagreement with the one
    named public funnel in this industry, where 12,800 attempts become 1,360
    live conversations and only the 1,360 are ever put underneath a rate.
    """
    record, _ = await _request(
        client,
        f"/calls/{call_id}/unreachable",
        MarkUnreachableRequest(call_id=call_id, reason=UNREACHABLE_REASON),
        CallRecord,
    )
    return record


async def _play_script(
    client: httpx.AsyncClient,
    case: SyntheticCase,
    call_id: UUID,
    transcript: _Transcript,
) -> tuple[CallRecord | None, str | None]:
    """Feed the scripted turns in order until the agent ends the call.

    Returns ``(record, error)``. Latency is recorded for customer turns only: the
    opening ``POST /calls`` speaks verbatim protocol text with no model call
    behind it, and including it would deflate the turn-latency distribution with
    a request that is not representative of a turn.

    Turns left unconsumed are not an error. A case scripts what the customer
    would say if asked, and an agent that terminates early — a wrong party, a
    refused consent, a transfer — correctly never asks. The golden set relies on
    this: the later turns of the wrong-party case exist to catch an agent that
    holds the line once and then discloses the balance to whoever pressed it, and
    are expected to go unused.
    """
    turns = case.scripted_turns
    for utterance in turns:
        reply, elapsed_ms = await _request(
            client,
            f"/calls/{call_id}/turns",
            TurnRequest(call_id=call_id, customer_utterance=utterance),
            TurnResponse,
        )
        transcript.turn_latencies_ms.append(elapsed_ms)
        transcript.agent_utterances.append(reply.agent_utterance)

        if reply.finished:
            if reply.record is None:
                return None, (
                    "agent reported finished=true without a record, which violates "
                    "the TurnResponse contract (INTERFACES §3)"
                )
            return reply.record, None

    return None, (
        f"the {len(turns)} scripted turn(s) were exhausted and the agent had not "
        "reached a terminal state"
    )


async def run_golden_set(
    cases: Sequence[SyntheticCase],
    *,
    run_id: UUID,
    base_url: str | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> list[CaseOutcome]:
    """Drive every case against the agent, bounded by a semaphore.

    Outcomes come back in the order the cases were given, regardless of the
    order they finished in, so a report diff between two runs is a diff about
    behaviour and not about scheduling.

    Raises :class:`RuntimeError` if the agent is not reachable at all, which the
    service turns into a ``FAILED`` run rather than a scorecard of zeros.
    """
    target = base_url or get_settings().agent_base_url
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded(case: SyntheticCase) -> CaseOutcome:
        async with semaphore:
            return await run_case(client, case, run_id)

    with span("eval.golden_set", run_id=run_id) as set_span:
        set_span.set_attribute("trail.scheduled_accounts", len(cases))
        async with httpx.AsyncClient(
            base_url=target, timeout=REQUEST_TIMEOUT_SECONDS
        ) as client:
            await _preflight(client)
            logger.info(
                "driving %d case(s) against %s at concurrency %d",
                len(cases),
                target,
                concurrency,
            )
            return list(await asyncio.gather(*(bounded(case) for case in cases)))
