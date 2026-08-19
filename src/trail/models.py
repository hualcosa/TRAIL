"""Pydantic v2 models for the Banco Aurora early-stage collections agent.

This module is the single source of truth for every value that crosses a
boundary in this system: HTTP request/response bodies, LLM structured-output
targets, Postgres `jsonb` payloads, and the eval report. Nothing else in the
package defines its own shape for these concepts.

Four conventions hold throughout:

* **Identifiers.** Machine-generated identifiers (`call_id`, `turn_id`,
  `trace_id`, `run_id`) are :class:`~uuid.UUID`. Identifiers that come from
  outside the system (`account_id`, `case_id`, `reviewed_by`) are ``str``.
* **Timestamps.** Every ``datetime`` is timezone-aware UTC. Postgres columns
  are ``timestamptz``.
* **Money has two types, and they are never interchangeable.** Money the
  system of record owns — :attr:`AccountProfile.balance_brl` — is
  :class:`~decimal.Decimal`: it is rendered into approved text and spoken
  aloud to the customer, and BLUEPRINT §5 makes a wrong balance spoken aloud a
  zero-tolerance failure. Binary floating point has no business anywhere near
  a figure someone is being asked to pay. Money the *customer* said —
  :attr:`PaymentCommitment.amount` — is ``str | None``, carried exactly as
  spoken and never parsed at capture time. ``cost_usd`` is ``float``: model
  spend in fractions of a cent, an analytics figure and not a ledger.
* **Strictness.** Every model forbids unknown fields. This makes the HTTP
  contract strict (FastAPI answers 422 on an unexpected key) and, more
  importantly, makes the generated JSON Schema emit
  ``additionalProperties: false``, which strict structured outputs
  requires of :class:`TurnExtraction` and its nested models.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Base model for the package: unknown fields are a hard error.

    Required for the LLM structured-output path — Pydantic only emits
    ``additionalProperties: false`` when ``extra`` is ``"forbid"``.
    """

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class Step(StrEnum):
    """The conversation steps, in order (BLUEPRINT §3).

    Declaration order **is** the conversation order; :func:`next_step` relies
    on it. Do not reorder members.

    Two steps carry legal weight before they carry any product weight.
    :attr:`VERIFY_RIGHT_PARTY` is a hard gate: revealing a debt to anyone but
    the debtor is a direct FDCPA third-party disclosure and BLUEPRINT §5's
    first zero-tolerance failure, so nothing after it is spoken until the
    identity compare passes. :attr:`DISCLOSE_AND_CONSENT` carries the
    mini-Miranda, the AI disclosure and the recording notice, and it lands
    before any negotiation rather than after it.

    :attr:`STATE_BALANCE` is the only customer-specific text in the whole
    approved script. It is a *slotted* block: the balance, product, due date
    and days past due are substituted deterministically from
    :class:`AccountProfile` — from the system of record, never from the model
    — and the compliance allowlist verifies the *rendered* form. See
    ``trail.protocol.Protocol.render``.
    """

    VERIFY_RIGHT_PARTY = "verify_right_party"
    DISCLOSE_AND_CONSENT = "disclose_and_consent"
    STATE_BALANCE = "state_balance"
    CONFIRM_TERMS = "confirm_terms"
    OFFER_PAYMENT_PATH = "offer_payment_path"
    CAPTURE_COMMITMENT = "capture_commitment"
    CONFIRM_CONTACT = "confirm_contact"
    POST_OUTCOME = "post_outcome"


def next_step(step: Step) -> Step | None:
    """Return the step following ``step``, or ``None`` if it is the last one.

    The state machine may still short-circuit to a terminal state at any point
    (wrong party, transfer to a human); this only describes the happy path.
    """
    order = tuple(Step)
    index = order.index(step) + 1
    return order[index] if index < len(order) else None


