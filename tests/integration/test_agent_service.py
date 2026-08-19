"""The agent service, end to end, against the running stack.

Two kinds of test live here. Most cost nothing: the health probe, the contract
errors, and the unreachable path all run without a single model call, and they
are what proves the HTTP contract in INTERFACES §3 and the write path into
Postgres.

One costs real money — :func:`test_a_full_call_runs_to_a_record_and_leaves_a_trail`
holds an entire seven-turn conversation with ``gpt-5.6-luna``. It is the only
place in the suite where the whole system runs: protocol, state machine, model,
compliance gate, database and traces. What it asserts are invariants that must
hold whatever the model says, plus the terminal state, which is the one quality
claim worth failing a build over.

One thing here has no healthcare counterpart and is the reason this file was
worth re-reading rather than renaming: the agent speaks a **customer-specific
amount**, so every compliance check below is handed the same slot mapping the
agent rendered with (:func:`~trail.agent.machine.slots_for_call`). Checking a
rendered utterance without it would compare the agent's correct balance against
an approved set that still holds ``{balance}``, and the headline safety claim of
this port would be asserted by a test that could never pass.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from psycopg_pool import AsyncConnectionPool

from trail.agent.compliance import assert_agent_text_is_approved
from trail.agent.machine import slots_for_call
from trail.cases.golden_v1 import CANONICAL_COOPERATIVE
from trail.db import get_call_record
from trail.models import (
    AccountProfile,
    CallRecord,
    MarkUnreachableRequest,
    StartCallRequest,
    StartCallResponse,
    Step,
    TerminalState,
    TurnRequest,
    TurnResponse,
)
from trail.protocol import Protocol

pytestmark = pytest.mark.integration

COMPLETED = {
    TerminalState.COMPLETED_NO_CALLBACK,
    TerminalState.COMPLETED_NEEDS_CALLBACK,
}


def _start(client: httpx.Client, profile: AccountProfile) -> StartCallResponse:
    response = client.post(
        "/calls",
        json=StartCallRequest(profile=profile, case_id=None).model_dump(mode="json"),
    )
    assert response.status_code == 201, response.text
    return StartCallResponse.model_validate(response.json())


async def _count(pool: AsyncConnectionPool, table: str, call_id: UUID) -> int:
    async with pool.connection() as conn:
        cursor = await conn.execute(
            f"SELECT count(*) FROM {table} WHERE call_id = %s",
            (call_id,),
        )
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# Liveness and contract
# ---------------------------------------------------------------------------


def test_the_agent_reports_itself_healthy(agent_client: httpx.Client) -> None:
    response = agent_client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_a_new_call_opens_with_the_approved_right_party_prompt(
    agent_client: httpx.Client,
    sample_profile: AccountProfile,
    real_protocol: Protocol,
) -> None:
    """Also proves the container mounted the protocol file in this repository.

    Compared against :meth:`~trail.protocol.Protocol.text_for` rather than
    ``render``, because ``verify_right_party`` declares no slots and must not:
    it is the one block spoken before the right party is proven, so it may say
    no more than that there is a pendency on an account (CONTRACT §11). A
    protocol file that grew a slot here would fail this assertion, which is the
    right place to find out.
    """
    started = _start(agent_client, sample_profile)

    assert started.step is Step.VERIFY_RIGHT_PARTY
    assert started.finished is False
    assert started.terminal_state is None
    assert started.agent_utterance == real_protocol.text_for(Step.VERIFY_RIGHT_PARTY)


def test_a_turn_whose_body_disagrees_with_its_path_is_rejected(
    agent_client: httpx.Client, sample_profile: AccountProfile
) -> None:
    """400, and before any model call — the check runs first."""
    started = _start(agent_client, sample_profile)

    response = agent_client.post(
        f"/calls/{started.call_id}/turns",
        json=TurnRequest(call_id=uuid4(), customer_utterance="Sim, sou eu.").model_dump(
            mode="json"
        ),
    )

    assert response.status_code == 400


def test_an_unknown_key_is_a_schema_violation(
    agent_client: httpx.Client, sample_profile: AccountProfile
) -> None:
    """``extra="forbid"`` is what makes the structured-output schema strict too.

    The rejected key is chosen rather than arbitrary. ``propensity_to_pay`` is
    the field this design refuses to hold anywhere (CONTRACT §5, §7) — a score
    that would order the specialist queue by how likely a customer looks to pay
    — and the wire contract turning it away with a 422 is the cheapest possible
    place for that refusal to be visible.
    """
    body = StartCallRequest(profile=sample_profile).model_dump(mode="json")
    body["propensity_to_pay"] = 0.82

    response = agent_client.post("/calls", json=body)

    assert response.status_code == 422


def test_a_call_that_has_not_finished_has_no_record_to_read(
    agent_client: httpx.Client, sample_profile: AccountProfile
) -> None:
    """The agent is stateless by design: this endpoint reads the system of record."""
    started = _start(agent_client, sample_profile)

    response = agent_client.get(f"/calls/{started.call_id}")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# The unreachable path — a full write cycle with no model call
# ---------------------------------------------------------------------------


async def test_an_unreached_customer_lands_a_record_and_its_trace_in_postgres(
    agent_client: httpx.Client,
    sample_profile: AccountProfile,
    db_pool: AsyncConnectionPool,
    real_protocol: Protocol,
) -> None:
    """The cheapest end-to-end proof that persistence works.

    No model call, one opening turn trace, one record. It also pins the two
    properties that matter most about a record: it is required to be reviewed,
    and nobody has reviewed it.

    The account stays in the denominator of every rate this system reports, and
    that is the whole reason ``not_reached`` is a first-class terminal state
    rather than a discarded attempt (CONTRACT §3).
    """
    started = _start(agent_client, sample_profile)

    response = agent_client.post(
        f"/calls/{started.call_id}/unreachable",
        json=MarkUnreachableRequest(
            call_id=started.call_id, reason="no answer: three dial attempts"
        ).model_dump(mode="json"),
    )
    assert response.status_code == 200, response.text
    record = CallRecord.model_validate(response.json())

    assert record.terminal_state is TerminalState.NOT_REACHED
    assert record.needs_specialist_review is True
    assert record.reviewed_by is None
    assert record.reviewed_at is None
    assert record.protocol_version == real_protocol.version

    persisted = await get_call_record(started.call_id)
    assert persisted == record

    read_back = agent_client.get(f"/calls/{started.call_id}")
    assert read_back.status_code == 200
    assert CallRecord.model_validate(read_back.json()) == record

    assert await _count(db_pool, "turn_traces", started.call_id) == 1
    assert await _count(db_pool, "llm_call_traces", started.call_id) == 0


def test_a_finished_call_refuses_another_turn(
    agent_client: httpx.Client, sample_profile: AccountProfile
) -> None:
    started = _start(agent_client, sample_profile)
    agent_client.post(
        f"/calls/{started.call_id}/unreachable",
        json=MarkUnreachableRequest(
            call_id=started.call_id, reason="voicemail"
        ).model_dump(mode="json"),
    )

    response = agent_client.post(
        f"/calls/{started.call_id}/turns",
        json=TurnRequest(call_id=started.call_id, customer_utterance="Alô?").model_dump(
            mode="json"
        ),
    )

    assert response.status_code == 409


# ---------------------------------------------------------------------------
# One whole call, with the model in the loop
# ---------------------------------------------------------------------------


async def test_a_full_call_runs_to_a_record_and_leaves_a_trail(
    agent_client: httpx.Client,
    db_pool: AsyncConnectionPool,
    real_protocol: Protocol,
) -> None:
    """The only test that exercises the entire system. It costs real tokens.

    The scripted customer is ``canonical_cooperative`` — the one shape in the
    golden set that can produce a fully automated completion. What is asserted:

    * **every word the agent spoke is approved protocol text**, verbatim where
      the block is verbatim and rendered from *this account's* figures where it
      is slotted. This is the invariant the whole design rests on and it holds
      regardless of what the model returns;
    * the balance block the agent spoke is the one rendered from the system of
      record, character for character — the port's headline claim, checked
      against a live service rather than a fixture;
    * the call reached a completed terminal state rather than a transfer;
    * the record is unreviewed and requires review;
    * a turn trace exists per turn, and an LLM trace with real token counts
      exists per model call — a call whose trace is missing is spend the
      economics post cannot account for.

    ``slots`` comes from :func:`~trail.agent.machine.slots_for_call`, the
    single call site of the render-side formatters. A test that formatted
    ``R$ 847,32`` itself would be a second implementation of the one thing in
    this system that is not allowed to have two, and it would pass while
    disagreeing with the service.
    """
    case = CANONICAL_COOPERATIVE
    slots = slots_for_call(case.profile)
    rendered_balance = real_protocol.render(Step.STATE_BALANCE, slots)

    started = _start(agent_client, case.profile)
    spoken = [started.agent_utterance]

    record: CallRecord | None = None
    for utterance in case.scripted_turns:
        response = agent_client.post(
            f"/calls/{started.call_id}/turns",
            json=TurnRequest(
                call_id=started.call_id, customer_utterance=utterance
            ).model_dump(mode="json"),
        )
        assert response.status_code == 200, response.text
        turn = TurnResponse.model_validate(response.json())
        spoken.append(turn.agent_utterance)
        if turn.finished:
            record = turn.record
            break

    assert record is not None, "the agent never reached a terminal state"

    for utterance in spoken:
        assert "{" not in utterance, f"unrendered template spoken: {utterance!r}"
        result = assert_agent_text_is_approved(utterance, real_protocol, slots)
        assert result.passed, f"the agent improvised: {result.violations}"

    assert rendered_balance in spoken, "the agent never stated the balance on record"

    assert record.terminal_state in COMPLETED
    assert record.needs_specialist_review is True
    assert record.reviewed_by is None
    assert record.commitments, "the customer promised an amount and a day"
    assert record.consent_given is True
    assert record.protocol_version == real_protocol.version
    assert record.cost_usd > 0
    assert record.total_input_tokens > 0

    submitted_turns = len(spoken) - 1
    assert await _count(db_pool, "turn_traces", started.call_id) == len(spoken)
    assert await _count(db_pool, "llm_call_traces", started.call_id) >= submitted_turns
    assert await get_call_record(started.call_id) == record
