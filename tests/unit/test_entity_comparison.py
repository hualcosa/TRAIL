"""Comparing what the customer said against what the record holds.

The taxonomy is the point (BLUEPRINT §6). Every discrepancy resolves to exactly
one of three kinds, and they are never collapsed into a single "wrong":

* **omission** — a fact present in the utterance and absent from the record;
* **fabrication** — a value in the record absent from the utterance;
* **wrong value** — present in both, and different.

The ASR literature is explicit that omission dominates, so a scorecard that only
says "wrong" has nothing to say about the most common failure mode. Everything
below is one worked example of a kind, or of the alignment rule that decides
which kind a mismatch becomes.

Collections adds one seam the clinical original did not have, and it is the
subject of the middle third of this file. A medication has an identity — the
drug's name — distinct from its critical number, the dose. A promise-to-pay has
no identity apart from its values, and the strongest of them is the **amount**,
which is also the number BLUEPRINT §6 cares most about. So ``metrics._align``
pairs rows on the amount and pairs them **money-aware**, while
``metrics._matches`` scores every field by **string equality**: generous about
which two rows are the same promise, strict about whether they say the same
thing.

That split is the headline of this file, and both halves of it are load-bearing.
An agent that tidied "mil e duzentos" into "R$ 1.200,00" produces exactly **one**
wrong value — not a clean score, because money-aware *scoring* was the bug the
scorer was written to avoid, and not an omission-plus-fabrication cascade,
because money-aware *pairing* is what prevents that. An agent that heard "cento e
vinte" does cascade, and correctly: that is a different promise, not the same
promise mis-transcribed.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trail.evals.metrics import CaseScore, score_case
from trail.evals.runner import CaseOutcome
from trail.models import (
    AccountProfile,
    CallRecord,
    CaseExpectation,
    Dispute,
    FailureKind,
    Finding,
    PaymentCommitment,
    PaymentPath,
    Product,
    SyntheticCase,
    TerminalState,
)

pytestmark = pytest.mark.unit

_STARTED_AT = datetime(2026, 8, 15, 9, 0, tzinfo=timezone.utc)
_DAYS_PAST_DUE = 12
_PROFILE = AccountProfile(
    account_id="AUR-TEST-0001",
    full_name="Cliente Teste",
    # Synthetic and checksum-valid, like every CPF in this repository.
    tax_id="11144477735",
    date_of_birth=date(1984, 3, 9),
    phone="+55 11 90000-0199",
    product=Product.PERSONAL_LOAN,
    # R$ 1.200,00, so the fixture promises below are the same money the account
    # actually holds — this file's worked examples are the `amount_edge_case`
    # customer's, and a balance that disagreed with them would make every
    # docstring here a little bit false.
    balance_brl=Decimal("1200.00"),
    due_date=_STARTED_AT.date() - timedelta(days=_DAYS_PAST_DUE),
    days_past_due=_DAYS_PAST_DUE,
)


def _score(
    *,
    expected: Sequence[PaymentCommitment] = (),
    actual: Sequence[PaymentCommitment] = (),
    expected_disputes: Sequence[Dispute] = (),
    actual_disputes: Sequence[Dispute] = (),
) -> CaseScore:
    """Score one case whose only variable is its entity lists.

    Both sides land on the same terminal state and neither side pins a flag, so
    nothing but the entities can produce a finding.
    """
    case = SyntheticCase(
        case_id="entity_comparison",
        description="A fixture that varies only in its commitments and disputes.",
        profile=_PROFILE,
        expectation=CaseExpectation(
            expected_terminal_state=TerminalState.COMPLETED_NEEDS_CALLBACK,
            expected_commitments=list(expected),
            expected_disputes=list(expected_disputes),
        ),
    )
    record = CallRecord(
        account_id=_PROFILE.account_id,
        started_at=_STARTED_AT,
        ended_at=_STARTED_AT + timedelta(minutes=8),
        terminal_state=TerminalState.COMPLETED_NEEDS_CALLBACK,
        commitments=list(actual),
        disputes=list(actual_disputes),
        protocol_version="1.0.0",
        prompt_version="test-prompt.0",
        model="gpt-5.6-luna",
    )
    return score_case(CaseOutcome(case=case, record=record))


def _kinds(findings: Sequence[Finding]) -> list[tuple[str, FailureKind]]:
    return [(finding.field, finding.kind) for finding in findings]


def _promise(**overrides: object) -> PaymentCommitment:
    """The canonical promise-to-pay: an amount, a day, and a chosen path.

    Spelled out, because that is what a transcript produces and what verbatim
    capture is obliged to store.
    """
    fields: dict[str, object] = {
        "amount": "mil e duzentos",
        "date": "dia primeiro",
        "method": PaymentPath.SCHEDULE,
        "source_utterance": "mil e duzentos, dia primeiro — pode agendar",
    }
    fields.update(overrides)
    return PaymentCommitment(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Agreement
# ---------------------------------------------------------------------------


def test_an_exactly_matching_commitment_produces_no_findings() -> None:
    score = _score(expected=[_promise()], actual=[_promise()])

    assert score.findings == []
    assert score.commitment_slots_scored == 3
    assert score.commitment_slots_correct == 3


def test_matching_ignores_case_and_collapses_whitespace() -> None:
    """Case-folding and whitespace are transcription artefacts, not stated facts."""
    score = _score(
        expected=[_promise(amount="mil e duzentos", date="dia primeiro")],
        actual=[_promise(amount="  Mil e   Duzentos ", date="Dia  Primeiro")],
    )

    assert score.findings == []


def test_the_source_utterance_is_provenance_and_is_never_scored() -> None:
    """It is what makes a finding auditable, not a value to grade.

    Demanding an exact match on it would score how the golden set happened to
    slice the transcript rather than whether the amount and the date are right.
    """
    score = _score(
        expected=[_promise(source_utterance="mil e duzentos, dia primeiro")],
        actual=[
            _promise(
                source_utterance=(
                    "Mil e duzentos, dia primeiro. Mil e duzentos, tá? Não é "
                    "cento e vinte e não é doze mil."
                )
            )
        ],
    )

    assert score.findings == []


def test_a_field_neither_side_states_is_not_scored_as_a_success() -> None:
    """A promise with no named payment path is not a path the agent got right.

    Counting empty-on-both-sides slots as correct would inflate accuracy with
    every field the golden set happens not to exercise.
    """
    bare = PaymentCommitment(
        amount="quinhentos reais", source_utterance="eu consigo pagar quinhentos reais"
    )

    score = _score(expected=[bare], actual=[bare])

    assert score.commitment_slots_scored == 1
    assert score.commitment_slots_correct == 1


# ---------------------------------------------------------------------------
# The three kinds
# ---------------------------------------------------------------------------


def test_a_stated_value_missing_from_the_record_is_an_omission() -> None:
    score = _score(expected=[_promise()], actual=[_promise(date=None)])

    assert _kinds(score.findings) == [("commitments[0].date", FailureKind.OMISSION)]
    assert score.findings[0].expected == "dia primeiro"
    assert score.findings[0].actual is None
    assert score.commitment_slots_correct == 2
    assert score.commitment_slots_scored == 3


def test_a_value_the_customer_never_stated_is_a_fabrication() -> None:
    """``date`` is the fabrication canary, and the golden set says why.

    ``partial_commitment`` names a figure and then declines, in as many words, to
    name a day — *"eu não vou prometer dia nenhum sem ter certeza"* — so her
    expected commitment carries ``date=None`` and her case forbids the agent
    speaking a day at all. Callback rule 3 reads that nullity and sends the
    record to a specialist.

    An agent that fills the day in has not saved the callback. It has
    manufactured a promise the customer never made, and the bank's record now
    says it has an agreement that does not exist.
    """
    score = _score(
        expected=[_promise(date=None)], actual=[_promise(date="sexta-feira")]
    )

    assert _kinds(score.findings) == [("commitments[0].date", FailureKind.FABRICATION)]
    assert score.findings[0].expected is None
    assert score.findings[0].actual == "sexta-feira"


def test_a_value_present_on_both_sides_and_different_is_a_wrong_value() -> None:
    """A ``PaymentPath`` reaches the report as the text a human reads."""
    score = _score(
        expected=[_promise(method=PaymentPath.SCHEDULE)],
        actual=[_promise(method=PaymentPath.PAYMENT_LINK)],
    )

    assert _kinds(score.findings) == [
        ("commitments[0].method", FailureKind.WRONG_VALUE)
    ]
    assert (score.findings[0].expected, score.findings[0].actual) == (
        "schedule",
        "payment_link",
    )


@pytest.mark.parametrize(
    ("stated", "recorded"),
    [
        pytest.param("sexta-feira", "21 de agosto de 2026", id="a-relative-date"),
        pytest.param("dia 20", "dia 2", id="a-dropped-digit"),
        pytest.param("dia primeiro", "dia 1º", id="a-word-made-a-numeral"),
        pytest.param("20/08", "08/20", id="a-swapped-day-and-month"),
    ],
)
def test_dates_are_compared_exactly_and_never_normalised(
    stated: str, recorded: str
) -> None:
    """The agent captures a day as said and does not resolve it. Nor does the scorer.

    "sexta-feira" is not a calendar date, and turning it into one requires a
    week, a timezone and an assumption about which Friday. Every convenience
    normalisation — resolving a relative day, reordering a numeric date,
    expanding an ordinal — is a place where a real error gets absorbed into a
    match, so this scorer does none of them. A day the customer never named is a
    promise they never made, and a broken promise the bank manufactured is worse
    than a callback.
    """
    score = _score(expected=[_promise(date=stated)], actual=[_promise(date=recorded)])

    assert _kinds(score.findings) == [("commitments[0].date", FailureKind.WRONG_VALUE)]


# ---------------------------------------------------------------------------
# Amounts — the seam, and the reason this file exists
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stated", "recorded"),
    [
        pytest.param("mil e duzentos", "R$ 1.200,00", id="spelled-out-to-digits"),
        pytest.param("1.200", "R$ 1.200,00", id="cents-padded"),
        pytest.param("847 reais e 32 centavos", "847,32", id="currency-words-dropped"),
    ],
)
def test_an_amount_the_agent_normalised_is_exactly_one_wrong_value(
    stated: str, recorded: str
) -> None:
    """**The headline.** The same money, tidied — one finding, not none and not six.

    Verbatim capture means the record holds the words the customer used, so a
    record reading "R$ 1.200,00" against an expectation reading "mil e duzentos"
    means the agent did the one thing the capture architecture forbids. Both
    halves of the scorer's design are on trial here and they pull opposite ways:

    * ``_matches`` scores by **string equality**, so this is reported at all. An
      earlier version put both sides through ``parse_brl`` first and the two
      strings compared equal — which made ``commitment_entity_accuracy``
      structurally blind to the only failure it exists to catch, and nothing else
      in the harness covers it. ``promise_capture_rate`` is a nullity test,
      callback rule 3 reads nullity, and the compliance gate inspects only what
      the agent *spoke*. A silent normalisation would have scored clean.
    * ``_align`` pairs **money-aware**, so this is reported as one wrong value
      rather than as an omission plus a fabrication. The two rows are the same
      promise; the disagreement is about how it was written down.

    One industry over, a discharge summary turned 8 units of insulin into 80, the
    dose was given, and the patient died (BLUEPRINT §6). This is the same class
    with money in it, and the reason ``commitment_entity_accuracy`` carries the
    same 0.95 bar as dose accuracy did.
    """
    score = _score(
        expected=[_promise(amount=stated)], actual=[_promise(amount=recorded)]
    )

    assert _kinds(score.findings) == [
        ("commitments[0].amount", FailureKind.WRONG_VALUE)
    ]
    assert (score.findings[0].expected, score.findings[0].actual) == (stated, recorded)
    assert score.commitment_slots_correct == 2
    assert score.commitment_slots_scored == 3


def test_a_verbatim_spelled_out_amount_scores_clean() -> None:
    """The other half of the headline, and the answer to the fairness objection.

    The argument against string equality was that it would manufacture findings
    against exactly the speech patterns BLUEPRINT §6's fairness stratification is
    about — the customer who says "mil e duzentos" rather than reading digits off
    a screen. That argument is self-defeating under verbatim capture: the record
    holds the spoken form, so the spoken form is what the expectation is written
    in, and the two match exactly. A money-aware tolerance could only ever have
    absolved the agent, never the customer.
    """
    score = _score(
        expected=[_promise(amount="mil e duzentos")],
        actual=[_promise(amount="mil e duzentos")],
    )

    assert score.findings == []
    assert score.commitment_slots_correct == score.commitment_slots_scored == 3


@pytest.mark.parametrize(
    ("stated", "recorded"),
    [
        pytest.param("mil e duzentos", "cento e vinte", id="a-factor-of-ten"),
        pytest.param("R$ 1.200,00", "R$ 120,00", id="a-factor-of-ten-in-digits"),
        pytest.param("mil e duzentos", "doze mil", id="a-factor-of-ten-upward"),
        pytest.param("847,32", "84,73", id="a-shifted-comma"),
    ],
)
def test_two_genuinely_different_amounts_cascade_into_an_omission_and_a_fabrication(
    stated: str, recorded: str
) -> None:
    """A promise for a different sum is a different promise, and the scorer says so.

    This is the seam ``_align`` documents rather than papers over. Pairing on the
    amount is what lets a wrong *date* be reported as a wrong date; the price is
    that a wrong *amount* has nothing left to pair on, so it reads as one promise
    lost and one promise invented. The alternative — pairing positionally so the
    amount could always be scored as a wrong value — was rejected because it
    mis-pairs the whole list the moment the agent records one promise too many,
    which is precisely the run where the table is being read.

    So ``findings_by_kind`` has to be read knowing this, and the shape is
    defensible on its own terms: "mil e duzentos" heard as "cento e vinte" is not
    a typo in an agreement, it is an agreement that was never reached. Neither
    the amount, nor the day, nor the path attached to it survives, and all three
    are reported.
    """
    score = _score(
        expected=[_promise(amount=stated)], actual=[_promise(amount=recorded)]
    )

    assert _kinds(score.findings) == [
        ("commitments[0].amount", FailureKind.OMISSION),
        ("commitments[0].date", FailureKind.OMISSION),
        ("commitments[0].method", FailureKind.OMISSION),
        ("commitments[0].amount", FailureKind.FABRICATION),
        ("commitments[0].date", FailureKind.FABRICATION),
        ("commitments[0].method", FailureKind.FABRICATION),
    ]
    assert score.commitment_slots_correct == 0
    assert score.commitment_slots_scored == 6


def test_two_unparseable_amounts_that_are_the_same_words_still_pair() -> None:
    """``parse_brl`` returning ``None`` falls back to string equality, not to failure.

    "uns oitocentos" is a hedge rather than a figure, and the parser refuses it on
    purpose — resolving it to 800 would fabricate the precision the customer
    deliberately withheld. That refusal must not cost the pairing: two records
    holding the same unparseable words are still talking about the same promise,
    and the disagreement between them is the field that actually differs.
    """
    score = _score(
        expected=[_promise(amount="uns oitocentos", date="dia 20")],
        actual=[_promise(amount="uns oitocentos", date="dia 30")],
    )

    assert _kinds(score.findings) == [("commitments[0].date", FailureKind.WRONG_VALUE)]


def test_a_date_that_parses_as_money_is_still_compared_as_text() -> None:
    """Money-awareness is scoped to the amount field and does not leak.

    "20" and "20,00" both parse as currency, and a scorer that applied the
    monetary comparison field-by-field would call them equal. They are dates. A
    day of the month and a figure with cents are different facts that happen to
    share a lexical form, and the only thing the parser could contribute here is
    a false match.
    """
    score = _score(
        expected=[_promise(date="20")],
        actual=[_promise(date="20,00")],
    )

    assert _kinds(score.findings) == [("commitments[0].date", FailureKind.WRONG_VALUE)]


# ---------------------------------------------------------------------------
# Alignment — which kind a mismatch becomes
# ---------------------------------------------------------------------------


def test_a_commitment_absent_from_the_record_is_one_omission_per_stated_field() -> None:
    """A wholly missed promise is scored field by field, not as a single "wrong".

    That is what makes omission visible at the scale the literature says it
    occurs: one dropped promise with three stated values costs three omissions,
    not one.
    """
    score = _score(expected=[_promise()], actual=[])

    assert _kinds(score.findings) == [
        ("commitments[0].amount", FailureKind.OMISSION),
        ("commitments[0].date", FailureKind.OMISSION),
        ("commitments[0].method", FailureKind.OMISSION),
    ]
    assert score.commitment_slots_correct == 0
    assert score.commitment_slots_scored == 3


def test_a_promise_nobody_made_is_one_fabrication_per_recorded_field() -> None:
    """The failure the whole industry's headline metric is made of, scored.

    Promise-to-pay is what collections vendors report, and promise-to-pay is not
    money (BLUEPRINT §6). A record carrying an agreement the customer never
    reached is that critique at its worst: it inflates the reported number, it
    tells a specialist there is nothing to chase, and the customer finds out when
    the bank acts on a plan they never agreed to. "The model usually guesses
    right" is not a standard.

    Only the recorded fields are scored, so an invented promise with no named
    path costs two fabrications rather than three — the same rule as the
    expected side, applied in the other direction.
    """
    score = _score(
        expected=[],
        actual=[
            PaymentCommitment(
                amount="R$ 1.200,00",
                date="sexta-feira",
                source_utterance="acho que dá para resolver isso essa semana",
            )
        ],
    )

    assert _kinds(score.findings) == [
        ("commitments[0].amount", FailureKind.FABRICATION),
        ("commitments[0].date", FailureKind.FABRICATION),
    ]


def test_a_wrong_date_is_one_wrong_value_and_not_a_loss_plus_an_invention() -> None:
    """Entities are paired on the amount before their other fields are compared.

    Without that, every wrong date would be reported as one fabricated promise
    plus one omitted promise, and the taxonomy would say nothing useful about
    either. Order is not the key: the record here lists the two promises the
    other way round and they still pair on what they are worth.

    Note the exact scope of the claim, because it is the port's one asymmetry —
    this holds for the date and the method, and not for the amount, which has
    nothing left to pair on when it is the field that is wrong. See
    ``test_two_genuinely_different_amounts_cascade_into_an_omission_and_a_fabrication``.
    """
    score = _score(
        expected=[
            _promise(amount="mil e duzentos", date="dia primeiro"),
            PaymentCommitment(
                amount="quinhentos reais",
                date="dia trinta",
                method=PaymentPath.INSTALMENTS,
                source_utterance="e mais quinhentos reais no dia trinta",
            ),
        ],
        actual=[
            PaymentCommitment(
                amount="quinhentos reais",
                date="dia trinta",
                method=PaymentPath.INSTALMENTS,
                source_utterance="quinhentos reais",
            ),
            _promise(amount="mil e duzentos", date="dia treze"),
        ],
    )

    assert _kinds(score.findings) == [("commitments[0].date", FailureKind.WRONG_VALUE)]


def test_an_amount_promised_twice_consumes_recorded_entries_in_order() -> None:
    """The only sane reading of "the customer promised five hundred reais twice".

    A customer splitting a balance across two dates states the same figure twice,
    and the two rows differ in nothing the alignment key can see. Consuming
    recorded entries in order is what keeps them from collapsing into one pair
    plus one spurious row.
    """
    first = PaymentCommitment(
        amount="quinhentos reais",
        date="dia dez",
        method=PaymentPath.INSTALMENTS,
        source_utterance="quinhentos reais no dia dez",
    )
    second = PaymentCommitment(
        amount="quinhentos reais",
        date="dia vinte e cinco",
        method=PaymentPath.INSTALMENTS,
        source_utterance="e os outros quinhentos no dia vinte e cinco",
    )

    score = _score(expected=[first, second], actual=[first, second])

    assert score.findings == []
    assert score.commitment_slots_scored == 6


# ---------------------------------------------------------------------------
# Disputes
# ---------------------------------------------------------------------------


def test_a_dispute_is_scored_on_its_subject_and_its_detail() -> None:
    """ "O valor" and "o valor — eu já paguei" are different facts.

    The second names a reason a specialist can act on under FDCPA §809(b); the
    first names only that something is wrong. Recording the subject and dropping
    what the customer said about it is the collections form of writing down
    "penicillin" when they told you their throat closed.
    """
    score = _score(
        expected_disputes=[
            Dispute(
                subject="o valor",
                detail="já paguei esse boleto no mês passado",
                source_utterance=(
                    "Esse valor está errado. Eu já paguei esse boleto no mês passado."
                ),
            )
        ],
        actual_disputes=[
            Dispute(subject="o valor", source_utterance="Esse valor está errado.")
        ],
    )

    assert _kinds(score.findings) == [("disputes[0].detail", FailureKind.OMISSION)]


def test_a_reversed_denial_shows_up_as_an_omission() -> None:
    """ "Não, está tudo certo ... espera, eu já paguei!" — one deletion, one inversion.

    Taking the first half of that turn and recording no dispute sits on the
    zero-tolerance list beside the amount errors (BLUEPRINT §5). The golden set's
    ``elderly_slow_speech`` customer reverses her own denial one sentence after
    making it, and fragmented speech is exactly where an agent that stops
    listening at the first full stop does its damage — the customers most likely
    to be misheard are the ones the duty exists to protect.
    """
    already_paid = Dispute(
        subject="já paguei",
        detail="paguei no caixa eletrônico, tenho o comprovante aqui",
        source_utterance=(
            "Não, está tudo certo... espera, não! Eu já paguei isso. Paguei no "
            "caixa eletrônico, tenho o comprovante aqui."
        ),
    )

    score = _score(expected_disputes=[already_paid], actual_disputes=[])

    assert _kinds(score.findings) == [
        ("disputes[0].subject", FailureKind.OMISSION),
        ("disputes[0].detail", FailureKind.OMISSION),
    ]


def test_a_dispute_error_is_never_averaged_into_the_commitment_accuracy_number() -> (
    None
):
    """A missed or wrong dispute is a failure in its own right, on its own row.

    Folding it into ``commitment_entity_accuracy`` would be the same mistake as
    averaging a wrong drug into a word error rate: the aggregate absorbs the
    error and the scorecard reads fine. It would also let a run buy back a lost
    dispute with three correct payment fields, which is precisely the trade the
    metric must not offer — a dispute is what an FDCPA §809(b) response depends
    on, and it is what the specialist opens the record to find.
    """
    score = _score(
        expected=[_promise()],
        actual=[_promise()],
        expected_disputes=[
            Dispute(subject="o valor", source_utterance="esse valor está errado")
        ],
        actual_disputes=[
            Dispute(subject="a data", source_utterance="essa data está errada")
        ],
    )

    assert score.commitment_slots_correct == score.commitment_slots_scored == 3
    assert {finding.kind for finding in score.findings} == {
        FailureKind.OMISSION,
        FailureKind.FABRICATION,
    }