class TerminalState(StrEnum):
    """How a call ended (BLUEPRINT §6).

    All five are first-class. ``NOT_REACHED`` in particular is a recorded
    outcome, never an absence of data. v0 is inbound — the customer taps a
    notification and calls in — so non-answer is rare today and central at
    post 8, when outbound arrives with its ~28% connection rate (BLUEPRINT §4,
    Razorpay's disclosed benchmark, vendor-graded and named as such). Keeping
    the state first-class now is what stops the denominator quietly changing
    shape when it starts to matter.

    Dropping unreached accounts from the denominator is the same self-flattery
    as reporting promise-to-pay and calling it money: both delete the part of
    the population the agent could not move, and both make a funnel look like
    an outcome.
    """

    COMPLETED_NO_CALLBACK = "completed_no_callback"
    COMPLETED_NEEDS_CALLBACK = "completed_needs_callback"
    TRANSFERRED_TO_HUMAN = "transferred_to_human"
    NOT_RIGHT_PARTY = "not_right_party"
    NOT_REACHED = "not_reached"


class FailureKind(StrEnum):
    """Extraction failure taxonomy (BLUEPRINT §6).

    Scored separately and never collapsed into a single pass/fail number:

    * ``OMISSION`` — a fact present in the utterance and absent from the record.
    * ``FABRICATION`` — a value in the record absent from the utterance.
    * ``WRONG_VALUE`` — present in both, and different.

    The ASR literature is explicit that omission dominates, which is why
    BLUEPRINT §6 asks for entity error rate on amounts, dates and account
    numbers rather than average WER: a scorecard that only reports "wrong" has
    nothing to say about the most common failure mode, and an amount that was
    never captured is not a smaller error than an amount captured wrongly.
    """

    OMISSION = "omission"
    FABRICATION = "fabrication"
    WRONG_VALUE = "wrong_value"


