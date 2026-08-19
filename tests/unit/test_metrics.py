"""Metric arithmetic, and the denominators that make it honest.

The primary metric is the fully-automated completion rate, and the only thing
that can quietly ruin it is its denominator. The one named public collections
funnel there is — SET Financial's — headlines 11.8% of *live conversations*
ending with a payment link; put the 12,800 attempts back underneath the 1,360
live conversations and the same funnel reads about 1.2%. Dividing by answered
calls instead of *scheduled accounts* is that trap one layer up: it additionally
deletes the ~72% of outbound attempts that never reach a person at all, a
population a voice agent cannot fix because the cause is a wrong number.

:func:`test_the_fully_automated_rate_divides_by_scheduled_accounts` and
:func:`test_an_unreached_customer_lowers_the_rate_rather_than_vanishing_from_it`
are the honest-denominator guarantee. They fail if anyone ever swaps the
denominator for connected calls, which is the whole reason they are named after
the property rather than after the function.

The same discipline is what the promise-capture tests are about from the other
side. ``promise_capture_rate`` is the metric this industry reports *instead* of
money, so it is published here over the identical denominator as automation and
tested for the ways the two come apart — including the deliberate disagreement
with callback rule 3, which is universal over the promise rows while this metric
is existential over them.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from tests.conftest import (
    DrivenCall,
    commitment_turn,
    complete_commitment,
    contact_turn,
    partial_commitment,
)
from trail.agent.llm import compute_cost_usd
from trail.cases.golden_v1 import AMOUNT_EDGE_CASE
from trail.evals.metrics import (
    COST_PER_ASSISTED_CONTACT_USD,
    COST_PER_SELF_SERVICE_CONTACT_USD,
    REGRESSION_TOLERANCES,
    SET_FINANCIAL_FUNNEL,
    SET_FINANCIAL_LIVE_TO_LINK_RATE,
    THRESHOLDS,
    VOICE_CONTAINMENT_TUNED_RANGE,
    check_thresholds,
    compute_metrics,
    cost_per_fully_automated_call_usd,
    detect_regression,
    false_terms_confirmations,
    fully_automated_rate,
    promise_capture_rate,
    set_financial_link_rate_over_attempts,
    terms_confirmation_rate,
    turn_latency_percentiles,
)
from trail.evals.runner import CaseOutcome
from trail.models import (
    AccountProfile,
    CallRecord,
    CaseExpectation,
    FailureKind,
    MetricSet,
    PaymentCommitment,
    PaymentPath,
    Product,
    Step,
    SyntheticCase,
    TerminalState,
)

pytestmark = pytest.mark.unit

_STARTED_AT = datetime(2026, 8, 15, 9, 0, tzinfo=timezone.utc)
_AS_OF = date(2026, 8, 15)
_DAYS_PAST_DUE = 12

_TAX_ID = "11144477735"
"""A synthetic, checksum-valid CPF. Invented; it belongs to nobody."""

AUTOMATED = TerminalState.COMPLETED_NO_CALLBACK
CALLBACK = TerminalState.COMPLETED_NEEDS_CALLBACK
TRANSFERRED = TerminalState.TRANSFERRED_TO_HUMAN
WRONG_PARTY = TerminalState.NOT_RIGHT_PARTY
NOT_REACHED = TerminalState.NOT_REACHED


def _profile(case_id: str) -> AccountProfile:
    """One synthetic delinquent account, mid-window in the 1-30 DPD segment.

    ``due_date`` is derived from ``days_past_due`` against a fixed ``_AS_OF``
    rather than from the wall clock: the pair are two views of the same fact and
    a profile whose figures disagreed would be a wrong fact spoken aloud.
    """
    return AccountProfile(
        account_id=f"BA-{case_id}",
        full_name="Cliente Teste",
        tax_id=_TAX_ID,
        date_of_birth=date(1984, 3, 9),
        phone="+55 11 90000-0199",
        product=Product.PERSONAL_LOAN,
        balance_brl=Decimal("847.32"),
        due_date=_AS_OF - timedelta(days=_DAYS_PAST_DUE),
        days_past_due=_DAYS_PAST_DUE,
    )


def _promise(
    amount: str | None = "R$ 847,32",
    payment_date: str | None = "dia 20",
    *,
    method: PaymentPath | None = PaymentPath.PAY_NOW,
) -> PaymentCommitment:
    """One promise-to-pay, exactly as the customer said it.

    ``amount`` and ``payment_date`` are independently nullable because that is
    the only thing callback rule 3 and :func:`promise_capture_rate` ever read
    about a commitment — never how much, never which day.
    """
    return PaymentCommitment(
        amount=amount,
        date=payment_date,
        method=method,
        source_utterance="posso pagar isso",
    )


def _outcome(
    terminal: TerminalState | None,
    *,
    case_id: str | None = None,
    cost_usd: float = 0.0,
    latencies: Sequence[float] = (),
    expected_terms_confirmed: bool | None = None,
    terms_confirmed: bool | None = None,
    expected_commitments: Sequence[PaymentCommitment] = (),
    commitments: Sequence[PaymentCommitment] = (),
    prompt_version: str = "test-prompt.0",
    model: str = "gpt-5.6-luna",
) -> CaseOutcome:
    """One driven case.

    ``terminal=None`` models a call the harness could not complete — a
    transport error, a contract violation, or a script that ran out. Those
    cases stay in every denominator: a case the harness could not complete is
    not a case that did not happen.
    """
    identifier = case_id or f"case-{uuid4().hex[:8]}"
    case = SyntheticCase(
        case_id=identifier,
        description="A synthetic outcome built for a metric test.",
        profile=_profile(identifier),
        expectation=CaseExpectation(
            expected_terminal_state=terminal or AUTOMATED,
            expected_terms_confirmed=expected_terms_confirmed,
            expected_commitments=list(expected_commitments),
        ),
    )
    if terminal is None:
        return CaseOutcome(case=case, record=None, error="the agent never answered")

    record = CallRecord(
        account_id=case.profile.account_id,
        started_at=_STARTED_AT,
        ended_at=_STARTED_AT + timedelta(minutes=8),
        terminal_state=terminal,
        commitments=list(commitments),
        terms_confirmed=terms_confirmed,
        protocol_version="1.0.0",
        prompt_version=prompt_version,
        model=model,
        cost_usd=cost_usd,
    )
    return CaseOutcome(case=case, record=record, turn_latencies_ms=tuple(latencies))


def _metrics(outcomes: Sequence[CaseOutcome]) -> MetricSet:
    metrics, _ = compute_metrics(
        run_id=uuid4(),
        golden_set_version="test_set",
        outcomes=outcomes,
        fallback_prompt_version="fallback-prompt",
        fallback_model="fallback-model",
    )
    return metrics


# ===========================================================================
# The honest denominator
# ===========================================================================


def test_the_fully_automated_rate_divides_by_scheduled_accounts() -> None:
    """THE HONEST-DENOMINATOR GUARANTEE.

    Eight scheduled accounts. Two were fully automated. Five reached a party
    and landed a record; one was never answered twice over, and one call never
    completed at all.

    The rate is 2/8. It is **not** 2/5. This test fails the moment the
    denominator becomes connected calls, answered calls, or calls the harness
    managed to complete — each of which is a different way of deleting the
    customers the system did worst by.
    """
    outcomes = [
        _outcome(AUTOMATED),
        _outcome(AUTOMATED),
        _outcome(CALLBACK),
        _outcome(TRANSFERRED),
        _outcome(WRONG_PARTY),
        _outcome(NOT_REACHED),
        _outcome(NOT_REACHED),
        _outcome(None),
    ]
    connected = [
        outcome
        for outcome in outcomes
        if outcome.record is not None and outcome.terminal_state is not NOT_REACHED
    ]

    assert len(outcomes) == 8
    assert len(connected) == 5
    assert fully_automated_rate(outcomes) == pytest.approx(2 / 8)
    assert fully_automated_rate(outcomes) < 2 / len(connected)


def test_an_unreached_customer_lowers_the_rate_rather_than_vanishing_from_it() -> None:
    """The same guarantee, stated as a difference.

    A voice agent cannot fix a wrong phone number. Adding a customer it never
    reached must make the number worse, because the account is still past due
    and still has had no conversation about it.
    """
    reached_only = [_outcome(AUTOMATED), _outcome(CALLBACK)]
    with_unreached = [*reached_only, _outcome(NOT_REACHED)]

    assert fully_automated_rate(reached_only) == pytest.approx(1 / 2)
    assert fully_automated_rate(with_unreached) == pytest.approx(1 / 3)


def test_a_case_the_harness_could_not_drive_counts_against_the_rate() -> None:
    """A run that cannot drive its cases has not automated them."""
    assert fully_automated_rate([_outcome(AUTOMATED), _outcome(None)]) == pytest.approx(
        0.5
    )


def test_the_rate_of_an_empty_run_is_zero_rather_than_undefined() -> None:
    assert fully_automated_rate([]) == 0.0


def test_reached_is_reported_and_never_used_as_a_denominator() -> None:
    """``reached`` exists so the blind spot is visible, not so it can divide."""
    outcomes = [_outcome(AUTOMATED), _outcome(NOT_REACHED), _outcome(None)]

    metrics = _metrics(outcomes)

    assert metrics.scheduled_accounts == 3
    assert metrics.reached == 1
    assert metrics.fully_automated_rate == pytest.approx(1 / 3)
    assert metrics.fully_automated_rate != pytest.approx(1 / metrics.reached)


def test_a_wrong_party_who_picked_up_counts_as_reached() -> None:
    """Reached is a telephony fact: somebody answered, whoever they were."""
    metrics = _metrics([_outcome(WRONG_PARTY), _outcome(NOT_REACHED)])

    assert metrics.reached == 1


# ===========================================================================
# Promise capture — the metric this industry reports instead of money
# ===========================================================================


def test_promise_capture_shares_the_denominator_and_asks_a_different_question() -> None:
    """Both rates over four scheduled accounts, and they disagree in both directions.

    A transferred call where the customer named an amount and a day captured a
    promise and automated nothing; a call that ran to a clean finish without one
    automated and captured nothing. 1/4 each, over the same denominator, on
    disjoint cases — which is exactly why both are published. A vendor quoting
    either as though it were the other is BLUEPRINT §6's critique of this
    industry, and neither of them is cash received.
    """
    promised_then_transferred = _outcome(TRANSFERRED, commitments=[_promise()])
    clean_but_promiseless = _outcome(AUTOMATED)
    outcomes = [
        promised_then_transferred,
        clean_but_promiseless,
        _outcome(CALLBACK),
        _outcome(NOT_REACHED),
    ]

    assert promise_capture_rate(outcomes) == pytest.approx(1 / 4)
    assert fully_automated_rate(outcomes) == pytest.approx(1 / 4)
    assert promise_capture_rate([promised_then_transferred]) == 1.0
    assert fully_automated_rate([promised_then_transferred]) == 0.0
    assert promise_capture_rate([clean_but_promiseless]) == 0.0
    assert fully_automated_rate([clean_but_promiseless]) == 1.0


def test_a_promise_missing_its_date_is_not_a_captured_promise() -> None:
    """Nullity, both fields, the same test callback rule 3 makes.

    An amount with no day is not something the bank can act on, and counting it
    would inflate the one number this industry already over-reports.
    """
    assert promise_capture_rate([_outcome(CALLBACK, commitments=[_promise()])]) == 1.0
    assert (
        promise_capture_rate(
            [_outcome(CALLBACK, commitments=[_promise(payment_date=None)])]
        )
        == 0.0
    )
    assert (
        promise_capture_rate([_outcome(CALLBACK, commitments=[_promise(amount=None)])])
        == 0.0
    )


def test_a_blank_capture_is_absent_rather_than_present() -> None:
    """The one refinement over a bare ``is not None`` test.

    A scorer that counted ``"   "`` as a captured promise would flatter the run
    for free. No golden-set case carries such a value, so this is a guard rather
    than a divergence from the routing rule.
    """
    blank = _outcome(CALLBACK, commitments=[_promise(amount="   ", payment_date=" ")])

    assert promise_capture_rate([blank]) == 0.0


def test_promise_capture_never_reads_how_much_was_promised() -> None:
    """R$ 4.000,00 and R$ 40,00 are the same promise to this metric.

    The same property callback rule 3 holds, for the same reason: deciding which
    *amounts* count is customer-specific logic, and neither the machine nor the
    scorecard holds a threshold. Reading the value here would quietly
    reintroduce, in the reporting layer, the judgement
    :class:`~trail.models.CallRecord` has no field to carry.
    """
    large = _outcome(AUTOMATED, commitments=[_promise(amount="R$ 4.000,00")])
    small = _outcome(AUTOMATED, commitments=[_promise(amount="R$ 40,00")])

    assert promise_capture_rate([large]) == promise_capture_rate([small]) == 1.0


def test_a_promise_captured_in_words_counts_the_same_as_one_in_digits() -> None:
    """ "Mil e duzentos" is a promise. The metric never parses it to find out.

    The agent captures verbatim and never normalises, so the record holds
    whatever the customer said. A capture rate that only recognised digits would
    penalise the speech patterns BLUEPRINT §6's fairness work is about, for
    behaviour that is entirely correct.
    """
    spoken = _outcome(
        AUTOMATED,
        commitments=[_promise(amount="mil e duzentos", payment_date="sexta-feira")],
    )

    assert promise_capture_rate([spoken]) == 1.0


def test_the_promise_rate_of_an_empty_run_is_zero_rather_than_undefined() -> None:
    assert promise_capture_rate([]) == 0.0


def test_a_partial_promise_beside_a_complete_one_counts_and_still_flags_a_callback(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
) -> None:
    """THE COME-APART WITH CALLBACK RULE 3, AND IT IS INTENDED.

    Rule 3 is **universal** over the commitment rows — *every* row must carry an
    amount and a date or a specialist phones. ``promise_capture_rate`` is
    **existential** over them — *some* row carrying both means a promise was
    captured. A customer who names one complete promise and one they cannot pin
    down therefore satisfies the metric and trips the rule at the same time.

    That is not a contradiction to be reconciled; it is two honest answers to two
    different questions. Did this call get us a promise we can act on — yes. Did
    this call finish writing down everything the customer said — no, so it does
    not count as automated and a person completes it. Collapsing them would mean
    either dropping a real promise off the funnel or calling an unfinished
    record clean, and the second is the failure this repository exists to refuse.

    The rule half is driven through the real machine rather than restated here,
    so the two halves cannot drift apart in the one place where agreeing with
    yourself would look like a measurement.
    """
    session = drive(sample_profile, step=Step.CAPTURE_COMMITMENT)
    both = [complete_commitment(sample_profile), partial_commitment()]

    session.advance(commitment_turn(*both))
    outcome = session.advance(contact_turn(confirmed=True))

    assert session.state.needs_callback is True
    assert outcome.terminal_state is TerminalState.COMPLETED_NEEDS_CALLBACK

    scored = [_outcome(CALLBACK, commitments=both)]

    assert promise_capture_rate(scored) == 1.0
    assert fully_automated_rate(scored) == 0.0


# ===========================================================================
# Terms restatement
# ===========================================================================


def test_the_terms_denominator_comes_from_the_expectation_not_the_record() -> None:
    """Three cases pin the restatement; two confirmed it. The fourth is out of scope."""
    outcomes = [
        _outcome(AUTOMATED, expected_terms_confirmed=True, terms_confirmed=True),
        _outcome(AUTOMATED, expected_terms_confirmed=True, terms_confirmed=True),
        _outcome(CALLBACK, expected_terms_confirmed=False, terms_confirmed=False),
        _outcome(WRONG_PARTY),
    ]

    assert terms_confirmation_rate(outcomes) == pytest.approx(2 / 3)


def test_an_agent_that_never_asks_scores_zero_and_not_one_hundred_percent() -> None:
    """The same denominator discipline as the primary metric.

    Keying off ``record.terms_confirmed is not None`` would let an agent that
    skipped the restatement entirely drop those calls out of the denominator and
    score a perfect rate for doing nothing.
    """
    never_asked = [
        _outcome(AUTOMATED, expected_terms_confirmed=True, terms_confirmed=None),
        _outcome(AUTOMATED, expected_terms_confirmed=True, terms_confirmed=None),
    ]

    assert terms_confirmation_rate(never_asked) == 0.0


def test_a_run_where_no_case_pins_the_restatement_reports_zero() -> None:
    assert terms_confirmation_rate([_outcome(NOT_REACHED)]) == 0.0


# ===========================================================================
# Cost
# ===========================================================================


def test_cost_is_all_the_spend_over_only_the_automated_completions() -> None:
    """Calls needing a callback cost specialist time *plus* AI spend (BLUEPRINT §7).

    They belong in the numerator and not the denominator. Reporting cost per
    call, or per connected minute, would flatter the system by spreading spend
    over work it did not finish.
    """
    outcomes = [
        _outcome(AUTOMATED, cost_usd=0.20),
        _outcome(CALLBACK, cost_usd=0.30),
        _outcome(TRANSFERRED, cost_usd=0.10),
        _outcome(NOT_REACHED, cost_usd=0.02),
    ]

    assert cost_per_fully_automated_call_usd(outcomes) == pytest.approx(0.62)


def test_cost_is_undefined_and_not_zero_when_nothing_was_automated() -> None:
    """``0.0`` would pass the ``<= $1.84`` bar on the worst possible run.

    Cost per fully-automated call has no value when the denominator is empty.
    Reporting free spend there is not a conservative simplification, it is a
    green threshold on a run that automated nothing.
    """
    outcomes = [_outcome(CALLBACK, cost_usd=0.30)]

    assert cost_per_fully_automated_call_usd(outcomes) is None


def test_spend_on_a_call_that_never_returned_a_record_is_invisible() -> None:
    """A stated limitation, tested so it stays stated.

    Spend on a call that never landed cannot be seen over the HTTP interface, so
    it is excluded — which biases this number *downward*. The report shows how
    many cases produced no terminal state so the size of the blind spot is
    visible rather than assumed away.
    """
    outcomes = [_outcome(AUTOMATED, cost_usd=0.20), _outcome(None)]

    assert cost_per_fully_automated_call_usd(outcomes) == pytest.approx(0.20)


def test_the_cost_formula_prices_cached_input_at_a_tenth_of_fresh_input() -> None:
    """One formula, used everywhere, so the three cost figures cannot disagree.

    ``gpt-5.6-luna`` is $0.20/MTok input and $1.20/MTok output; cache reads are a
    tenth of the input rate. The first argument is the *uncached remainder*, not
    the provider's raw ``input_tokens`` total, so the terms do not double-count
    (INTERFACES §6).
    """
    assert compute_cost_usd(1_000_000, 0, 0) == pytest.approx(0.20)
    assert compute_cost_usd(0, 1_000_000, 0) == pytest.approx(1.20)
    assert compute_cost_usd(0, 0, 1_000_000) == pytest.approx(0.02)
    assert compute_cost_usd(1_000, 100, 700) == pytest.approx(0.000334)


def test_the_cost_formula_charges_no_cache_write_premium() -> None:
    """This provider caches automatically and bills no write premium.

    The parameter survives because ``LLMCallTrace`` and the records table carry
    the column, and a provider with write-priced caching would need it again.
    Pinning it at zero here means a future provider swap that reintroduces the
    charge has to change this test deliberately rather than inherit a silent
    under-count.
    """
    assert compute_cost_usd(0, 0, 0, 1_000_000) == pytest.approx(0.0)


def test_the_vendor_funnel_is_derived_from_its_inputs_and_not_hardcoded() -> None:
    """151 links over 12,800 attempts — the same denominator argument, borrowed.

    Derived from :data:`SET_FINANCIAL_FUNNEL` so it cannot drift from it. The
    headline 11.8% is a *conditional* rate, links per live conversation; put the
    attempts back underneath and the same funnel reads about 1.2%. Both figures
    are true, they answer different questions, and printing them side by side is
    the cheapest inoculation there is against quoting the wrong one.

    The 11.1% the funnel itself implies and the 11.8% the vendor headlines are
    left disagreeing on purpose, so this test pins the gap rather than tidying
    it away.
    """
    attempts, live, links, _transfers = SET_FINANCIAL_FUNNEL

    assert set_financial_link_rate_over_attempts() == pytest.approx(links / attempts)
    assert set_financial_link_rate_over_attempts() == pytest.approx(0.0118, abs=0.0005)
    assert set_financial_link_rate_over_attempts() < SET_FINANCIAL_LIVE_TO_LINK_RATE / 5
    assert links / live == pytest.approx(0.111, abs=0.001)
    assert links / live != pytest.approx(SET_FINANCIAL_LIVE_TO_LINK_RATE, abs=0.001)


# ===========================================================================
# Latency
# ===========================================================================


def test_latency_percentiles_use_nearest_rank_and_report_real_observations() -> None:
    """On a few dozen samples an interpolated p95 reports a latency nobody saw."""
    outcomes = [
        _outcome(AUTOMATED, latencies=list(range(1, 11))),
        _outcome(AUTOMATED, latencies=list(range(11, 21))),
    ]

    p50, p95 = turn_latency_percentiles(outcomes)

    assert (p50, p95) == (10, 19)


def test_a_run_with_no_turns_reports_zero_latency() -> None:
    assert turn_latency_percentiles([_outcome(NOT_REACHED)]) == (0.0, 0.0)


# ===========================================================================
# The whole MetricSet
# ===========================================================================


def test_every_terminal_state_appears_in_the_counts_even_at_zero() -> None:
    """A state with no calls is a zero, not a missing key.

    The report prints all five; a missing key would silently shorten the table
    and hide the outcome nobody wants to look at.
    """
    metrics = _metrics([_outcome(AUTOMATED)])

    assert set(metrics.terminal_state_counts) == set(TerminalState)
    assert metrics.terminal_state_counts[AUTOMATED] == 1
    assert metrics.terminal_state_counts[NOT_REACHED] == 0


def test_the_taxonomy_counts_every_finding_the_run_produced() -> None:
    """``findings_by_kind`` is a breakdown of the findings list, not a subset."""
    outcomes = [
        _outcome(
            CALLBACK,
            expected_commitments=[_promise(amount="R$ 300,00", payment_date="dia 20")],
            commitments=[],
        ),
        _outcome(AUTOMATED, expected_terms_confirmed=True, terms_confirmed=None),
    ]

    metrics, findings = compute_metrics(
        run_id=uuid4(),
        golden_set_version="test_set",
        outcomes=outcomes,
        fallback_prompt_version="fallback-prompt",
        fallback_model="fallback-model",
    )

    assert set(metrics.findings_by_kind) == set(FailureKind)
    assert sum(metrics.findings_by_kind.values()) == len(findings)
    assert metrics.findings_by_kind[FailureKind.OMISSION] >= 4


def test_an_amount_the_agent_normalised_lands_in_the_wrong_value_column() -> None:
    """BY DESIGN, and it is the only failure ``commitment_entity_accuracy`` catches.

    The customer said "mil e duzentos" and the agent wrote down "R$ 1.200,00" —
    the one thing verbatim capture forbids. Scoring is string equality, so this
    is a ``WRONG_VALUE``. Pairing is money-aware, so it is *one* wrong value
    rather than an omission plus a fabrication: the two rows are agreed to be the
    same promise, and then judged to say different things.

    Nothing else in the harness sees it. ``promise_capture_rate`` is a nullity
    test, callback rule 3 reads nullity, and the compliance gate inspects only
    what the agent spoke. A scorer that parsed both sides first would report 3/3
    and no findings on precisely the run that manufactured a figure.
    """
    said = _promise(amount="mil e duzentos", payment_date="dia primeiro")
    written = _promise(amount="R$ 1.200,00", payment_date="dia primeiro")

    metrics, findings = compute_metrics(
        run_id=uuid4(),
        golden_set_version="test_set",
        outcomes=[
            _outcome(AUTOMATED, expected_commitments=[said], commitments=[written])
        ],
        fallback_prompt_version="fallback-prompt",
        fallback_model="fallback-model",
    )

    assert metrics.findings_by_kind[FailureKind.WRONG_VALUE] == 1
    assert metrics.findings_by_kind[FailureKind.OMISSION] == 0
    assert metrics.findings_by_kind[FailureKind.FABRICATION] == 0
    assert [finding.field for finding in findings] == ["commitments[0].amount"]
    assert metrics.commitment_slots_scored == 3
    assert metrics.commitment_entity_accuracy == pytest.approx(2 / 3)


def test_the_report_is_stamped_with_the_versions_the_agent_ran_not_the_harness() -> (
    None
):
    """The evals container has its own config, and it is not what was measured.

    A report stamping the harness's prompt version onto the agent's results is
    describing a system that was never run.
    """
    outcomes = [_outcome(AUTOMATED, prompt_version="agent-prompt.7", model="opus-x")]

    metrics = _metrics(outcomes)

    assert metrics.prompt_version == "agent-prompt.7"
    assert metrics.model == "opus-x"


def test_the_harness_config_is_used_only_when_no_record_landed_at_all() -> None:
    metrics = _metrics([_outcome(None)])

    assert metrics.prompt_version == "fallback-prompt"
    assert metrics.model == "fallback-model"


def test_a_run_that_produced_nothing_reports_no_metric_rather_than_a_perfect_one() -> (
    None
):
    """AN EMPTY DENOMINATOR IS NOT A PASS.

    A run in which every call failed used to print ``commitment_entity_accuracy
    100.0% PASS`` and ``cost_per_fully_automated $0.00 PASS`` — two of the
    pre-registered bars met by a run that produced nothing at all. That is the
    "an infrastructure failure and a quality failure must not look alike" rule
    from ``runner._preflight``, defeated one layer down by two sentinels.

    Both metrics are ``None`` here, the sample size is carried so an accuracy can
    never be read without it, and ``check_thresholds`` scores neither — which is
    what the report reads to print ``n/a`` in the verdict column.

    Note which rates are *not* undefined. ``fully_automated_rate`` and
    ``promise_capture_rate`` divide by scheduled accounts, and this run had one,
    so their zeroes are measurements rather than sentinels and they fail their
    bars honestly.
    """
    metrics = _metrics([_outcome(NOT_REACHED)])

    assert metrics.commitment_entity_accuracy is None
    assert metrics.commitment_slots_scored == 0
    assert metrics.cost_per_fully_automated_call_usd is None
    assert metrics.promise_capture_rate == 0.0
    assert sum(metrics.findings_by_kind.values()) == 0

    results = check_thresholds(metrics)
    undefined = {result.threshold.metric for result in results if result.undefined}

    assert undefined == {
        "commitment_entity_accuracy",
        "cost_per_fully_automated_call_usd",
    }
    assert all(result.passed is None for result in results if result.undefined)
    assert all(result.passed is not True for result in results if result.undefined)
    assert (
        next(r for r in results if r.threshold.metric == "promise_capture_rate").passed
        is False
    )


def test_a_falsely_confirmed_restatement_is_counted_and_gated() -> None:
    """The failure the terms check exists for raises its own rate.

    ``terms_confirmation_rate`` counts confirmations over the cases where the
    restatement is in scope, so an agent that records "confirmed" for the
    customer who said the wrong figure moves the metric *up*. The companion
    counter is what makes that unprofitable, and its pre-registered bar is zero:
    a customer who leaves the call certain of an amount the bank never agreed to
    is a broken promise the system manufactured.
    """
    honest = [
        _outcome(AUTOMATED, expected_terms_confirmed=True, terms_confirmed=True),
        _outcome(CALLBACK, expected_terms_confirmed=False, terms_confirmed=False),
    ]
    flattering = [
        _outcome(AUTOMATED, expected_terms_confirmed=True, terms_confirmed=True),
        _outcome(CALLBACK, expected_terms_confirmed=False, terms_confirmed=True),
    ]

    assert terms_confirmation_rate(honest) == pytest.approx(0.5)
    assert terms_confirmation_rate(flattering) == pytest.approx(1.0)

    assert false_terms_confirmations(honest) == 0
    assert false_terms_confirmations(flattering) == 1

    bar = next(t for t in THRESHOLDS if t.metric == "false_terms_confirmations")
    result = next(
        r
        for r in check_thresholds(_metrics(flattering))
        if r.threshold.metric == "false_terms_confirmations"
    )

    assert (bar.direction, bar.value) == ("max", 0.0)
    assert result.passed is False


def test_a_compliance_violation_is_not_folded_into_the_extraction_taxonomy() -> None:
    """BLUEPRINT §6 defines the three kinds over *record values*.

    A ``must_not_contain`` hit is a phrase the agent spoke, not a value in a
    record, so counting it as a fabrication would inflate one column of the
    table whose stated purpose is to show that omission dominates. It still
    reaches the report as a finding; ``compliance_violations`` is the only
    counter that owns it.

    The phrase is the amount case's own canary: a customer who said "mil e
    duzentos" hearing "doze mil reais" read back to her is the 8-becomes-80 error
    class with money in it.
    """
    case = AMOUNT_EDGE_CASE
    outcome = CaseOutcome(
        case=case,
        record=None,
        agent_utterances=("Então são doze mil reais, certo?",),
    )

    metrics, findings = compute_metrics(
        run_id=uuid4(),
        golden_set_version="test_set",
        outcomes=[outcome],
        fallback_prompt_version="fallback-prompt",
        fallback_model="fallback-model",
    )

    assert metrics.compliance_violations == 1
    assert any(f.detail.startswith("COMPLIANCE VIOLATION") for f in findings)
    assert sum(metrics.findings_by_kind.values()) == len(findings) - 1
    assert metrics.findings_by_kind[FailureKind.FABRICATION] == 0


# ===========================================================================
# Pre-registered thresholds
# ===========================================================================


def test_the_automation_bar_is_the_cold_launch_floor_and_not_the_tuned_range() -> None:
    """30% — the cold-launch end of the practitioner containment range.

    A threshold edited after seeing the number is not a threshold, it is a
    description. This bar sits below the 45-55% tuned range on purpose: this
    agent has never met a real caller, and a bar borrowed from tuned deployments
    would be a bar this system was never entitled to be measured against.
    """
    bar = next(t for t in THRESHOLDS if t.metric == "fully_automated_rate")

    assert bar.direction == "min"
    assert bar.value == 0.30
    assert bar.value < min(VOICE_CONTAINMENT_TUNED_RANGE)


def test_the_promise_capture_bar_carries_the_weakness_of_the_number_it_borrows() -> (
    None
):
    """The only public collections funnel there is, used with its caveats attached.

    The bar is the vendor's live-to-link rate. Two mismatches make it *harder*
    here than there and both are stated in the rationale rather than smoothed
    over: that rate is conditional on reaching a live conversation while this one
    divides by every scheduled account, and a captured promise is not a sent
    link. A borrowed number wrong in the harder direction is still borrowed, and
    the word VENDOR-REPORTED is in the rationale so nobody reads it as evidence
    it is not.
    """
    bar = next(t for t in THRESHOLDS if t.metric == "promise_capture_rate")

    assert bar.direction == "min"
    assert bar.value == SET_FINANCIAL_LIVE_TO_LINK_RATE
    assert "VENDOR-REPORTED" in bar.rationale
    assert bar.value > set_financial_link_rate_over_attempts()


def test_the_cost_bar_is_the_self_service_benchmark_and_not_the_human_one() -> None:
    """Anchored on the incumbent automation, not on the incumbent person.

    A bar at the $13.50 assisted-contact median would pass a system seven times
    cheaper than a specialist and still worse than the IVR the bank already owns.
    "Cheaper than a person" proves nothing for the small early-bucket balances
    this is scoped to, where the alternative was never a person.
    """
    bar = next(t for t in THRESHOLDS if t.metric == "cost_per_fully_automated_call_usd")

    assert bar.direction == "max"
    assert bar.value == COST_PER_SELF_SERVICE_CONTACT_USD
    assert bar.value < COST_PER_ASSISTED_CONTACT_USD


def test_a_bar_with_no_comparator_says_so_in_the_word_declared() -> None:
    """A declared bar and a derived bar are different kinds of claim.

    Two of the eight have nothing published to stand on. A reader who cannot tell
    them apart has been misled by the formatting alone, so the distinction lives
    in the rationale text where the report prints it, and this test is what stops
    a future bar being slipped in without one.
    """
    declared = {t.metric for t in THRESHOLDS if "DECLARED" in t.rationale}

    assert declared == {"fully_automated_rate", "terms_confirmation_rate"}


def test_compliance_violations_are_a_gate_and_not_a_rate() -> None:
    """Zero tolerance. Nothing else on the scorecard trades against it."""
    bar = next(t for t in THRESHOLDS if t.metric == "compliance_violations")

    assert (bar.direction, bar.value) == ("max", 0.0)


def test_every_pre_registered_bar_names_a_field_the_report_can_actually_read() -> None:
    """A typo in a metric name would be an ``AttributeError`` in the middle of a run.

    Both tables are addressed by string, which is what lets the report iterate
    them; this is the cost of that, paid once, offline.
    """
    fields = set(MetricSet.model_fields)

    assert {t.metric for t in THRESHOLDS} <= fields
    assert {t.metric for t in REGRESSION_TOLERANCES} <= fields


def test_every_threshold_is_reported_whether_it_passed_or_failed() -> None:
    """Showing only the failures makes a run look better the more bars you delete."""
    metrics = _metrics([_outcome(AUTOMATED)])

    results = check_thresholds(metrics)

    assert [result.threshold.metric for result in results] == [
        threshold.metric for threshold in THRESHOLDS
    ]


@pytest.mark.parametrize(
    ("rate", "expected"),
    [
        pytest.param(0.500, True, id="above-the-bar"),
        pytest.param(0.300, True, id="exactly-on-the-bar"),
        pytest.param(0.200, False, id="below-the-bar"),
    ],
)
def test_a_minimum_threshold_passes_at_the_bar_and_fails_below_it(
    rate: float, expected: bool
) -> None:
    metrics = _metrics([_outcome(AUTOMATED)]).model_copy(
        update={"fully_automated_rate": rate}
    )

    result = next(
        r
        for r in check_thresholds(metrics)
        if r.threshold.metric == "fully_automated_rate"
    )

    assert result.passed is expected


def test_one_compliance_violation_fails_the_run_outright() -> None:
    metrics = _metrics([_outcome(AUTOMATED)]).model_copy(
        update={"compliance_violations": 1}
    )

    result = next(
        r
        for r in check_thresholds(metrics)
        if r.threshold.metric == "compliance_violations"
    )

    assert result.passed is False


# ===========================================================================
# Regression detection
# ===========================================================================


def _with(metrics: MetricSet, **updates: float | None) -> MetricSet:
    return metrics.model_copy(update=updates)


def test_a_metric_that_slid_past_its_tolerance_is_named() -> None:
    baseline = _metrics([_outcome(AUTOMATED)])
    current = _with(baseline, fully_automated_rate=0.20)
    previous = _with(baseline, fully_automated_rate=0.40)

    statements = detect_regression(current, previous)

    assert len(statements) == 1
    assert statements[0].startswith("fully_automated_rate regressed")


def test_a_move_inside_the_tolerance_is_not_a_regression() -> None:
    """A fifteen-case golden set is quantised: one case is 6.7 points of any rate.

    The slack exists so re-ordering noise does not fire the gate. It is not a
    licence to drift — the budget is per-run, so a metric sliding one tolerance
    per run still trips the moment a single run moves more than its share.
    """
    baseline = _metrics([_outcome(AUTOMATED)])
    current = _with(baseline, fully_automated_rate=0.39)
    previous = _with(baseline, fully_automated_rate=0.40)

    assert detect_regression(current, previous) == []


def test_an_improvement_is_never_reported_because_this_is_a_gate() -> None:
    baseline = _metrics([_outcome(AUTOMATED)])
    current = _with(baseline, fully_automated_rate=0.80)
    previous = _with(baseline, fully_automated_rate=0.40)

    assert detect_regression(current, previous) == []


def test_promise_capture_regresses_on_its_own_line_and_not_with_automation() -> None:
    """The two rates share a denominator and nothing else.

    An agent that starts finishing calls clean without ever capturing a promise
    moves exactly one of them, and that movement is the one worth seeing. Folding
    promise capture in with automation would hide the run where the funnel step
    collapsed and the execution number held.
    """
    baseline = _metrics([_outcome(AUTOMATED)])
    current = _with(baseline, fully_automated_rate=0.40, promise_capture_rate=0.10)
    previous = _with(baseline, fully_automated_rate=0.40, promise_capture_rate=0.30)

    statements = detect_regression(current, previous)

    assert len(statements) == 1
    assert statements[0].startswith("promise_capture_rate regressed")


def test_one_new_compliance_violation_is_a_regression_with_no_slack() -> None:
    baseline = _metrics([_outcome(AUTOMATED)])
    current = _with(baseline, compliance_violations=1)
    previous = _with(baseline, compliance_violations=0)

    tolerance = next(
        t for t in REGRESSION_TOLERANCES if t.metric == "compliance_violations"
    )
    statements = detect_regression(current, previous)

    assert tolerance.slack == 0.0
    assert len(statements) == 1
    assert "compliance_violations regressed" in statements[0]


def test_cost_and_latency_regress_on_a_proportion_rather_than_an_amount() -> None:
    """ "How much worse" is the meaningful question for money and milliseconds."""
    baseline = _metrics([_outcome(AUTOMATED)])
    previous = _with(baseline, cost_per_fully_automated_call_usd=1.00)

    within = _with(baseline, cost_per_fully_automated_call_usd=1.15)
    beyond = _with(baseline, cost_per_fully_automated_call_usd=1.25)

    assert detect_regression(within, previous) == []
    assert len(detect_regression(beyond, previous)) == 1


def test_a_metric_undefined_on_either_side_is_skipped_rather_than_coerced() -> None:
    """THE NULLABLE DISCIPLINE, ONE LAYER FURTHER ON.

    "Cost went from undefined to $0.42" is not a movement, and neither is its
    opposite. Coercing the ``None`` to zero would make the first run that
    automated anything a cost regression against a run that automated nothing —
    an infrastructure failure reported as a quality one, which is the same
    confusion ``check_thresholds`` refuses one layer up.
    """
    baseline = _metrics([_outcome(AUTOMATED)])
    defined = _with(baseline, cost_per_fully_automated_call_usd=0.42)
    undefined = _with(baseline, cost_per_fully_automated_call_usd=None)

    assert detect_regression(defined, undefined) == []
    assert detect_regression(undefined, defined) == []
