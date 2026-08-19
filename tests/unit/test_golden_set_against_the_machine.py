"""The seam nothing else crosses: the golden set driven through the machine.

``test_golden_set.py`` validates the fixture against itself. ``test_state_machine.py``
validates the machine against itself. Both passed, one industry over, while the
two disagreed about the single most consequential rule in the system — whether a
specialist callback is decided by commitment *presence* or commitment
*completeness* — and the disagreement pinned the primary metric at zero.

The arithmetic is identical here and it is checkable from the fixture alone:
every case in ``golden_v1`` that expects ``completed_no_callback`` reports at
least one :class:`~trail.models.PaymentCommitment`. So under a presence rule
none of the six was reachable, ``fully_automated_rate`` was 0.0 on every possible
run, and the only way to score above zero was for the agent to *fail to write
down a promise to pay* — the headline metric anti-correlated with extraction
quality, in the flattering direction. Nothing caught it, because no test drove a
case through ``machine.advance``.

This file is that test. It builds, for each case, the extractions a **perfect
agent** would produce — exactly what the case's own :class:`CaseExpectation`
declares, nothing more — and asserts the machine lands on the terminal state the
case declares, carrying the commitments, disputes, restatement verdict and
contact channel it declares. It is the arithmetic behind the 40% ceiling,
executed.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import pytest

from trail.agent import machine
from trail.agent.machine import (
    MAX_IDENTITY_ATTEMPTS,
    TRANSFER_TO_HUMAN_UTTERANCE,
    CallState,
    Turn,
)
from trail.cases import GOLDEN_SET
from trail.models import (
    PaymentCommitment,
    PaymentPath,
    Step,
    SyntheticCase,
    TerminalState,
    TurnExtraction,
)
from trail.protocol import Protocol

pytestmark = pytest.mark.unit

CASE_IDS = [case.case_id for case in GOLDEN_SET]

#: Cases whose callback reason lives in the transcript rather than in a pinned
#: field, and the step where a perfect agent would set
#: :attr:`~trail.models.TurnExtraction.unresolved`.
#:
#: Both are the same shape: the customer said something the record cannot carry
#: whole. Ricardo asks, three times, for a discount — a request the approved text
#: answers with a capability statement rather than an answer, and the third ask
#: leaves a question a specialist has to pick up. Otília gestures at a second
#: payment she can pin neither an amount nor a day to, which correctly produces
#: no row at all, because a ``PaymentCommitment`` with two nulls records the
#: *shape* of a promise nobody made.
#:
#: It is a constant here rather than a field on ``CaseExpectation`` on purpose:
#: encoding "the customer asked something" as an expectation field would make the
#: fixture describe the machine instead of the customer, and the golden set states
#: the reason in prose for exactly that reason (``golden_v1``, "Which cases carry
#: ``unresolved``, and which carry ``needs_human``").
_UNRESOLVED_AT: dict[str, Step] = {
    "asks_for_discount": Step.OFFER_PAYMENT_PATH,
    "elderly_slow_speech": Step.CAPTURE_COMMITMENT,
}

#: Cases that transfer, and the step the customer says the thing that transfers
#: them. This has no healthcare counterpart: there, a transfer was a refused
#: consent and refused consent happens at one known step. Here three different
#: turns reach the same exit — a refused recording at ``disclose_and_consent``, an
#: explicit dispute at ``state_balance``, a hardship disclosure at
#: ``offer_payment_path`` — and the whole claim of CONTRACT §7 is that the machine
#: cannot tell them apart afterwards. Which means the *driver* has to know where
#: each one lands, because the record deliberately will not say.
#:
#: All three set ``needs_human``, which is a bit with no reason attached.
#: ``consent_refused`` additionally answers the consent question with a ``False``,
#: because she refused two things in one breath and a perfect agent writes down
#: both; ``_listen`` takes the ``needs_human`` exit first, which is why the
#: refusal never reaches ``CallState.consent_given``.
_TRANSFERS_AT: dict[str, Step] = {
    "consent_refused": Step.DISCLOSE_AND_CONSENT,
    "disputes_the_amount": Step.STATE_BALANCE,
    "hardship_disclosed": Step.OFFER_PAYMENT_PATH,
}

#: The one case that verifies on a date of birth instead of a CPF (CONTRACT §9).
#:
#: A driver that handed everybody a date of birth would make this case
#: indistinguishable from the baseline and would silently stop exercising the CPF
#: path for the other twelve reachable customers — which is the failure
#: ``golden_v1``'s own docstring warns the harness about, so it is asserted below
#: rather than left to a reader of this constant.
_VERIFIES_BY_BIRTH_DATE = frozenset({"no_cpf_over_phone"})


def _disputes_step(case: SyntheticCase) -> Step:
    """The turn a perfect agent writes this case's disputes down on.

    Collections has no dedicated dispute-collection step, and that asymmetry with
    healthcare's ``collect_allergies`` is the whole finding of
    ``disputes_the_amount``: a dispute is not solicited, it arrives on whichever
    turn the customer raises it, and for every dispute declared in ``golden_v1``
    that turn is the one that transfers. ``capture_commitment`` is the fallback
    for a hypothetical case that raises one and still completes, so such a case
    would be *recorded* rather than silently dropped by this driver.
    """
    return _TRANSFERS_AT.get(case.case_id, Step.CAPTURE_COMMITMENT)


def _selected_path(case: SyntheticCase) -> PaymentPath | None:
    """The approved path this case chose, read off its own commitment row.

    :class:`~trail.models.CaseExpectation` does not pin ``selected_path`` — the
    conftest stub leaves it null for that reason — but callback rule 2 fires on a
    null path, so a driver that never named one would send all fifteen cases to
    ``completed_needs_callback`` and prove nothing. The only place a case states
    which of the four it took is ``PaymentCommitment.method``, so that is where
    this reads it, and nowhere else: the turns have to come from the fixture or
    this file is measuring the graph against a second copy of the graph.

    ``None`` for a case that never reached ``capture_commitment``. The two that
    transfer before it never reach ``offer_payment_path``'s rule either.
    """
    return next(
        (row.method for row in case.expectation.expected_commitments if row.method),
        None,
    )


def _extraction(case: SyntheticCase, step: Step, **fields: object) -> TurnExtraction:
    """One turn as a perfect agent would have written it down.

    The three bits that are not per-step arguments — ``unresolved``,
    ``needs_human`` and the dispute rows — are decided here from the case rather
    than at each call site, so that a case which transfers does so on the turn the
    customer actually said the thing, and the rows they said it with ride on the
    same extraction.
    """
    transfers_at = _TRANSFERS_AT.get(case.case_id)
    return TurnExtraction(
        step=step,
        raw_utterance=f"<{case.case_id} at {step.value}>",
        understood=True,
        unresolved=_UNRESOLVED_AT.get(case.case_id) is step,
        needs_human=transfers_at is step,
        disputes=(
            list(case.expectation.expected_disputes)
            if _disputes_step(case) is step
            else []
        ),
        **fields,  # type: ignore[arg-type]
    )


def _identity(case: SyntheticCase) -> TurnExtraction:
    """The right party, stating the identifiers CONTRACT §9 accepts.

    Family name plus an exact, checksum-valid CPF for twelve of the thirteen
    reachable customers; family name plus an exact date of birth for the one who
    declines to read a national identifier to an automated caller, which is good
    behaviour rather than evasive behaviour and is why the fallback exists.
    """
    profile = case.profile
    second: dict[str, object] = (
        {"stated_date_of_birth": profile.date_of_birth.isoformat()}
        if case.case_id in _VERIFIES_BY_BIRTH_DATE
        else {"stated_tax_id": profile.tax_id}
    )
    return _extraction(
        case,
        Step.VERIFY_RIGHT_PARTY,
        identity_confirmed=True,
        stated_name=profile.full_name,
        **second,
    )


def _drive(
    case: SyntheticCase,
    protocol: Protocol,
    *,
    commitments: Sequence[PaymentCommitment] | None = None,
) -> CallState:
    """Run one case through the real conversation graph with a perfect agent.

    The turns are derived from the case's own expectation and from nothing else,
    so this measures the graph against the fixture rather than against a second
    copy of the graph.

    ``commitments`` overrides only the rows written at ``capture_commitment``, and
    deliberately does not touch :func:`_selected_path`. An agent that failed to
    write down a promise still heard "manda o link" one step earlier; stripping
    both would confound rule 3 with rule 2 and make the perverse-direction test
    below unreadable.
    """
    graph = machine.build_graph(protocol)
    call_id, _ = machine.open_call(graph, case.profile, case_id=case.case_id)
    expectation = case.expectation
    rows = list(
        expectation.expected_commitments if commitments is None else commitments
    )

    def turn(extraction: TurnExtraction, **fields: object) -> CallState:
        machine.advance(graph, call_id, Turn(extraction=extraction, **fields))  # type: ignore[arg-type]
        state = machine.state_of(graph, call_id)
        assert state is not None
        return state

    if not case.reachable:
        machine.advance(graph, call_id, Turn(override="not_reached"))
        state = machine.state_of(graph, call_id)
        assert state is not None
        return state

    if case.answering_party == "other":
        # A wrong party is recognised by never supplying both identifiers, and
        # that takes MAX_IDENTITY_ATTEMPTS turns rather than one: the first
        # incomplete answer earns a reprompt, because somebody who has not
        # finished answering is not yet evidence of a wrong party. The case
        # carries two scripted turns for exactly this reason.
        for _ in range(MAX_IDENTITY_ATTEMPTS):
            state = turn(
                _extraction(case, Step.VERIFY_RIGHT_PARTY, identity_confirmed=False)
            )
            if state.finished:
                break
        return state

    state = turn(_identity(case))
    if state.finished:
        return state

    state = turn(
        _extraction(
            case,
            Step.DISCLOSE_AND_CONSENT,
            consent_given=_TRANSFERS_AT.get(case.case_id)
            is not Step.DISCLOSE_AND_CONSENT,
        )
    )
    if state.finished:
        return state

    # `state_balance` collects nothing — it asserts. The only field a turn here
    # can carry is a correction, which is `needs_human` plus a dispute row, and
    # `_extraction` has already put both on the one case that raises one.
    state = turn(_extraction(case, Step.STATE_BALANCE))
    if state.finished:
        return state

    # A case that expects an unconfirmed restatement gets two wrong attempts; the
    # retry path itself is exercised in test_state_machine.py, and a case that
    # recovers on the second attempt reaches the same record as one that lands it
    # on the first.
    verdicts = [True] if expectation.expected_terms_confirmed else [False, False]
    for verdict in verdicts:
        state = turn(_extraction(case, Step.CONFIRM_TERMS), terms_correct=verdict)
    if state.finished:
        return state

    state = turn(
        _extraction(case, Step.OFFER_PAYMENT_PATH, selected_path=_selected_path(case))
    )
    if state.finished:
        return state

    state = turn(_extraction(case, Step.CAPTURE_COMMITMENT, commitments=rows))
    if state.finished:
        return state

    return turn(
        _extraction(
            case,
            Step.CONFIRM_CONTACT,
            contact_channel_confirmed=expectation.expected_contact_channel,
        )
    )


@pytest.mark.parametrize("case", GOLDEN_SET, ids=CASE_IDS)
def test_a_perfect_agent_reaches_the_terminal_state_the_case_declares(
    case: SyntheticCase, real_protocol: Protocol
) -> None:
    """THE CROSS-CHECK THAT WAS MISSING.

    Fifteen cases, driven through the real state machine with exactly the record
    each one declares. If a case cannot reach its own expected terminal state,
    then no agent can, however good — and the metric that counts those states is
    measuring the seam rather than the system.
    """
    state = _drive(case, real_protocol)

    assert state.finished, case.case_id
    assert state.terminal_state is case.expectation.expected_terminal_state


@pytest.mark.parametrize("case", GOLDEN_SET, ids=CASE_IDS)
def test_a_perfect_agent_produces_the_record_the_case_declares(
    case: SyntheticCase, real_protocol: Protocol
) -> None:
    """The terminal state is not the whole expectation, and the rest is scored.

    ``metrics.score_case`` reads four more things off the record — the commitment
    rows, the dispute rows, the restatement verdict and the contact channel — and
    a case whose declared values are unreachable through the graph would produce
    findings against a perfect agent, in exactly the same invisible way an
    unreachable terminal state pins ``fully_automated_rate``.

    The nulls are as load-bearing as the values. A call that ended before
    ``confirm_terms`` leaves ``terms_confirmed`` null rather than false, and the
    two cases that do so declare ``None`` for precisely that reason: pinning a
    field a call cannot reach would put a guaranteed omission into
    ``terms_confirmation_rate``'s denominator.
    """
    state = _drive(case, real_protocol)
    expectation = case.expectation

    assert state.commitments == list(expectation.expected_commitments), case.case_id
    assert state.disputes == list(expectation.expected_disputes), case.case_id
    assert state.terms_confirmed is expectation.expected_terms_confirmed, case.case_id
    assert state.contact_channel_confirmed is expectation.expected_contact_channel, (
        case.case_id
    )


def test_the_forty_percent_ceiling_is_reachable_and_not_just_declared(
    real_protocol: Protocol,
) -> None:
    """6/15 is a ceiling only if a perfect agent can actually touch it.

    The golden set, the README and ``test_golden_set.py`` all state the ceiling
    as six of fifteen. This asserts the machine agrees — which it did not, one
    industry over, and the gap was invisible because each side had a test proving
    only itself.
    """
    automated = [
        case
        for case in GOLDEN_SET
        if _drive(case, real_protocol).terminal_state
        is TerminalState.COMPLETED_NO_CALLBACK
    ]

    assert len(automated) == 6
    assert len(automated) / len(GOLDEN_SET) == pytest.approx(0.40)


def test_an_agent_that_omits_every_commitment_gains_almost_nothing_and_the_gain_is_named(
    real_protocol: Protocol,
) -> None:
    """The perverse direction, measured rather than assumed away.

    The first assertion is the fixture arithmetic that made the healthcare defect
    total: **every** case expecting ``completed_no_callback`` reports at least one
    commitment, so a presence rule puts the ceiling at zero and the only way to
    score above it is to record nothing. The headline metric would then be
    anti-correlated with extraction quality, in the flattering direction, and this
    file exists to stop that returning.

    Under the completeness rule the exposure is **one case in fifteen**, and it is
    worth stating exactly why it is not zero. From inside a call, "the customer
    promised nothing" and "the agent heard nothing" are the same extraction; a
    capture model cannot detect its own omission, and inventing a rule that
    guessed would be the interpretation this design refuses. So
    ``partial_commitment`` — the only case whose callback rests solely on an
    incomplete commitment row — can be flipped by dropping the row.

    The compensating control is the layer built for it. That run scores an
    omission on this case, drives ``commitment_entity_accuracy`` to zero against a
    pre-registered ``>= 0.95`` bar, and the failure taxonomy exists precisely so
    omission is counted rather than averaged. Trading 6.7 points of automation for
    a failed bar and a wall of omissions is not a trade the scorecard hides — and
    ``promise_capture_rate`` moves the other way at the same time, which is the
    metric this port added for exactly this reason: a run that stopped writing
    promises down cannot quietly look like a run that got more of them.
    """
    assert all(
        case.expectation.expected_commitments
        for case in GOLDEN_SET
        if case.expectation.expected_terminal_state
        is TerminalState.COMPLETED_NO_CALLBACK
    )

    flipped = [
        case.case_id
        for case in GOLDEN_SET
        if case.expectation.expected_terminal_state
        is TerminalState.COMPLETED_NEEDS_CALLBACK
        and _drive(case, real_protocol, commitments=[]).terminal_state
        is TerminalState.COMPLETED_NO_CALLBACK
    ]

    assert flipped == ["partial_commitment"]
    assert (6 + len(flipped)) / len(GOLDEN_SET) < 0.50


def test_difficulty_mentioned_in_passing_changes_no_terminal_state(
    real_protocol: Protocol,
) -> None:
    """The flagship CONTRACT §7 case, driven rather than asserted about.

    ``mentions_difficulty_in_passing`` is identical to a clean call except for one
    sentence about a tight month, dismissed by the speaker herself and followed by
    a complete promise to pay. Its expected outcome is the one the identical call
    would have without that sentence. A machine that produced
    ``completed_needs_callback`` — or worse, ``transferred_to_human`` — here would
    read as "the agent classified a vulnerable customer and routed on it", which
    is the finding the case exists to test, even when the real cause was an
    unrelated rule elsewhere in ``advance``.
    """
    difficulty = next(
        case for case in GOLDEN_SET if case.case_id == "mentions_difficulty_in_passing"
    )
    baseline = next(
        case for case in GOLDEN_SET if case.case_id == "canonical_cooperative"
    )

    assert (
        _drive(difficulty, real_protocol).terminal_state
        is _drive(baseline, real_protocol).terminal_state
        is TerminalState.COMPLETED_NO_CALLBACK
    )


def test_a_transfer_keeps_what_the_customer_said(real_protocol: Protocol) -> None:
    """``transferred_to_human`` **and** a non-empty dispute list, on the same case.

    This is the combination ``disputes_the_amount`` exists to pin, and it is the
    difference between a transfer that keeps what the customer said and one that
    throws it away. ``machine._listen`` writes ``commitments`` and ``disputes``
    into the state update *before* the ``needs_human`` branch, so the row survives
    the exit — and the exit is taken on precisely the turn the row was said,
    because an explicit dispute is both the most important thing a customer says
    and something the approved script cannot answer.

    The specialist who picks this call up already has his words in front of them
    and does not have to ask a man who has explained himself to explain himself
    again. FDCPA §809(b) cease-collection-on-dispute is that person's action, on
    their own reading of these words.

    What the transfer does *not* carry is why it happened. The second half of this
    test is the proof: ``consent_refused`` leaves through the same node, on a
    different step, for an unrelated reason, and says the identical sentence.
    """
    disputed = next(c for c in GOLDEN_SET if c.case_id == "disputes_the_amount")
    refused = next(c for c in GOLDEN_SET if c.case_id == "consent_refused")

    state = _drive(disputed, real_protocol)

    assert state.terminal_state is TerminalState.TRANSFERRED_TO_HUMAN
    assert state.disputes == list(disputed.expectation.expected_disputes)
    assert state.disputes, "the transfer threw away what the customer said"
    assert state.step is Step.STATE_BALANCE

    other = _drive(refused, real_protocol)

    assert other.terminal_state is TerminalState.TRANSFERRED_TO_HUMAN
    assert other.step is Step.DISCLOSE_AND_CONSENT
    assert other.disputes == []
    assert state.agent_utterance == other.agent_utterance == TRANSFER_TO_HUMAN_UTTERANCE


def test_only_the_no_cpf_case_verifies_on_a_birth_date(
    real_protocol: Protocol,
) -> None:
    """The CONTRACT §9 fallback is exercised by exactly one case, and no more.

    ``golden_v1`` names this as a harness obligation rather than a fixture one: a
    driver that handed every customer a date of birth would satisfy the identity
    gate everywhere and stop testing the CPF path entirely, and nothing would go
    red. So the shape of the driver's own identity turns is asserted here.

    The asymmetry the gate keeps is not weakened by the fallback: a CPF that *was*
    stated and does not match is not repaired by a correct birthday, because a
    stated identifier is a claim and a wrong claim is a stronger signal than a
    missing one. What this customer demonstrates is the legitimate absence.
    """
    by_birth_date = [
        case.case_id
        for case in GOLDEN_SET
        if case.reachable
        and case.answering_party == "customer"
        and _identity(case).stated_tax_id is None
    ]

    assert by_birth_date == ["no_cpf_over_phone"]

    case = next(c for c in GOLDEN_SET if c.case_id == "no_cpf_over_phone")
    identity = _identity(case)

    assert identity.stated_date_of_birth == case.profile.date_of_birth.isoformat()
    assert (
        _drive(case, real_protocol).terminal_state
        is TerminalState.COMPLETED_NO_CALLBACK
    )


def test_driving_a_case_never_leaves_a_session_mid_call(
    real_protocol: Protocol,
) -> None:
    """Every case ends, and ends with an ``ended_at``.

    A case that ran out of turns without finishing would make the assertions
    above vacuous in the quietest possible way.
    """
    for case in GOLDEN_SET:
        state = _drive(case, real_protocol)

        assert state.finished, case.case_id
        assert isinstance(state.ended_at, datetime), case.case_id
        assert state.ended_at.tzinfo is not None, case.case_id


def test_the_machine_needs_no_network_database_or_key_to_answer_any_of_this() -> None:
    """Stated as a test because it is the property that makes the file possible.

    A node is handed one extraction and returns what the agent says next.
    Nothing above opened a socket, and a ``CallState`` can be built from nothing
    but a profile and a clock.
    """
    state = machine.new_call(GOLDEN_SET[0].profile)

    assert state.step is Step.VERIFY_RIGHT_PARTY
    assert state.needs_callback is False
    assert state.terminal_state is None