class EvalRunStatus(StrEnum):
    """Lifecycle of an eval run. ``POST /runs`` returns while still RUNNING."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class PaymentPath(StrEnum):
    """The four approved ways to settle, and the only four.

    Described identically to every customer, including the instalment plan,
    which is a **published, universal** option rather than a negotiated one.
    The enum is closed on purpose: a settlement, a waiver, a discount or a
    bespoke plan has no representation in this system, so "the agent granted
    something it had no authority to grant" (BLUEPRINT §5) cannot be recorded
    even if it were somehow said. ``trail.agent.compliance`` catches the saying;
    this closed set catches the recording.
    """

    PAY_NOW = "pay_now"
    PAYMENT_LINK = "payment_link"
    SCHEDULE = "schedule"
    INSTALMENTS = "instalments"


class Product(StrEnum):
    """The two products in Banco Aurora's book (BLUEPRINT §4).

    Spoken aloud through a rendered slot in :attr:`Step.STATE_BALANCE`, so the
    set is closed for the same reason the approved text is verbatim: the
    customer hears one of exactly two phrases, and both were reviewed.
    """

    PERSONAL_LOAN = "personal_loan"
    CREDIT_CARD = "credit_card"


# --------------------------------------------------------------------------
# Domain models
# --------------------------------------------------------------------------


class PaymentCommitment(StrictModel):
    """One promise-to-pay as the customer stated it. Not normalised.

    ``amount`` and ``date`` are strings, and they stay strings. "mil e
    duzentos" is not turned into ``1200``, "sexta-feira" is not resolved into
    a date, and an amount with no currency is not assumed to be reais. The
    agent captures; resolution is inference, and inference is where the error
    class is manufactured — an amount misread by one order of magnitude is a
    wrong figure in a customer's payment plan, and a relative date resolved
    against the wrong week is a broken promise the customer never made.
    ``trail.money.parse_brl`` exists and is used **only by the eval scorer**,
    never by the agent.

    ``source_utterance`` is mandatory and carries the verbatim customer words
    this record was extracted from. It is what makes a trace auditable: a
    reviewer can see the claim and its evidence side by side, and the eval
    harness can classify a mismatch as omission, fabrication, or wrong value
    rather than just "wrong".
    """

    amount: str | None = Field(
        default=None,
        description=(
            "The amount as stated, e.g. '847,32' or 'oitocentos e quarenta e "
            "sete'. Copied, never normalised and never converted."
        ),
    )
    date: str | None = Field(
        default=None,
        description=(
            "The date as stated, e.g. 'dia 20' or 'sexta-feira'. Copied, never "
            "resolved into a calendar date."
        ),
    )
    method: PaymentPath | None = Field(
        default=None,
        description="The approved path the customer chose, if they named one.",
    )
    source_utterance: str = Field(
        description="Verbatim customer words this commitment was extracted from."
    )


class Dispute(StrictModel):
    """Something the customer said about the debt that this agent cannot resolve.

    Recorded verbatim, never characterised. "já paguei", "esse valor está
    errado" and "eu nunca peguei esse empréstimo" are three different facts for
    the specialist, and the agent is not permitted to assess which — nor
    whether any of them is true. ``subject`` is the customer's own words for
    what they are disputing, not a category this system chose for them.

    A ``Dispute`` row triggers **no callback of its own** and no special
    routing: sorting the queue by the merit of a dispute would be the agent
    assessing that merit. It reaches the same specialist queue as every other
    record. Separately, an explicit dispute is also a thing the approved script
    cannot answer, so it sets :attr:`TurnExtraction.needs_human` and transfers
    — FDCPA §809(b) cease-collection-on-dispute is the specialist's action to
    take, on their own reading of these words, not this system's.

    ``source_utterance`` is mandatory, for the same reason it is mandatory on
    :class:`PaymentCommitment`.
    """

    subject: str = Field(
        description="What they are disputing, as they said it: 'o valor', 'já paguei'."
    )
    detail: str | None = Field(
        default=None,
        description="What they said about it, as stated. Never interpreted or assessed.",
    )
    source_utterance: str = Field(
        description="Verbatim customer words this dispute was extracted from."
    )


class AccountProfile(StrictModel):
    """The delinquent account as the system of record holds it.

    Entirely synthetic — no real customer data in this repo, and every ``tax_id``
    is an invented CPF that happens to pass its own check digits (see
    ``trail.money.is_valid_cpf``).

    This model is the **source** of every customer-specific word the agent
    speaks. ``product``, ``balance_brl``, ``due_date`` and ``days_past_due``
    are formatted by ``trail.money`` and substituted into the
    :attr:`Step.STATE_BALANCE` block deterministically. No value here ever
    round-trips through the language model on its way to the customer's ear.

    ``days_past_due`` is bounded at 1..30 by the type, not by a comment. The
    segment *is* the scope (BLUEPRINT §3): 1–30 days past due is people who
    mostly forgot or had a bad month, which is the strongest automation case
    and the cleanest ethical footing. BLUEPRINT §7 is explicit that AI
    underperforms humans on later buckets and larger balances. An account
    outside the window is not a slightly harder call for this agent; it is a
    call this agent should not be placing, and a validation error is the
    cheapest possible place to find that out.
    """

    account_id: str
    full_name: str
    tax_id: str = Field(
        description="CPF, 11 digits, digits only, no punctuation. Synthetic."
    )
    date_of_birth: date = Field(
        description=(
            "The fallback second identifier, used when a customer will not say "
            "a CPF over the phone."
        )
    )
    phone: str
    product: Product
    balance_brl: Decimal = Field(
        description=(
            "Amount past due, from the system of record. Decimal, never float, "
            "and never a value the model produced or repeated back."
        )
    )
    due_date: date = Field(description="The date the payment was due.")
    days_past_due: int = Field(
        ge=1,
        le=30,
        description="1..30. The segment is the scope — see the class docstring.",
    )


class TurnExtraction(StrictModel):
    """Structured output target for one customer turn.

    Every field beyond ``step`` and ``raw_utterance`` is optional *by step*:
    ``identity_confirmed`` is only meaningful during
    :attr:`Step.VERIFY_RIGHT_PARTY`, ``selected_path`` only during
    :attr:`Step.OFFER_PAYMENT_PATH`, and so on. The model fills what the turn
    actually contains and leaves the rest at its default.

    This is a **capture** model, not an interpretation model. It records what
    the customer said. It does not decide whether the debt is owed, how likely
    they are to pay, how much support they need, or what should happen next.

    Three fields are worth reading twice, because each is a place where a
    capture model could quietly become a classifier.

    ``identity_confirmed`` is the model's *opinion* and is not the gate.
    ``stated_name``, ``stated_tax_id`` and ``stated_date_of_birth`` carry the
    identifiers the person actually gave, and
    :func:`trail.agent.machine.identity_matches` compares them to the booked
    account deterministically, CPF check digits included. Third-party
    disclosure is a direct FDCPA violation and BLUEPRINT §5's first
    zero-tolerance failure, and the caller's own words are interpolated into
    the extraction prompt — so the gate cannot rest on a boolean the utterance
    itself can argue with. Capture, then compare.

    ``restated_amount`` and ``restated_date`` are *captured*, not judged. The
    verdict on whether the customer restated the terms correctly is a separate
    model call against the rendered approved text
    (``trail.agent.llm.judge_terms_restatement``), so the transcript of what
    they said survives independently of the judgement made about it.

    ``unresolved`` records that the turn left something outstanding, never what
    or how much it matters. It is what makes the callback decision a
    completeness check rather than a judgement about the customer: the machine
    reads the bit, never the reason, and every record reaches the same
    specialist queue regardless.

    ------------------------------------------------------------------
    THERE IS DELIBERATELY NO HARDSHIP, VULNERABILITY, SENTIMENT,
    DISTRESS OR REASON_FOR_TRANSFER FIELD, AND THERE MUST NEVER BE ONE.
    ------------------------------------------------------------------
    This is the single most important paragraph in this file, because two
    obligations pull in opposite directions here and the resolution is the
    whole design.

    BLUEPRINT §7 refuses to automate hardship negotiation. BLUEPRINT §5 makes
    "missed hardship or vulnerability cue" a zero-tolerance failure under FCA
    Consumer Duty and CONC. Read naively, the second asks for a hardship
    detector — a classifier that reads a debtor's words and decides how
    vulnerable they are — and the first forbids acting on whatever it produces.

    **The duty is to route to a human, not to classify.** A hardship cue
    reaches the graph as ``needs_human``: a bit with no reason attached, which
    produces exactly the same transfer, in exactly the same words, as a refused
    consent, a request for a person, or any question the approved script cannot
    answer. The agent never records *that* it was hardship, never assigns a
    severity, and never negotiates.

    The moment that bit carries a *why*, this model is classifying a vulnerable
    person in a debt-collection context, inside a system that also decides what
    to say to them next. The classification would be made by a language model
    from one phone transcript, unreviewed, unvalidated and consequential. That
    is not a capture model with one extra field on it; it is a different
    product, answering to a different regulator, and the specialist who reads
    every record is already the thing the duty asks for.

    Whether a cue was *missed* is therefore measured by golden-set expectation
    and not by a record field: case ``hardship_disclosed`` pins its
    ``expected_terminal_state`` at ``TRANSFERRED_TO_HUMAN`` and fails if the
    call completed. The measurement lives in the eval harness, where a human
    fixed the answer in advance, rather than in a field a model fills at
    runtime.

    The mirror case is ``mentions_difficulty_in_passing``: a customer who
    mentions a tight month mid-sentence and then commits normally. Correct
    behaviour there is aggressively unremarkable — record the words verbatim,
    continue, change nothing about routing. A transfer on that case is this
    model having classified.
    """

    step: Step = Field(description="The step this utterance was spoken during.")
    raw_utterance: str = Field(description="The customer's verbatim words.")
    understood: bool = Field(
        default=False,
        description="True if the utterance was intelligible and on-topic.",
    )
    needs_human: bool = Field(
        default=False,
        description=(
            "True if the customer asked for a person, refused or withdrew "
            "consent, said they are not the right party, was unintelligible, "
            "or said anything the approved script cannot answer — which is "
            "where hardship, vulnerability and explicit disputes land. This is "
            "a routing signal only: it never carries a reason, a severity or a "
            "category, and it is never set from an inference about how the "
            "customer sounds."
        ),
    )
    unresolved: bool = Field(
        default=False,
        description=(
            "True if this turn left something the record cannot carry whole: a "
            "question the approved text does not answer, an amount or a date "
            "the customer could not give, or a question left unanswered. A "
            "completeness bit with no reason attached — it never records what "
            "was outstanding, only that something was."
        ),
    )
    commitments: list[PaymentCommitment] = Field(default_factory=list)
    disputes: list[Dispute] = Field(default_factory=list)
    identity_confirmed: bool | None = Field(
        default=None, description="Only set during verify_right_party."
    )
    stated_name: str | None = Field(
        default=None,
        description=(
            "The name the person on the line gave, copied as they said it. "
            "Only set during verify_right_party."
        ),
    )
    stated_tax_id: str | None = Field(
        default=None,
        description=(
            "The CPF the person on the line gave, digits as spoken. Punctuation "
            "and spacing are left exactly as heard; the machine strips "
            "non-digits before comparing. Only set during verify_right_party."
        ),
    )
    stated_date_of_birth: str | None = Field(
        default=None,
        description=(
            "The date of birth the person on the line gave, written as "
            "YYYY-MM-DD. Null if they gave none or it was not a date. Only set "
            "during verify_right_party."
        ),
    )
    consent_given: bool | None = Field(
        default=None, description="Only set during disclose_and_consent."
    )
    restated_amount: str | None = Field(
        default=None,
        description=(
            "The amount as the customer restated it, verbatim. Captured, not "
            "judged. Only set during confirm_terms."
        ),
    )
    restated_date: str | None = Field(
        default=None,
        description=(
            "The date as the customer restated it, verbatim. Captured, not "
            "judged. Only set during confirm_terms."
        ),
    )
    selected_path: PaymentPath | None = Field(
        default=None,
        description=(
            "The approved path the customer chose. Null if they chose none of "
            "them. Only set during offer_payment_path."
        ),
    )
    contact_channel_confirmed: bool | None = Field(
        default=None, description="Only set during confirm_contact."
    )
    notes: str | None = Field(
        default=None,
        description="Verbatim overflow the other fields do not cover. Never a summary or an assessment.",
    )


class CallRecord(StrictModel):
    """The completed record for one call — the mocked system of record.

    ``reviewed_by`` and ``reviewed_at`` start ``None`` so nothing finalises
    itself. Every AI output requires human verification, and the human is a
    Banco Aurora **collections specialist** — never "agent", which is ambiguous
    with the thing that made the call.

    -------------------------------------------------------------------
    THIS MODEL HAS NO PRIORITY, URGENCY, SEVERITY, RISK_SCORE, TRIAGE,
    HARDSHIP, VULNERABILITY, PROPENSITY, SEGMENT OR SCORE FIELD, AND
    MUST NEVER GAIN ONE.
    -------------------------------------------------------------------
    Sorting the specialist queue by how likely a customer looks to pay is the
    collections form of the red-flag detector: an inferred, customer-specific
    classification, produced by a language model from a single phone
    transcript, that decides who gets human attention and in what order. It is
    "concerning cases first" with the money pointing the other way.

    Omitting the words "score" and "propensity" does not change what the field
    is: "engaged", "collectable", "bucket" and "strategy" are the same
    classification wearing a softer name. Nor does the tempting deterministic
    version — "order by balance, that is not a model output at all" — which is
    worse rather than better, because it is the same disparate treatment with
    none of the deniability. Under FDCPA and UDAAP the exposure is the unequal
    treatment such an ordering produces; under FCA Consumer Duty and CONC the
    customers it would sort to the bottom — smallest balances, least
    articulate, most distressed, and per BLUEPRINT §6's fairness
    stratification, most likely to be misheard by the ASR in the first place —
    are precisely the ones the duty exists to protect.

    Every record goes to the same specialist queue, in the same order, with no
    filtering, ordering or prioritisation of any kind. The specialist makes
    every judgement about the customer, including whether a dispute has merit
    and whether hardship support is owed.

    If you are here to add a field so the queue can be worked "best accounts
    first" — that is the exact change this comment exists to prevent. Sort by
    ``started_at``.
    """

    call_id: UUID = Field(default_factory=uuid4)
    account_id: str
    started_at: datetime
    ended_at: datetime
    terminal_state: TerminalState

    commitments: list[PaymentCommitment] = Field(default_factory=list)
    disputes: list[Dispute] = Field(default_factory=list)
    selected_path: PaymentPath | None = None
    contact_channel_confirmed: bool | None = None
    consent_given: bool | None = None
    terms_confirmed: bool | None = None

    protocol_version: str
    prompt_version: str
    model: str

    needs_specialist_review: Literal[True] = Field(
        default=True,
        description=(
            "Always True. Typed as Literal[True] so the type checker, the JSON "
            "Schema, and Pydantic validation all reject any attempt to finalise "
            "a record without specialist review. The database pins it a third "
            "time with a CHECK constraint, because a manual UPDATE never goes "
            "through this model."
        ),
    )
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    cost_usd: float = 0.0
    wall_seconds: float = 0.0


# --------------------------------------------------------------------------
# Trace models — semantic records, distinct from OTel spans
# --------------------------------------------------------------------------


class LLMCallTrace(StrictModel):
    """One model API call: what was asked, what came back, what it cost.

    Written for every model call, including failed ones. ``prompt_version`` and
    ``model`` are stamped on the row rather than joined from config, so a trace
    stays interpretable after either changes.
    """

    trace_id: UUID = Field(default_factory=uuid4)
    call_id: UUID
    step: Step
    prompt_version: str
    model: str
    request_json: dict[str, Any] = Field(
        description="The request body as sent, minus credentials."
    )
    response_json: dict[str, Any] = Field(
        description="The parsed response, or an error payload if the call failed."
    )
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = Field(
        default=0,
        description=(
            "From response.usage.cache_read_input_tokens. Recorded on every "
            "call so the economics post can separate cached from uncached spend."
        ),
    )
    cache_creation_input_tokens: int = Field(
        default=0,
        description=(
            "From response.usage.cache_creation_input_tokens — the tokens "
            "written into the cache, billed at 1.25x the input rate. A separate "
            "field from input_tokens, which is the uncached remainder only, so "
            "the total prompt is the sum of all three."
        ),
    )
    cost_usd: float = 0.0
    latency_ms: int = 0
    created_at: datetime


class TurnTrace(StrictModel):
    """One conversational turn: what the agent said, what the customer said back.

    ``customer_utterance`` is the empty string on the opening turn, where the
    agent speaks first and ``extraction`` is ``None``.
    """

    turn_id: UUID = Field(default_factory=uuid4)
    call_id: UUID
    step: Step
    agent_utterance: str
    customer_utterance: str = ""
    extraction: TurnExtraction | None = None
    latency_ms: int = 0
    created_at: datetime


# --------------------------------------------------------------------------
# Eval models
# --------------------------------------------------------------------------


class CaseExpectation(StrictModel):
    """What a synthetic case is expected to produce, fixed before the run."""

    expected_terminal_state: TerminalState
    expected_commitments: list[PaymentCommitment] = Field(default_factory=list)
    expected_disputes: list[Dispute] = Field(default_factory=list)
    expected_terms_confirmed: bool | None = None
    expected_contact_channel: bool | None = None
    must_not_contain: list[str] = Field(
        default_factory=list,
        description=(
            "Phrases whose presence anywhere in the agent's output is a "
            "compliance violation — a discount, settlement, waiver or "
            "instalment plan the agent has no authority to grant, pressure or "
            "threat language, or anything about the debt said to an unverified "
            "party (BLUEPRINT §5). Matched case-insensitively as substrings."
        ),
    )


class SyntheticCase(StrictModel):
    """One golden-set customer: profile, scripted turns, expected outcome.

    Customer turns are **scripted, not LLM-generated**. Deterministic,
    reproducible, free, and it eliminates simulator collusion — an LLM customer
    and an LLM agent share biases and conspire toward success. BLUEPRINT §6
    puts it more bluntly: LLM-generated callers are systematically too easy,
    too cooperative, too articulate and too on-topic, so the hard cases have to
    be written by hand or the numbers look great and mean nothing.
    """

    case_id: str
    description: str
    profile: AccountProfile
    scripted_turns: list[str] = Field(
        default_factory=list,
        description="Customer utterances, consumed in order, one per agent turn.",
    )
    reachable: bool = Field(
        default=True,
        description="False models a customer the agent never connects to.",
    )
    answering_party: Literal["customer", "other", "none"] = "customer"
    expectation: CaseExpectation


class Finding(StrictModel):
    """One scored discrepancy between an expectation and an actual record."""

    case_id: str
    field: str = Field(
        description="Dotted path of the compared field, e.g. 'commitments[0].amount'."
    )
    kind: FailureKind
    expected: str | None = None
    actual: str | None = None
    detail: str = ""


class MetricSet(StrictModel):
    """Metrics for one eval run, computed against a fixed golden set.

    ``fully_automated_rate`` is the primary *execution* metric and is defined
    as::

        count(terminal_state == COMPLETED_NO_CALLBACK) / SCHEDULED ACCOUNTS

    The denominator is **scheduled accounts, not connected calls, and not
    answered calls**. Computing it over answered calls is the trap BLUEPRINT §6
    names directly: vendors report promise-to-pay, and promise-to-pay is not
    money. The one named public funnel in this industry — SET Financial's
    12,800 attempts to 1,360 live conversations to 151 payment links — is
    vendor-reported and stops at *links*, not at cash received, and its
    headline 11.8% live-to-link rate reads very differently once the 12,800
    attempts are put back underneath it. The MVP models non-answer on purpose
    so the honest number looks bad on the first run.

    ``promise_capture_rate`` shares that denominator deliberately, and the two
    rates ask different questions: automation asks "did the call finish clean",
    capture asks "did we actually get a promise". Reporting both, over the same
    denominator, is the direct answer to the critique above — you can see the
    funnel step and the outcome side by side instead of one standing in for the
    other. Neither is cash received within 30 days; that is the north star, it
    is longitudinal, and it belongs to the outcomes layer rather than to this
    in-call scorecard.

    ``cost_per_fully_automated_call_usd`` uses the same denominator logic:
    total spend across *all* attempted calls divided by the count of fully
    automated completions. Calls needing a callback consume specialist time
    *plus* AI cost, so they belong in the numerator and not the denominator.

    **Two metrics are nullable, and that is the point.** A rate with an empty
    denominator is undefined, not perfect and not free. Reporting ``1.0`` for
    commitment accuracy on a run that scored no commitment field, or ``0.00``
    for cost on a run that automated nothing, makes an infrastructure failure
    read as two passing bars — the same failure ``runner._preflight`` exists to
    prevent, one layer down. ``None`` is therefore the honest value, it is what
    the report prints as ``n/a``, and ``check_thresholds`` refuses to score it
    either way.
    """

    run_id: UUID
    golden_set_version: str
    scheduled_accounts: int = Field(
        description="Denominator for fully_automated_rate. Every case in the golden set."
    )
    reached: int = Field(description="Calls where a party answered. Reporting only.")
    terminal_state_counts: dict[TerminalState, int] = Field(default_factory=dict)

    fully_automated_rate: float = Field(
        description="count(COMPLETED_NO_CALLBACK) / scheduled_accounts. See class docstring."
    )
    promise_capture_rate: float = Field(
        description=(
            "Calls that produced at least one commitment carrying both an "
            "amount and a date, over scheduled_accounts. Same denominator as "
            "fully_automated_rate, different question — see class docstring."
        )
    )
    commitment_entity_accuracy: float | None = Field(
        description=(
            "Exact-match accuracy over commitment amount, date and method. "
            "None when no field was scored — see class docstring."
        )
    )
    commitment_slots_scored: int = Field(
        default=0,
        description=(
            "The denominator behind commitment_entity_accuracy: (entity, field) "
            "positions where either side carried a value. Carried on the report "
            "so an accuracy figure can never be read without its sample size."
        ),
    )
    terms_confirmation_rate: float
    false_terms_confirmations: int = Field(
        default=0,
        description=(
            "Cases whose expectation pinned terms confirmation false and whose "
            "record says true. Zero-tolerance: this is the one way to raise "
            "terms_confirmation_rate by accepting a wrong amount or a wrong "
            "date as correct."
        ),
    )
    compliance_violations: int = Field(
        description="Count of must_not_contain hits in agent output. Target is zero."
    )
    findings_by_kind: dict[FailureKind, int] = Field(
        default_factory=dict,
        description=(
            "Breakdown of the *extraction* findings only (BLUEPRINT §6). "
            "Compliance violations are a spoken phrase rather than a record "
            "value and are counted by compliance_violations alone."
        ),
    )
    cost_per_fully_automated_call_usd: float | None

    p50_turn_latency_ms: float
    p95_turn_latency_ms: float

    prompt_version: str
    model: str
    created_at: datetime


class EvalRun(StrictModel):
    """An eval run: status while in flight, metrics and findings once finished."""

    run_id: UUID = Field(default_factory=uuid4)
    started_at: datetime
    finished_at: datetime | None = None
    status: EvalRunStatus = EvalRunStatus.RUNNING
    metrics: MetricSet | None = None
    findings: list[Finding] = Field(default_factory=list)
    regression_vs: UUID | None = Field(
        default=None, description="The run this one was compared against, if any."
    )
    regressions: list[str] = Field(
        default_factory=list,
        description="Human-readable regression statements, one per regressed metric.",
    )


# --------------------------------------------------------------------------
# HTTP wire models — agent service
# --------------------------------------------------------------------------


class StartCallRequest(StrictModel):
    """Body of ``POST /calls``."""

    profile: AccountProfile
    case_id: str | None = Field(
        default=None,
        description="Golden-set case this call belongs to, when driven by the eval harness.",
    )


class StartCallResponse(StrictModel):
    """Response of ``POST /calls``. ``terminal_state`` is always ``None`` here."""

    call_id: UUID
    step: Step
    agent_utterance: str
    finished: bool = False
    terminal_state: TerminalState | None = None

    trace_id: str | None = Field(
        default=None,
        description=(
            "Observability metadata: the OpenTelemetry trace this request "
            "produced, 32 lowercase hex digits. Null when tracing is disabled — "
            "a local run without a collector is a supported mode, not an error."
        ),
    )
    trace_url: str | None = Field(
        default=None,
        description=(
            "Observability metadata: an absolute link to that trace in the "
            "Langfuse UI, built from TRAIL_LANGFUSE_UI_BASE_URL and "
            "TRAIL_LANGFUSE_PROJECT_ID. Absolute because the browser reaches "
            "Langfuse directly rather than through this service. Null "
            "whenever trace_id is."
        ),
    )


class TurnRequest(StrictModel):
    """Body of ``POST /calls/{call_id}/turns``.

    ``call_id`` duplicates the path parameter; the two must match or the agent
    answers 400.
    """

    call_id: UUID
    customer_utterance: str


class TurnResponse(StrictModel):
    """Response of ``POST /calls/{call_id}/turns``.

    ``record`` is populated exactly when ``finished`` is true.

    ``trace_id`` and ``trace_url`` are observability metadata and default to
    ``None``, so every client that parsed this model before they existed still
    parses it — including the eval harness, which reads none of the three.
    """

    call_id: UUID
    step: Step
    agent_utterance: str
    finished: bool
    terminal_state: TerminalState | None = None
    record: CallRecord | None = None

    trace_id: str | None = Field(
        default=None,
        description=(
            "Observability metadata: the OpenTelemetry trace this turn "
            "produced, 32 lowercase hex digits. Null when tracing is disabled."
        ),
    )
    trace_url: str | None = Field(
        default=None,
        description=(
            "Observability metadata: an absolute link to that trace in the "
            "Langfuse UI, built from TRAIL_LANGFUSE_UI_BASE_URL and "
            "TRAIL_LANGFUSE_PROJECT_ID. Null whenever trace_id is."
        ),
    )


class MarkUnreachableRequest(StrictModel):
    """Body of ``POST /calls/{call_id}/unreachable``.

    Ends the call with :attr:`TerminalState.NOT_REACHED`. ``reason`` is
    operational (no answer, disconnected number, voicemail) and describes the
    connection, never the customer and never the debt.
    """

    call_id: UUID
    reason: str


# --------------------------------------------------------------------------
# HTTP wire models — evals service
# --------------------------------------------------------------------------


class StartEvalRequest(StrictModel):
    """Body of ``POST /runs``. Both fields default to the service's own defaults."""

    golden_set_version: str | None = None
    compare_to: UUID | None = Field(
        default=None,
        description="Run to detect regressions against. Defaults to the latest completed run.",
    )


class StartEvalResponse(StrictModel):
    """Response of ``POST /runs``. The run itself proceeds in the background."""

    run_id: UUID
