"""Driving the golden set over HTTP, against an agent that is not there.

The harness speaks to the agent through exactly the endpoints the CLI uses,
with ``httpx``, over the network — an eval that calls internal functions tests
the code, while one that calls the interface tests the *system*, including
serialisation, timeouts and the contract the real client sees (BLUEPRINT §6).
That design is what makes these tests possible: an :class:`httpx.MockTransport`
substitutes for the whole agent, and the harness cannot tell.

The stub is a perfect agent — it returns exactly what each case expects — so a
full run is the harness measuring itself. The expected scorecard is then the
golden set's own arithmetic, and any deviation is a bug in the scorer rather
than in the agent. Nothing here calls a model, opens a socket, or reads a
customer's balance from anywhere but a fixture.
"""

from __future__ import annotations

import functools
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest

from tests.conftest import (
    AS_OF,
    DEFAULT_TAX_ID,
    FAKE_AGENT_BASE_URL,
    FAKE_COST_PER_CALL_USD,
    MakeAgent,
    RecordStore,
)
from trail.cases import GOLDEN_SET, GOLDEN_SET_VERSION
from trail.cases.golden_v1 import (
    CANONICAL_COOPERATIVE,
    NOT_REACHED,
    WRONG_PARTY,
)
from trail.evals import runner
from trail.evals.metrics import compute_metrics
from trail.evals.runner import run_case, run_golden_set
from trail.models import (
    AccountProfile,
    CallRecord,
    CaseExpectation,
    PaymentCommitment,
    PaymentPath,
    Product,
    SyntheticCase,
    TerminalState,
)

pytestmark = pytest.mark.unit


def _short_script_case() -> SyntheticCase:
    """A case whose script is shorter than the conversation it is asked to hold."""
    return SyntheticCase(
        case_id="script_runs_out",
        description="Two scripted turns for a call that needs seven.",
        profile=AccountProfile(
            account_id="AUR-TEST-9001",
            full_name="Cliente Teste",
            tax_id=DEFAULT_TAX_ID,
            date_of_birth=date(1960, 1, 1),
            phone="+55 11 90000-0199",
            product=Product.PERSONAL_LOAN,
            balance_brl=Decimal("410.00"),
            due_date=AS_OF - timedelta(days=7),
            days_past_due=7,
        ),
        scripted_turns=["Sim, sou eu.", "Pode continuar."],
        expectation=CaseExpectation(
            expected_terminal_state=TerminalState.COMPLETED_NO_CALLBACK
        ),
    )


# ---------------------------------------------------------------------------
# One case at a time
# ---------------------------------------------------------------------------


async def test_a_scripted_case_is_driven_to_a_terminal_state(
    make_agent: MakeAgent,
) -> None:
    client, _ = make_agent()

    outcome = await run_case(client, CANONICAL_COOPERATIVE, uuid4())

    assert outcome.error is None
    assert outcome.record is not None
    assert outcome.terminal_state is TerminalState.COMPLETED_NO_CALLBACK
    assert (
        len(outcome.agent_utterances) == len(CANONICAL_COOPERATIVE.scripted_turns) + 1
    )
    assert len(outcome.turn_latencies_ms) == len(CANONICAL_COOPERATIVE.scripted_turns)


async def test_the_opening_utterance_is_not_timed_as_a_turn(
    make_agent: MakeAgent,
) -> None:
    """``POST /calls`` reads approved text with no model call behind it.

    Including it would deflate the turn-latency distribution with a request that
    is not representative of a turn.
    """
    client, _ = make_agent()

    outcome = await run_case(client, CANONICAL_COOPERATIVE, uuid4())

    assert len(outcome.agent_utterances) == len(outcome.turn_latencies_ms) + 1


async def test_an_unreached_account_is_closed_through_the_real_endpoint(
    make_agent: MakeAgent,
) -> None:
    """Modelling non-answer through the endpoint, not by skipping the case.

    Skipping it would take the account out of the denominator, which is the one
    thing the primary metric must never allow. It is the same move that turns
    the one named public funnel in this industry into an 11.8% headline by
    dividing links by live conversations instead of by attempts. The MVP models
    non-answer on purpose so the honest number looks bad on the first run.
    """
    client, agent = make_agent()

    outcome = await run_case(client, NOT_REACHED, uuid4())

    assert outcome.terminal_state is TerminalState.NOT_REACHED
    assert outcome.record is not None
    assert outcome.turn_latencies_ms == ()
    paths = [path for _, path in agent.requests]
    assert any(path.endswith("/unreachable") for path in paths)
    assert not any(path.endswith("/turns") for path in paths)


async def test_a_turn_the_agent_never_asks_for_is_left_unused_and_is_not_an_error(
    make_agent: MakeAgent,
) -> None:
    """The wrong-party case scripts a second refusal it expects to go unused.

    Valdir presses a second time — *"É do cartão? Quanto que é?"* — and a
    perfect agent has already terminated, so the turn is never fed. An unused
    turn is not a harness failure; it is the gate holding.
    """
    client, _ = make_agent()

    outcome = await run_case(client, WRONG_PARTY, uuid4())

    assert outcome.error is None
    assert outcome.terminal_state is TerminalState.NOT_RIGHT_PARTY
    assert len(outcome.turn_latencies_ms) == 1
    assert len(WRONG_PARTY.scripted_turns) == 2


# ---------------------------------------------------------------------------
# The interface, and that it is the only thing the harness touches
# ---------------------------------------------------------------------------


async def test_the_harness_speaks_only_the_endpoints_the_client_speaks(
    make_agent: MakeAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four endpoints, INTERFACES §3, and nothing else.

    The record arrives on the ``TurnResponse`` that reports ``finished``, so the
    harness never fetches it back with ``GET /calls/{id}``. That is not an
    optimisation: a harness that re-read the record would be scoring what the
    database holds rather than what the interface returned, and a serialisation
    bug between the two would score clean.
    """
    _, agent = make_agent()
    monkeypatch.setattr(
        runner.httpx,
        "AsyncClient",
        functools.partial(httpx.AsyncClient, transport=agent.transport),
    )

    await run_golden_set(GOLDEN_SET, run_id=uuid4(), base_url=FAKE_AGENT_BASE_URL)

    def shape(method: str, path: str) -> tuple[str, str]:
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "calls":
            return method, f"/calls/{{id}}/{parts[2]}"
        return method, path

    assert {shape(method, path) for method, path in agent.requests} == {
        ("GET", "/healthz"),
        ("POST", "/calls"),
        ("POST", "/calls/{id}/turns"),
        ("POST", "/calls/{id}/unreachable"),
    }


def test_the_runner_never_imports_the_agent_it_drives() -> None:
    """The eval reaches the agent over the network or not at all.

    An eval that imports :mod:`trail.agent.machine` can reach inside, read the
    checkpointer and assert on state the real client cannot see — and it stops
    testing the system in exactly the places the system is most likely to break:
    serialisation, ``Decimal`` balances on the wire, timeouts, the contract. The
    cost of holding this line is that a failure here can be a serialisation bug
    or a quality problem and telling them apart is work. The benefit is that
    both are *visible*, because the harness sees what the CLI sees.
    """
    origins: set[str] = set()
    for value in vars(runner).values():
        for attribute in ("__module__", "__name__"):
            origin = getattr(value, attribute, None)
            if isinstance(origin, str):
                origins.add(origin)

    assert not [name for name in origins if name.startswith("trail.agent")]


# ---------------------------------------------------------------------------
# Failure, and why it is never an exception
# ---------------------------------------------------------------------------


async def test_a_script_that_runs_out_is_an_error_and_not_a_completion(
    make_agent: MakeAgent,
) -> None:
    case = _short_script_case()
    client, _ = make_agent(cases=[case], never_finish=True)

    outcome = await run_case(client, case, uuid4())

    assert outcome.record is None
    assert outcome.terminal_state is None
    assert outcome.error is not None
    assert "exhausted" in outcome.error


async def test_an_agent_that_errors_mid_call_does_not_abort_the_run(
    make_agent: MakeAgent,
) -> None:
    """One broken case must not take the other fourteen with it.

    The exception is swallowed into the outcome, which is why the runner puts it
    on the span by hand — otherwise the case looks healthy in Langfuse and wrong
    in the report.
    """
    client, _ = make_agent(turn_status=500)

    outcome = await run_case(client, CANONICAL_COOPERATIVE, uuid4())

    assert outcome.record is None
    assert outcome.error is not None
    assert "HTTP 500" in outcome.error


async def test_finishing_without_a_record_is_reported_as_a_contract_violation(
    make_agent: MakeAgent,
) -> None:
    """``TurnResponse.record`` is populated exactly when ``finished`` is true."""
    client, _ = make_agent(omit_record_on_finish=True)

    outcome = await run_case(client, CANONICAL_COOPERATIVE, uuid4())

    assert outcome.record is None
    assert outcome.error is not None
    assert "INTERFACES §3" in outcome.error


async def test_a_contract_drift_is_not_reported_as_a_transport_failure() -> None:
    """A 200 the models reject is described as contract drift, not as transport.

    ``_describe`` renders the three failure shapes differently on purpose. A
    transport error says the agent is unwell; a :class:`ValidationError` says the
    agent answered confidently with something that is not the agreed shape, which
    is the failure the harness exists to catch early — a ``Decimal`` balance
    serialised as a float, a renamed field, an enum value nobody added to the
    model. Collapsing the two into "the call failed" would put a schema change
    and a restarting container in the same bucket.
    """

    def answer_with_the_wrong_shape(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201, json={"call_id": str(uuid4()), "step": "verify_right_party"}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(answer_with_the_wrong_shape),
        base_url=FAKE_AGENT_BASE_URL,
    ) as client:
        outcome = await run_case(client, CANONICAL_COOPERATIVE, uuid4())

    assert outcome.record is None
    assert outcome.error is not None
    assert "did not match the contract" in outcome.error
    assert "HTTP" not in outcome.error


async def test_an_agent_that_is_down_abandons_the_run_instead_of_scoring_it_zero(
    make_agent: MakeAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An infrastructure failure and a quality failure must not look alike.

    Without the preflight, an agent that is simply not up produces fifteen
    per-case transport errors, a ``fully_automated_rate`` of 0.0, and a
    scorecard that reads like a catastrophic quality regression. Every bar in
    :data:`~trail.evals.metrics.THRESHOLDS` would fail, the run would look
    like the worst regression the system had ever had, and the cause would be a
    container that had not finished starting.
    """
    _, agent = make_agent(healthy=False)
    monkeypatch.setattr(
        runner.httpx,
        "AsyncClient",
        functools.partial(httpx.AsyncClient, transport=agent.transport),
    )

    with pytest.raises(RuntimeError, match="not reachable"):
        await run_golden_set(GOLDEN_SET, run_id=uuid4(), base_url=FAKE_AGENT_BASE_URL)


# ---------------------------------------------------------------------------
# The whole set
# ---------------------------------------------------------------------------


async def test_outcomes_come_back_in_case_order_however_they_finished(
    make_agent: MakeAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """So a diff between two runs is about behaviour and not about scheduling."""
    _, agent = make_agent()
    monkeypatch.setattr(
        runner.httpx,
        "AsyncClient",
        functools.partial(httpx.AsyncClient, transport=agent.transport),
    )

    outcomes = await run_golden_set(
        GOLDEN_SET, run_id=uuid4(), base_url=FAKE_AGENT_BASE_URL
    )

    assert [outcome.case.case_id for outcome in outcomes] == [
        case.case_id for case in GOLDEN_SET
    ]


async def test_a_perfect_agent_scores_exactly_what_the_golden_set_predicts(
    make_agent: MakeAgent,
    monkeypatch: pytest.MonkeyPatch,
    record_store: RecordStore,
) -> None:
    """The end-to-end check on the harness itself.

    An agent that returns exactly what every case expects must produce the set's
    own arithmetic: six of fifteen fully automated (40%, the declared ceiling),
    not one finding, and not one compliance violation — because every word it
    spoke came out of the approved protocol, verbatim where the block is
    verbatim and rendered from the account where the block is slotted.

    ``reached`` is fourteen and the denominator is still fifteen. That gap is
    the whole point of the primary metric, and it is visible here in a run with
    no agent, no model and no database behind it.

    ``promise_capture_rate`` is eight of fifteen against six of fifteen
    automated, and the two numbers coming apart is the reason both are
    published. ``asks_for_discount`` and ``terms_restated_wrong_twice`` both
    leave a promise carrying an amount and a day behind them and both still need
    a specialist to phone. Automation asks whether the call finished clean;
    capture asks whether a promise was obtained; and neither of them is money.
    """
    _, agent = make_agent()
    monkeypatch.setattr(
        runner.httpx,
        "AsyncClient",
        functools.partial(httpx.AsyncClient, transport=agent.transport),
    )

    outcomes = await run_golden_set(
        GOLDEN_SET, run_id=uuid4(), base_url=FAKE_AGENT_BASE_URL
    )
    metrics, findings = compute_metrics(
        run_id=uuid4(),
        golden_set_version=GOLDEN_SET_VERSION,
        outcomes=outcomes,
        fallback_prompt_version="unused",
        fallback_model="unused",
    )

    assert findings == []
    assert metrics.compliance_violations == 0
    assert metrics.scheduled_accounts == 15
    assert metrics.reached == 14
    assert metrics.fully_automated_rate == pytest.approx(6 / 15)
    assert metrics.promise_capture_rate == pytest.approx(8 / 15)
    assert metrics.commitment_entity_accuracy == pytest.approx(1.0)
    assert metrics.terms_confirmation_rate == pytest.approx(10 / 11)
    assert metrics.false_terms_confirmations == 0
    assert metrics.cost_per_fully_automated_call_usd == pytest.approx(
        15 * FAKE_COST_PER_CALL_USD / 6
    )
    assert len(record_store.records) == 15
    assert all(
        record.needs_specialist_review is True
        for record in record_store.specialist_queue()
    )


async def test_one_dropped_payment_date_is_one_omission_and_moves_nothing_else(
    make_agent: MakeAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Degrade the agent by exactly one field and watch the scorecard.

    The terminal state is unchanged, so the primary metric does not move; what
    moves is ``commitment_entity_accuracy``, the omission count, and — because
    a promise without a day is not a captured promise — ``promise_capture_rate``
    by exactly one case. That separation is deliberate. A system can finish
    every call clean and still be wrong about the money, and one number must not
    hide the other: ``fully_automated_rate`` is the number a vendor quotes, and
    it is the number least able to see this failure.

    The dropped field is the *date* rather than the amount, and that is not
    arbitrary. ``metrics._align`` pairs promises on the amount, so a dropped
    amount reads as an omission plus a fabrication while a dropped date reads as
    the single wrong field it is — the seam is stated in ``_align``'s own
    docstring and this test is where it becomes visible.
    """

    def drop_the_first_payment_date(
        case: SyntheticCase, record: CallRecord
    ) -> CallRecord:
        if case.case_id != CANONICAL_COOPERATIVE.case_id or not record.commitments:
            return record
        commitments = list(record.commitments)
        commitments[0] = commitments[0].model_copy(update={"date": None})
        return record.model_copy(update={"commitments": commitments})

    _, agent = make_agent(policy=drop_the_first_payment_date)
    monkeypatch.setattr(
        runner.httpx,
        "AsyncClient",
        functools.partial(httpx.AsyncClient, transport=agent.transport),
    )

    outcomes = await run_golden_set(
        GOLDEN_SET, run_id=uuid4(), base_url=FAKE_AGENT_BASE_URL
    )
    metrics, findings = compute_metrics(
        run_id=uuid4(),
        golden_set_version=GOLDEN_SET_VERSION,
        outcomes=outcomes,
        fallback_prompt_version="unused",
        fallback_model="unused",
    )

    assert [(finding.case_id, finding.field) for finding in findings] == [
        (CANONICAL_COOPERATIVE.case_id, "commitments[0].date")
    ]
    assert metrics.fully_automated_rate == pytest.approx(6 / 15)
    assert metrics.promise_capture_rate == pytest.approx(7 / 15)
    assert metrics.commitment_entity_accuracy < 1.0
    assert metrics.compliance_violations == 0


async def test_an_invented_commitment_is_a_fabrication_the_run_reports(
    make_agent: MakeAgent,
) -> None:
    """The second promise nobody made, scored end to end.

    Adriana promised one payment. A record carrying two describes a customer who
    agreed to something she never said, and a specialist reading it would hold
    her to it. Every field of the invented row is a fabrication in its own
    right, because that is the shape of the harm: the amount, the day and the
    method are three separate claims about what she committed to.
    """

    def invent_a_second_promise(case: SyntheticCase, record: CallRecord) -> CallRecord:
        if case.case_id != CANONICAL_COOPERATIVE.case_id:
            return record
        return record.model_copy(
            update={
                "commitments": [
                    *record.commitments,
                    PaymentCommitment(
                        amount="quinhentos reais",
                        date="dia dez",
                        method=PaymentPath.INSTALMENTS,
                        source_utterance="ah, e o resto eu vejo depois",
                    ),
                ]
            }
        )

    client, _ = make_agent(policy=invent_a_second_promise)

    outcome = await run_case(client, CANONICAL_COOPERATIVE, uuid4())
    _, findings = compute_metrics(
        run_id=uuid4(),
        golden_set_version=GOLDEN_SET_VERSION,
        outcomes=[outcome],
        fallback_prompt_version="unused",
        fallback_model="unused",
    )

    assert [finding.actual for finding in findings] == [
        "quinhentos reais",
        "dia dez",
        PaymentPath.INSTALMENTS.value,
    ]
    assert all(finding.kind.value == "fabrication" for finding in findings)


def test_the_unreachable_reason_asserts_nothing_about_the_customer() -> None:
    """The one string the harness authors itself, and the ceiling on what it may say.

    ``UNREACHABLE_REASON`` is written by this module rather than by the golden
    set, so it is the only sentence in an eval run that is neither approved text
    nor a customer's words. It describes the telephony outcome and stops there:
    no balance, no product, no judgement about why nobody picked up. "Customer
    avoiding contact" would be a classification of a debtor, written by the
    harness, onto a record that has no field for one (CONTRACT §7) — and it
    would be a guess about a wrong phone number.
    """
    reason = runner.UNREACHABLE_REASON.casefold()

    assert "no answer" in reason
    assert not any(
        word in reason
        for word in (
            "avoid",
            "refus",
            "ignor",
            "evasive",
            "hardship",
            "risk",
            "priority",
            "urgent",
            "dívida",
            "saldo",
        )
    )


def test_the_case_outcome_reports_no_terminal_state_when_no_record_landed() -> None:
    """``terminal_state`` is derived from the record, never carried beside it.

    A second field would let an outcome claim a terminal state for a call that
    produced nothing — which is precisely the case the scorer must count in the
    denominator and not the numerator. Deriving it makes "no record" and "no
    terminal state" the same fact rather than two that can disagree.
    """
    landed = runner.CaseOutcome(case=CANONICAL_COOPERATIVE, record=None, error="boom")

    assert landed.terminal_state is None


async def test_a_case_the_harness_could_not_drive_is_still_scored_as_a_case(
    make_agent: MakeAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A case the harness could not complete is not a case that did not happen.

    Every turn fails, so every record is ``None``. The run still has fifteen
    scheduled accounts underneath it and a ``fully_automated_rate`` of zero,
    because a run that cannot drive its cases has not automated them. This is
    the *quality* failure the preflight exists to distinguish from an agent that
    is simply down — here the agent answers ``/healthz`` and then breaks on
    every call, which is the shape a real regression has.
    """
    _, agent = make_agent(turn_status=500)
    monkeypatch.setattr(
        runner.httpx,
        "AsyncClient",
        functools.partial(httpx.AsyncClient, transport=agent.transport),
    )

    outcomes = await run_golden_set(
        GOLDEN_SET, run_id=uuid4(), base_url=FAKE_AGENT_BASE_URL
    )
    metrics, _ = compute_metrics(
        run_id=uuid4(),
        golden_set_version=GOLDEN_SET_VERSION,
        outcomes=outcomes,
        fallback_prompt_version="unused",
        fallback_model="unused",
    )

    assert len(outcomes) == 15
    assert metrics.scheduled_accounts == 15
    assert metrics.fully_automated_rate == 0.0
    assert metrics.promise_capture_rate == 0.0
    # Undefined, not free: nothing was automated, so there is no denominator to
    # divide the run's spend by, and 0.0 would sail under the $1.84 ceiling.
    assert metrics.cost_per_fully_automated_call_usd is None
