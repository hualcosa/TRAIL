"""The conversation graph: a LangGraph ``StateGraph`` over :class:`~trail.models.Step`.

Nothing in this module calls a model, touches a database, or opens a socket. A
node is handed one :class:`~trail.models.TurnExtraction` and decides what the
agent says next and where the call goes. That is what makes every branch
unit-testable with no API key and no compose stack, and it is why "can this
agent grant a customer a discount?" can be answered by reading this file and
``compliance.py``.

**The agent's utterance is always approved text.** Seven of the eight blocks are
:meth:`trail.protocol.Protocol.text_for`, read verbatim. The eighth —
``state_balance`` — is :meth:`trail.protocol.Protocol.render`, the same
approved template with four customer-specific values substituted by literal
string replacement from :func:`slots_for_call`. Those four values come out of
the deterministic formatters in :mod:`trail.money`, reading
:class:`~trail.models.AccountProfile` fields that came out of the system of
record. **No slot value ever originates from a model**, and no utterance in this
file is composed word by word.

The only text composed here at all is three administrative constants — a
transfer hand-off, a wrong-party close and an identity reprompt — which carry no
approved collections content of any kind: no amount, no product, no due date, no
customer, no instruction, no term. They are proved to carry none by the
compliance assertions rather than asserted to: the offline suite runs
``assert_no_unauthorised_concession`` and
``assert_no_unverified_party_disclosure`` over all three. If any of them ever
needed to say something about a debt, it would have to move into
``protocol/collections_1_30_dpd.md`` and be reviewed as approved content.

**Shape of the graph.** One node per step the agent listens at, plus four exit
nodes for the five terminal states — ``post_outcome`` is where both
``completed_no_callback`` and ``completed_needs_callback`` leave, since the
difference between them is a flag on the record rather than a different ending.
A listening node says its approved text, ``interrupt()``\\ s
until the service hands back the customer's reply, applies its rule, and returns
a :class:`~langgraph.types.Command` naming the next node. The service never
tracks where a call is: the checkpointer does, keyed by ``call_id``.

Terminal states (BLUEPRINT §6) are the graph's exits, and what reaches each one:

``not_right_party``
    Identity was not confirmed. A hard gate, not a step that degrades into a
    partial conversation, because disclosing a debt to anyone but the debtor is
    a direct FDCPA third-party disclosure and the first entry on BLUEPRINT §5's
    zero-tolerance list. Nothing about the debt has been said at this point: the
    approved ``verify_right_party`` text names only "uma pendência na sua conta
    conosco", which every caller hears precisely because it names no amount, no
    product and no due date. The gate itself is deterministic — see
    :func:`identity_matches`.
``transferred_to_human``
    Consent was refused, the extraction set ``needs_human``, or an outbound
    utterance failed a compliance assertion. The agent transfers *without
    classifying what it heard* — ``needs_human`` is a routing bit with no reason
    attached, and hardship, vulnerability and explicit disputes all arrive
    through it and all leave through this exit, in the same words (BLUEPRINT §7).
``completed_needs_callback``
    The call ran to the end and left something outstanding that a specialist has
    to phone about: terms unconfirmed after a second attempt, no payment path
    chosen, a commitment row missing its amount or its date, a contact channel
    not explicitly confirmed, or a turn the record could not carry whole. All
    five are completeness tests, and not one of them reads what the customer
    said — not the size of the amount, not the merit of a dispute, not how the
    customer sounded.
``completed_no_callback``
    The call ran to the end with nothing outstanding. This is the numerator of
    the primary execution metric, measured against the low-30s cold-launch
    containment floor rather than against a tuned vendor figure.
``not_reached``
    Nobody was reached. Marked explicitly by the caller, never inferred, and a
    first-class outcome rather than an absence of data — v0 is inbound so it is
    rare today, and outbound arrives at a ~28% connection rate (BLUEPRINT §4).
    Dropping unreached accounts from the denominator is the same self-flattery
    as reporting promise-to-pay and calling it money.
"""

from __future__ import annotations

import operator
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt

from trail.models import (
    AccountProfile,
    CallRecord,
    Dispute,
    LLMCallTrace,
    PaymentCommitment,
    PaymentPath,
    Step,
    TerminalState,
    TurnExtraction,
    next_step,
)
from trail.money import (
    format_brl,
    format_date_ptbr,
    format_days_past_due,
    format_product_ptbr,
    is_valid_cpf,
)
from trail.protocol import Protocol

# --------------------------------------------------------------------------
# Administrative utterances
# --------------------------------------------------------------------------
#
# NOT APPROVED COLLECTIONS CONTENT. These three sentences name no amount, no
# product, no due date, no fee, no term and no customer, and they give no
# instruction and grant nothing. They live here rather than in the protocol file
# because that file is the register of *approved collections text*, and putting
# administrative call-handling text in it would blur the boundary that makes
# verbatim delivery meaningful. The invariant is enforced, not asserted: the
# offline test suite runs `assert_no_unauthorised_concession` and
# `assert_no_unverified_party_disclosure` over all three.
#
# ONE DELIBERATE DEVIATION FROM THE APPROVED WORDING, AND WHY.
#
# Two of these three constants are reachable *before* the identity gate has
# passed. `IDENTITY_REPROMPT_UTTERANCE` obviously is — it exists to be spoken at
# `verify_right_party`. `TRANSFER_TO_HUMAN_UTTERANCE` less obviously so, and it
# is the interesting one: `_listen` routes to `transferred_to_human` on any
# `needs_human` extraction at *any* step, `verify_right_party` included, and
# `force_transfer` does the same when the compliance gate refuses an utterance.
# A caller who says "quem é? me tira dessa lista" on the opening turn hears the
# transfer sentence with identity still unproven.
#
# So the institution name is stripped from it. The approved wording reads "um
# especialista do Banco Aurora"; what is spoken is "um especialista".
#
# Note what this is NOT resting on. `compliance._DISCLOSURE_TERMS` deliberately
# does *not* list "banco aurora" or "aurora" — see the argued comment above that
# tuple — because the approved `verify_right_party` block names the lender by
# design, and it has to: a caller has a right to know who is calling, and an
# automated call from an unnamed institution asking for a CPF is
# indistinguishable from a scam. So the scanner would not have caught this
# sentence, and the strip is a judgement rather than a rule being obeyed.
#
# The judgement is that "who is chasing you" is a weaker fact than the approved
# opening already discloses, but it is not nothing, and it is disclosed here for
# no benefit: the approved block that names the bank does so to establish
# legitimacy before asking for identifiers, and a hand-off sentence buys none of
# that. Where a disclosure earns something it stays; where it earns nothing it
# goes. Fail closed on the cheap side.
#
# The alternative — two transfer utterances, one pre-verification and one post —
# was rejected because a transfer whose *wording* varies with what the agent
# knows about the caller is a transfer that leaks what the agent knows, and
# because the whole point of this exit is that every reason for taking it
# produces identical behaviour.
# `NOT_RIGHT_PARTY_UTTERANCE` never named the bank and needs no such surgery.

TRANSFER_TO_HUMAN_UTTERANCE = (
    "Obrigado. Vou transferir você para um especialista agora. "
    "Por favor, aguarde na linha."
)

NOT_RIGHT_PARTY_UTTERANCE = (
    "Obrigado. Não posso continuar esta ligação e não vou deixar nenhum recado. "
    "Tenha um bom dia."
)

IDENTITY_REPROMPT_UTTERANCE = (
    "Obrigado. Antes de continuar, preciso do seu nome completo e do seu CPF "
    "ou da sua data de nascimento. Por favor, me informe os dois."
)

#: How many turns the agent will spend collecting identifiers before it treats
#: an unverified line as the wrong party. Matched to the terms-restatement
#: allowance: one retry, then fail closed.
MAX_IDENTITY_ATTEMPTS = 2

#: Node names for the terminal states. They are the ``TerminalState`` values, so
#: a rendered graph reads in the same vocabulary as the outcome counts on the
#: eval scorecard, rather than in node names invented for the drawing.
_TRANSFER = TerminalState.TRANSFERRED_TO_HUMAN.value
_WRONG_PARTY = TerminalState.NOT_RIGHT_PARTY.value
_NOT_REACHED = TerminalState.NOT_REACHED.value
_POST_OUTCOME = Step.POST_OUTCOME.value

#: The steps the agent waits at. `post_outcome` is absent because the agent
#: never listens there: the closing statement is spoken and the call ends on the
#: same turn.
LISTENING_STEPS = tuple(step for step in Step if step is not Step.POST_OUTCOME)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# The one customer-specific utterance, and the one place its values are built
# --------------------------------------------------------------------------


def slots_for_call(profile: AccountProfile) -> dict[str, str]:
    """Build the slot values for this call's ``state_balance`` utterance.

    Four keys — ``product``, ``balance``, ``due_date``, ``days_past_due`` — which
    are exactly what ``Protocol.slots_for(Step.STATE_BALANCE)`` parses out of the
    approved block. :meth:`trail.protocol.Protocol.render` requires the two
    sets to match exactly in both directions, so a block edited without a change
    here (or the reverse) raises at the first call rather than speaking a brace.

    **This is the only call site of the render-side formatters, and it must
    stay that way.** The agent renders the utterance it is about to speak;
    ``assert_agent_text_is_approved`` renders the same block to decide whether
    that utterance is approved. If the two ever built the dictionary
    differently — one padding cents and the other not, one saying "empréstimo"
    and the other "emprestimo pessoal" — the allowlist would reject the agent's
    own approved text, and the failure would arrive as a mystery compliance
    violation rather than as a diff. One function, called twice, cannot disagree
    with itself.

    Every value is a pure function of an :class:`~trail.models.AccountProfile`
    field that came from the system of record. Nothing here reads a model
    output, a customer utterance, the clock or the environment, which is what
    makes the rendered utterance reproducible by a compliance reviewer holding
    only the record and the protocol file.

    :func:`~trail.money.format_days_past_due` raises on a non-positive count
    and :func:`~trail.money.format_product_ptbr` raises on a product with no
    approved spoken name. Both are deliberate: a broken profile stops the one
    step that speaks numbers aloud instead of improvising a phrase for it.
    """
    return {
        "product": format_product_ptbr(profile.product),
        "balance": format_brl(profile.balance_brl),
        "due_date": format_date_ptbr(profile.due_date),
        "days_past_due": format_days_past_due(profile.days_past_due),
    }


# --------------------------------------------------------------------------
# The identity gate — deterministic, and deliberately not the model's opinion
# --------------------------------------------------------------------------

_NON_WORD = re.compile(r"[^a-z0-9]+")

# `[^0-9]` rather than `\D`, for the reason `trail.money` gives: `\D` leaves
# Unicode digits from other scripts in place, and a CPF assembled out of
# Arabic-Indic digits would never compare equal to the booked value while
# looking, in a log, exactly like one that should have.
_NON_DIGIT = re.compile(r"[^0-9]+")


def _token_list(value: str) -> list[str]:
    """Case-folded, accent-stripped name tokens, **in the order they were said**.

    Accents are folded rather than matched because a transcript spells the same
    name both ways depending on the ASR — "João" and "Joao", "Sá" and "Sa" — and
    a gate that treated those as different people would fail closed on a correct
    answer. Folding cannot change *which* name is meant, which is the test every
    normalisation in this system has to pass.

    Order is preserved because :func:`_family_name_matches` needs to know which
    token is the given name. Returning a set here was a real defect: set
    iteration order is arbitrary, so "the first token" was whichever one the
    hash landed on, and the given-name exclusion silently protected a random
    name instead of the first one.
    """
    decomposed = unicodedata.normalize("NFD", value.casefold())
    folded = "".join(c for c in decomposed if not unicodedata.combining(c))
    return [token for token in _NON_WORD.split(folded) if token]


def _tokens(value: str) -> set[str]:
    return set(_token_list(value))


def _digits(value: str) -> str:
    return _NON_DIGIT.sub("", value)


def identity_matches(profile: AccountProfile, extraction: TurnExtraction) -> bool:
    """Whether the person on the line gave the booked customer's identifiers.

    Two deterministic comparisons against the account, plus the model's own
    ``identity_confirmed`` as a **veto rather than a permission**. All three must
    hold:

    * the **surname** on the account appears among the tokens of ``stated_name``
      — surname rather than the whole name because given names are nicknamed
      constantly and "Zé" for "José" is not a wrong party;
    * a **second identifier** matches: ``stated_tax_id``, stripped to digits,
      equals the booked CPF *and* passes :func:`~trail.money.is_valid_cpf`; or,
      when no CPF was given at all, ``stated_date_of_birth`` parses as a date and
      **equals** the booked one;
    * the extraction's ``identity_confirmed`` is not an explicit ``False``.

    That last one is a veto, not a permission, and the asymmetry is deliberate.
    An explicit denial is information only the model has — "sou o marido dela, o
    nome dela é Marina Rocha, CPF 529.982.247-25" matches the account on both
    identifiers and is still the wrong party, and the disclaimer is the only
    thing that says so. A *null* verdict is the absence of that signal, not its
    negation, and it is what an accumulated identity looks like: when the name
    arrives on one turn and the CPF on the next, no single turn ever sees both,
    so no single turn can honestly answer the question. Requiring a ``True``
    there would make a two-turn identity permanently unprovable.

    **Why the second identifier is stronger here than in the healthcare system
    this gate is ported from.** There the second identifier was a date of birth,
    and a date of birth is guessable in a way that matters: it is a value with
    roughly thirty thousand plausible settings, frequently known to a spouse, an
    adult child or anyone holding the post, and it carries no internal structure
    that a wrong guess can fail. A CPF is a checksummed national identifier. Two
    of its eleven digits are a weighted function of the other nine, so a
    transcription slip or an invented number fails arithmetic before it ever
    reaches the equality test — which is why :func:`~trail.money.is_valid_cpf`
    is a *separate* condition rather than an optimisation on the comparison. The
    equality check alone would already reject a wrong CPF; running the checksum
    as well is what distinguishes "this is somebody else's number" from "this is
    not a number", and only the second is a signal that the line is being
    guessed at. Repeated-digit CPFs are rejected there too, because they pass
    the checksum and are exactly what a caller produces when inventing one.

    Date of birth remains as the **fallback** second identifier, and only as a
    fallback, because declining to say a CPF over the phone is not evasive
    behaviour — in Brazil it is good behaviour, and a gate that treated it as a
    failure would filter out the most security-literate customers in the book. A
    CPF that *was* stated and does not match is not repaired by a correct
    birthday: a stated identifier is a claim, and a wrong claim is a stronger
    signal than a missing one. Both absent is a fail.

    Everything else fails, including a missing field, an unparseable date, and an
    extraction that declined to answer. Third-party disclosure of a debt is a
    direct FDCPA violation and BLUEPRINT §5's first zero-tolerance failure; this
    is the only hard gate in the system, so it fails closed in every direction.

    **Why this is not left to the model.** The caller's own utterance is
    interpolated into the extraction prompt, so a boolean produced from that
    prompt is a boolean the utterance can argue with — and the thing it unlocks
    is every word about the debt in the protocol, starting with the balance. The
    model still reads the utterance, which is what it is for: it transcribes the
    identifiers into ``stated_name``, ``stated_tax_id`` and
    ``stated_date_of_birth``, and this function decides. Capture, then compare.
    """
    if extraction.identity_confirmed is False:
        return False
    if not extraction.stated_name:
        return False

    if not _family_name_matches(profile.full_name, extraction.stated_name):
        return False

    stated_cpf = _digits(extraction.stated_tax_id or "")
    if stated_cpf:
        return stated_cpf == profile.tax_id and is_valid_cpf(stated_cpf)

    if not extraction.stated_date_of_birth:
        return False
    try:
        stated = date.fromisoformat(extraction.stated_date_of_birth.strip())
    except ValueError:
        return False
    return stated == profile.date_of_birth


#: Portuguese name particles. They are not family names and they are dropped
#: before matching — "da" appearing in both names proves nothing about identity.
_NAME_PARTICLES = frozenset({"de", "da", "do", "das", "dos", "e", "del", "di"})

#: A family-name token shorter than this is not distinctive enough to compare.
#: Healthcare used 4 to keep "Al", "Jo" and "de" from matching inside ordinary
#: words. That floor is wrong here in both directions: it would reject "Sá",
#: "Luz" and "Reis", which are real Brazilian family names, and it was never what
#: kept particles out — :data:`_NAME_PARTICLES` does that by name, which is the
#: correct instrument, because "da" is not short, it is not a name.
#:
#: Two is therefore the floor, and it is safe here for a reason that did not hold
#: in the original: this comparison is token-equality against a tokenised name,
#: never a substring search, so a two-letter family name cannot match inside a
#: longer word. It can only match a caller who said that exact token.
_MIN_FAMILY_NAME_CHARS = 2


def _family_name_matches(booked: str, stated: str) -> bool:
    """Whether ``stated`` carries at least one of ``booked``'s family names.

    **This is the one place the port had to loosen a healthcare rule, and the
    reason is the locale rather than the risk appetite.** The original took the
    last token of the booked name as *the* surname and required it verbatim,
    which works where names are given-name-plus-one-surname. Brazilian names are
    routinely given name plus maternal plus paternal family name, with particles
    in between — "Marina Rocha da Silva Santos" — and the same person answers
    the phone as "Marina Rocha", "Marina Santos" or "Marina Rocha Santos"
    depending on which one they use day to day. Demanding the final token would
    have sent a large, systematic and entirely legitimate slice of customers to
    ``not_right_party``, and it would have done it hardest to people with the
    longest names.

    So: any booked family-name token, particles removed, matching any token of
    what the caller said. Rejected alternatives, both of them worse:

    * **Any booked token at all, including the given name.** "Maria" and "José"
      are among the most common given names in Brazil, so a wrong party who
      happens to share one — a spouse, a parent, a child — would clear the name
      half of the gate on nothing. The given name is dropped for exactly that
      reason.
    * **Full-name equality.** Unimpeachable and useless: it fails on a middle
      name the caller omits, and the FDCPA cost of refusing the right party is
      not zero — it is a customer who cannot resolve a debt they wanted to
      resolve, which is the harm BLUEPRINT §7 is about.

    The gate does not rest on this. The name is corroboration; the load is
    carried by an exact match on a checksummed CPF, or failing that on an exact
    date of birth. Loosening the weakest of three conjuncts changes what a
    legitimate customer has to say, not what an impostor has to know.
    """
    booked_tokens = [t for t in _token_list(booked) if t not in _NAME_PARTICLES]
    if len(booked_tokens) < 2:
        # A single-token booked name has no family name to distinguish from the
        # given one. Compare the whole thing rather than guess which it is.
        return bool(booked_tokens) and booked_tokens[0] in _tokens(stated)

    family = {t for t in booked_tokens[1:] if len(t) >= _MIN_FAMILY_NAME_CHARS}
    return bool(family & _tokens(stated))


# --------------------------------------------------------------------------
# What crosses the graph boundary
# --------------------------------------------------------------------------


@dataclass
class CallState:
    """Everything one in-flight call knows about itself.

    The graph's state schema, and therefore what the checkpointer holds for the
    duration of a call. Never persisted as-is: what survives is the
    :class:`~trail.models.CallRecord` built from it plus the turn and LLM
    traces written as the call runs.

    Six fields accumulate across turns and carry ``operator.add`` reducers, so a
    node returns only what this turn added rather than re-reading and rewriting
    the whole list.
    """

    call_id: UUID
    profile: AccountProfile
    started_at: datetime
    step: Step = Step.VERIFY_RIGHT_PARTY
    case_id: str | None = None

    ended_at: datetime | None = None
    finished: bool = False
    terminal_state: TerminalState | None = None
    #: What the agent says on the way out. Only a terminal node sets it; every
    #: other utterance leaves through the ``interrupt`` that asks for a reply.
    agent_utterance: str = ""

    identity_confirmed: bool = False
    identity_attempts: int = 0

    #: Identifiers accumulate across the verify turns. Asked for two things,
    #: people commonly give one and then the other, and a reprompt whose answer
    #: cannot be combined with what was already said is a reprompt that can never
    #: succeed. Only non-null values overwrite, so a later turn that mentions
    #: neither does not erase what the earlier one established.
    stated_name: str | None = None
    stated_tax_id: str | None = None
    stated_date_of_birth: str | None = None
    consent_given: bool | None = None
    terms_confirmed: bool | None = None
    terms_attempts: int = 0
    selected_path: PaymentPath | None = None
    contact_channel_confirmed: bool | None = None

    #: Set when the call must end with a specialist callback. Only ever set by
    #: rules that are independent of *what* the customer said — see the node
    #: functions.
    needs_callback: bool = False

    commitments: Annotated[list[PaymentCommitment], operator.add] = field(
        default_factory=list
    )
    disputes: Annotated[list[Dispute], operator.add] = field(default_factory=list)

    #: Every utterance actually delivered, in order. Read by the wrong-party
    #: disclosure assertion, which needs the whole transcript rather than one
    #: turn to answer "was anything about the debt disclosed before identity was
    #: confirmed?".
    agent_transcript: Annotated[list[str], operator.add] = field(default_factory=list)

    total_input_tokens: Annotated[int, operator.add] = 0
    total_output_tokens: Annotated[int, operator.add] = 0
    cost_usd: Annotated[float, operator.add] = 0.0


@dataclass(frozen=True)
class TurnOutcome:
    """What the agent says next, and whether that was the last thing it says.

    ``step`` is the step the utterance belongs to, which is what
    :class:`~trail.models.TurnResponse` and
    :class:`~trail.models.TurnTrace` both report. On a transfer it is the step
    the call died on, so a reviewer can see where the conversation stopped
    without reading the transcript.
    """

    step: Step
    agent_utterance: str
    finished: bool = False
    terminal_state: TerminalState | None = None


@dataclass(frozen=True)
class Turn:
    """One customer reply, as the service hands it back to the waiting node.

    The extraction and the terms-restatement verdict are the service's two model
    calls; the token and cost fields are what they cost, folded into the call's
    running totals by whichever node receives them.
    """

    extraction: TurnExtraction | None = None
    terms_correct: bool | None = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    cost_usd: float = 0.0

    #: Ends or holds the call without reading ``extraction``. ``"retry"`` keeps
    #: the same question open after a model call the service could not complete,
    #: so its cost is still accounted for and the caller can resubmit the turn.
    #: The other two are terminal node names and end the call there.
    override: Literal["retry", "transferred_to_human", "not_reached"] | None = None


def usage(traces: Iterable[LLMCallTrace]) -> dict[str, Any]:
    """Fold model-call traces into the token and cost fields of a :class:`Turn`.

    ``total_input_tokens`` is every input token the model processed: the
    uncached remainder, what was served from cache, and what was written into
    it. The three are disjoint and their sum is the prompt. The split the
    economics post needs — a cache read costs a tenth of fresh input — lives on
    the per-call :class:`~trail.models.LLMCallTrace` rows, where it belongs.
    """
    traces = tuple(traces)
    return {
        "total_input_tokens": sum(
            t.input_tokens + t.cache_read_input_tokens + t.cache_creation_input_tokens
            for t in traces
        ),
        "total_output_tokens": sum(t.output_tokens for t in traces),
        "cost_usd": sum(t.cost_usd for t in traces),
    }


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------
#
# One per step the agent listens at. Each says its approved text, waits, applies
# a rule that reads only the *shape* of what came back, and names the next node.

#: A step rule: given the state before the turn, the reply, and the state update
#: accumulated so far, return where the call goes next.
Rule = Callable[[CallState, Turn, dict[str, Any]], Command]


def _say(state: CallState, step: Step, protocol: Protocol) -> str:
    """The approved text for ``step``, rendered where it is slotted.

    ``state_balance`` goes through :meth:`~trail.protocol.Protocol.render`
    with :func:`slots_for_call`; every other block is verbatim. Reaching for
    ``text_for`` on the slotted block would return the raw template, braces and
    all, and speaking that fails the allowlist — which is the designed
    behaviour, not a hazard to route around here.

    **The two retries are re-readings, never corrections.** On an incomplete
    identity answer the agent asks for both identifiers again in administrative
    words that disclose nothing. On an incorrect restatement it re-reads the
    rendered ``state_balance`` block and the ``confirm_terms`` question, both
    unchanged, and asks once more. It never composes a correction, never states
    the amount in different words, never meets the customer halfway on a
    near-miss and never adapts the approved text to what they said. A
    paraphrased balance is a fabricated figure no matter how close it lands, and
    "helping" a customer converge on a number is the agent asserting a fact it
    cannot verify — the only fact it holds is the one already in the block, and
    the only correction available is to read that block again.
    """
    if step is Step.VERIFY_RIGHT_PARTY and state.identity_attempts:
        return IDENTITY_REPROMPT_UTTERANCE
    if step is Step.STATE_BALANCE:
        return protocol.render(Step.STATE_BALANCE, slots_for_call(state.profile))
    if step is Step.CONFIRM_TERMS and state.terms_attempts:
        return (
            protocol.render(Step.STATE_BALANCE, slots_for_call(state.profile))
            + "\n\n"
            + protocol.text_for(Step.CONFIRM_TERMS)
        )
    return protocol.text_for(step)


def _listen(
    step: Step, protocol: Protocol, rule: Rule
) -> Callable[[CallState], Command]:
    """Build the node for one step: say, wait, screen, then apply ``rule``.

    The two checks every turn gets, whatever step it arrived at, live here and
    nowhere else — an immediate human transfer on any non-routine turn, and a
    specialist callback on a turn the record cannot carry whole. Both read a bit
    and never a reason: the agent makes no classification of what it heard
    (BLUEPRINT §7). Hardship, vulnerability, a request for a person and a
    question the approved script cannot answer all arrive as the same
    ``needs_human`` bit and all leave through the same exit, in the same words.
    """

    def node(state: CallState) -> Command:
        # Everything above `interrupt` re-runs when the service resumes, so it
        # has to stay pure. `_say` reads the protocol, the profile and the retry
        # counters, and `slots_for_call` is a pure function of the profile.
        outcome = TurnOutcome(step=step, agent_utterance=_say(state, step, protocol))
        turn: Turn = interrupt(outcome)

        update: dict[str, Any] = {
            "total_input_tokens": turn.total_input_tokens,
            "total_output_tokens": turn.total_output_tokens,
            "cost_usd": turn.cost_usd,
        }
        if turn.override == "retry":
            # A model call the service could not complete. The question stays
            # open and the node re-asks it verbatim; only the cost is kept.
            return Command(goto=step.value, update=update)
        if turn.override is not None:
            # The utterance never reached the customer — the compliance gate
            # refused it, or nobody answered — so it is not transcript.
            return Command(goto=turn.override, update=update)

        update["agent_transcript"] = [outcome.agent_utterance]
        extraction = turn.extraction
        if extraction is None:
            return Command(goto=_TRANSFER, update=update)

        # WHAT THE CUSTOMER SAID IS KEPT EVEN WHEN THE CALL IS ABOUT TO END, AND
        # THAT ORDERING IS THE POINT.
        #
        # The healthcare original wrote its captured entities inside the step
        # rules, which was correct there because an allergy and a request for a
        # person almost never arrived on the same turn. Here they systematically
        # do: BLUEPRINT §5 and §7 route an explicit dispute to a person, so the
        # single most important thing a customer says — "esse valor está errado,
        # eu já paguei" — is said on precisely the turn that transfers. Capturing
        # after the `needs_human` branch meant the record reached the specialist
        # with the dispute deleted, and the specialist then phoned a customer to
        # ask a question the customer had already answered.
        #
        # Writing it here instead is pure capture and changes no routing: the
        # transfer still happens, in the same words, for the same non-reason. It
        # only stops the exit from being lossy. The rules below therefore read
        # `turn.extraction` for their nullity checks and never write these two
        # lists again — both fields carry `operator.add` reducers, so a second
        # write would append the same rows twice.
        update["commitments"] = list(extraction.commitments)
        update["disputes"] = list(extraction.disputes)

        if extraction.needs_human:
            return Command(goto=_TRANSFER, update=update)
        if extraction.unresolved:
            update["needs_callback"] = True
        return rule(state, turn, update)

    return node


def _next(step: Step, update: dict[str, Any]) -> Command:
    """Advance to the step after ``step``, or to the closing statement."""
    nxt = next_step(step)
    if nxt is None or nxt is Step.POST_OUTCOME:
        return Command(goto=_POST_OUTCOME, update=update)
    return Command(goto=nxt.value, update={**update, "step": nxt})


def _verify_right_party(
    state: CallState, turn: Turn, update: dict[str, Any]
) -> Command:
    """Fails closed in every direction, and deterministically.

    Merge this turn's identifiers into what the call already knows, then judge
    the accumulation rather than the turn: `model_copy` keeps
    :func:`identity_matches` comparing one extraction against the account, so
    the gate itself is unchanged — it is simply shown everything the caller has
    said instead of only the last thing.
    """
    extraction = turn.extraction
    assert extraction is not None  # guaranteed by `_listen`
    name = extraction.stated_name or state.stated_name
    tax_id = extraction.stated_tax_id or state.stated_tax_id
    dob = extraction.stated_date_of_birth or state.stated_date_of_birth
    update |= {
        "stated_name": name,
        "stated_tax_id": tax_id,
        "stated_date_of_birth": dob,
    }

    accumulated = extraction.model_copy(
        update={
            "stated_name": name,
            "stated_tax_id": tax_id,
            "stated_date_of_birth": dob,
        }
    )
    if identity_matches(state.profile, accumulated):
        return _next(Step.VERIFY_RIGHT_PARTY, update | {"identity_confirmed": True})

    attempts = state.identity_attempts + 1
    update["identity_attempts"] = attempts
    # An *absent* identifier is recoverable; one that was given and did not
    # match is not. Answering the first half of a two-part question is ordinary
    # human behaviour, and "this is not the customer" is a strong claim to make
    # about someone who simply has not finished answering — particularly when
    # the second half is a CPF, which people routinely hesitate over before
    # giving. Ask once more; the reprompt is administrative, names no
    # institution and discloses nothing.
    if (not name or not (tax_id or dob)) and attempts < MAX_IDENTITY_ATTEMPTS:
        return Command(goto=Step.VERIFY_RIGHT_PARTY.value, update=update)
    return Command(goto=_WRONG_PARTY, update=update)


def _disclose_and_consent(
    state: CallState, turn: Turn, update: dict[str, Any]
) -> Command:
    """Also fails closed: a customer who did not clearly agree gets a person.

    There is no second ask, and the agent has no approved text for one — which
    is the strongest form the rule can take, because the capability simply does
    not exist rather than being a rule a model is asked to respect. A refusal or
    a withdrawal is a transfer, with no attempt to persuade.
    """
    assert turn.extraction is not None
    given = turn.extraction.consent_given is True
    update["consent_given"] = given
    if not given:
        return Command(goto=_TRANSFER, update=update)
    return _next(Step.DISCLOSE_AND_CONSENT, update)


def _state_balance(state: CallState, turn: Turn, update: dict[str, Any]) -> Command:
    """The customer is acknowledging figures that were just read to them.

    No rule and no callback: this step asserts, it does not collect. The block
    invites a correction ("se esse valor não bate com o que você tem, me diga
    agora"), and a customer who takes the invitation is saying something the
    approved script cannot answer — so it arrives as ``needs_human`` and
    ``_listen`` has already transferred before this function runs. The verbatim
    words survive on the turn trace, which is where a specialist reads them.
    Writing a ``Dispute`` row here as well would be a second, quieter copy of
    the same fact, and the two could disagree.
    """
    return _next(Step.STATE_BALANCE, update)


def _confirm_terms(state: CallState, turn: Turn, update: dict[str, Any]) -> Command:
    """Score one restatement attempt; a second failure does not abort the call.

    ``turn.terms_correct`` is the verdict from
    :meth:`trail.agent.llm.LLMClient.judge_terms_restatement`, which reads only
    whether the customer reproduced the amount **and** the date against the
    rendered approved text. On an incorrect first restatement the node loops back
    on itself and ``_say`` re-reads the rendered balance. On a second failure the
    discrepancy is recorded, a specialist callback is required, and the remaining
    steps still run: an unconfirmed restatement is information for the
    specialist, and the payment path, the commitment and the contact channel are
    worth collecting either way.

    The rule reads a boolean and never the restatement itself, so it cannot
    become a judgement about how articulate the customer was — which matters
    here more than most places, because BLUEPRINT §6's fairness stratification
    says the customers most likely to be misheard are the ones the duty exists to
    protect, and this is the step where an ASR error turns into a record.
    """
    attempts = state.terms_attempts + 1
    update["terms_attempts"] = attempts
    if turn.terms_correct is True:
        return _next(Step.CONFIRM_TERMS, update | {"terms_confirmed": True})
    if attempts == 1:
        return Command(goto=Step.CONFIRM_TERMS.value, update=update)
    return _next(
        Step.CONFIRM_TERMS,
        update | {"terms_confirmed": False, "needs_callback": True},
    )


def _offer_payment_path(
    state: CallState, turn: Turn, update: dict[str, Any]
) -> Command:
    """A customer who chose none of the four approved paths gets a callback.

    The rule reads whether a path was named, never *which* — the four are
    described identically to every customer and are worth the same to this
    machine, so choosing instalments over paying now changes nothing about
    routing, the queue, or anything else. That symmetry is what keeps the
    approved capability statement ("não consigo oferecer descontos...") a
    statement about the system rather than a response to a request, and it is
    also why :class:`~trail.models.PaymentPath` is a closed enum: a settlement
    or a bespoke plan has no representation here even if one were somehow said.

    A null path is a completeness failure and nothing more. It does not record
    that the customer refused, or hesitated, or asked for something else.
    """
    assert turn.extraction is not None
    update["selected_path"] = turn.extraction.selected_path
    if turn.extraction.selected_path is None:
        update["needs_callback"] = True
    return _next(Step.OFFER_PAYMENT_PATH, update)


def _capture_commitment(
    state: CallState, turn: Turn, update: dict[str, Any]
) -> Command:
    """THE CONCESSION BOUNDARY, AND THE REASON THIS RULE READS NULLITY.

    A commitment row missing its amount or its date is a row the specialist has
    to phone about — not because of *how much* was promised, but because the call
    did not finish writing it down. The test reads two fields for ``None`` and
    never reads their values, so the agent behaves identically whether the
    customer promises four thousand reais or forty: a customer promising
    R$ 4.000,00 with a date comes out fully automated, and a customer promising
    R$ 40,00 who will not name a day does not.

    That is uncomfortable and it is correct. Deciding *which amounts* warrant
    human review is customer-specific logic and the agent holds no threshold —
    nor should it, because a threshold is precisely the "how much is this account
    worth" judgement that BLUEPRINT §7 refuses and that
    :class:`~trail.models.CallRecord` has no field to carry. The healthcare
    system this rule is ported from made the same trade one industry over,
    reading a medication row for a missing dose, unit or frequency and never for
    what the drug was, because routing on the drug is patient-specific
    medication logic and a potential FDA device function. Same shape, same
    reason, different regulator: routing on the amount is customer-specific
    collections logic and the thing FDCPA and UDAAP make expensive.

    An amount or a date the customer named but could not pin down produces no
    row at all — the value would have to be guessed, and a guessed figure in a
    payment plan is the most dangerous fabrication in this system — so it reaches
    ``_listen`` as ``extraction.unresolved`` instead.

    **The disputes half of this node has no callback rule, on purpose.** A
    recorded :class:`~trail.models.Dispute` does not require one: the
    specialist reviews every record regardless, and making a dispute trigger a
    callback would be the agent assessing that dispute's merit — routing on the
    content of what the customer said, which is the exact pattern BLUEPRINT §7
    rules out, and the tempting direction, because nothing feels risky about
    being a little more careful with someone who says they already paid. "Já
    paguei", "esse valor não é meu" and "eu nunca peguei esse empréstimo" are
    three different facts for the specialist and this machine is not permitted
    to decide which is serious, let alone which is true. FDCPA §809(b)
    cease-collection-on-dispute is the specialist's action, taken by a person, on
    a record that reached them exactly like every other record. A dispute the
    customer left *unfinished* is a different thing and arrives as
    ``unresolved``; an explicit one is also something the approved script cannot
    answer and arrives as ``needs_human``, which transfers.
    """
    assert turn.extraction is not None
    # `_listen` has already written both lists into `update`; this rule only
    # reads them. Writing again would double-count through the `operator.add`
    # reducer on `CallState.commitments`.
    rows = turn.extraction.commitments
    if not all(row.amount is not None and row.date is not None for row in rows):
        update["needs_callback"] = True
    return _next(Step.CAPTURE_COMMITMENT, update)


def _confirm_contact(state: CallState, turn: Turn, update: dict[str, Any]) -> Command:
    """A contact channel that was not explicitly confirmed means a specialist phones.

    "Provavelmente chega no meu celular" is not a confirmation, and the failure
    it guards is a payment link sent to a stale channel — which reads to the
    customer as the bank ignoring a promise they made minutes earlier, and shows
    up in the numbers as a repeat contact nobody can explain. This is an
    administrative delivery fact, not a judgement about the customer: the rule
    reads whether a channel was affirmed and nothing about which one, and it
    fails closed — an unanswered question is not a channel.
    """
    assert turn.extraction is not None
    update["contact_channel_confirmed"] = turn.extraction.contact_channel_confirmed
    if turn.extraction.contact_channel_confirmed is not True:
        update["needs_callback"] = True
    return _next(Step.CONFIRM_CONTACT, update)


RULES: dict[Step, Rule] = {
    Step.VERIFY_RIGHT_PARTY: _verify_right_party,
    Step.DISCLOSE_AND_CONSENT: _disclose_and_consent,
    Step.STATE_BALANCE: _state_balance,
    Step.CONFIRM_TERMS: _confirm_terms,
    Step.OFFER_PAYMENT_PATH: _offer_payment_path,
    Step.CAPTURE_COMMITMENT: _capture_commitment,
    Step.CONFIRM_CONTACT: _confirm_contact,
}


# --------------------------------------------------------------------------
# Terminal nodes
# --------------------------------------------------------------------------


def _end(step: Step, utterance: str, terminal: TerminalState) -> dict[str, Any]:
    return {
        "step": step,
        "agent_utterance": utterance,
        "finished": True,
        "terminal_state": terminal,
        "ended_at": _utcnow(),
    }


def _post_outcome(state: CallState, protocol: Protocol) -> dict[str, Any]:
    """The closing statement, and the only place the primary metric is decided.

    ``needs_callback`` is not a priority score and changes no routing. Every
    record reaches the same specialist queue in ``started_at`` order either way,
    and the approved ``post_outcome`` text tells every customer, out loud, that
    a specialist reviews the call "em todas as ligações, sem exceção". What the
    flag decides is the *terminal state*, which is a measurement — it is what
    separates an honest fully-automated rate from the vendor habit of reporting
    promise-to-pay and calling it money.
    """
    return _end(
        Step.POST_OUTCOME,
        protocol.text_for(Step.POST_OUTCOME),
        TerminalState.COMPLETED_NEEDS_CALLBACK
        if state.needs_callback
        else TerminalState.COMPLETED_NO_CALLBACK,
    )


def _transferred_to_human(state: CallState) -> dict[str, Any]:
    """End the call by handing it to a person, saying nothing about why.

    Reached by a refused consent, a ``needs_human`` extraction, and a failed
    compliance assertion. All three are non-routine turns, and all three get the
    same behaviour and the same words — which is the point: a transfer that
    varied with the reason would be the agent classifying what it heard, and the
    thing it would most often be classifying is a customer in difficulty
    (BLUEPRINT §7).
    """
    return _end(
        state.step, TRANSFER_TO_HUMAN_UTTERANCE, TerminalState.TRANSFERRED_TO_HUMAN
    )


def _not_right_party(state: CallState) -> dict[str, Any]:
    return _end(
        Step.VERIFY_RIGHT_PARTY,
        NOT_RIGHT_PARTY_UTTERANCE,
        TerminalState.NOT_RIGHT_PARTY,
    )


def _not_reached(state: CallState) -> dict[str, Any]:
    """Nobody answered. Marked by the caller, never inferred, and no utterance.

    The operational reason — no answer, disconnected number, voicemail — is
    recorded on the span and never on the record, because it is a telephony fact
    and the record is a collections document. The account stays in the primary
    metric's denominator, which is the whole point of modelling non-answer.
    """
    return _end(state.step, "", TerminalState.NOT_REACHED)


# --------------------------------------------------------------------------
# The graph
# --------------------------------------------------------------------------

#: Types allowed out of a checkpoint. The list is short because the state is
#: small, and explicit because a session store is a deserialisation boundary:
#: LangGraph blocks anything not named here in a future release, and a system
#: holding consumer debt data should be reading that list rather than
#: discovering it.
_CHECKPOINT_TYPES = [
    ("trail.models", name)
    for name in (
        "AccountProfile",
        "Step",
        "TerminalState",
        "TurnExtraction",
        "PaymentCommitment",
        "Dispute",
        "PaymentPath",
        "Product",
    )
] + [("trail.agent.machine", name) for name in ("TurnOutcome", "Turn")]


def _destinations(step: Step) -> tuple[str, ...]:
    """Where one listening node can send the call. Drawn, not just declared."""
    nxt = next_step(step)
    onward = _POST_OUTCOME if nxt is None or nxt is Step.POST_OUTCOME else nxt.value
    exits = (step.value, onward, _TRANSFER, _NOT_REACHED)
    return (*exits, _WRONG_PARTY) if step is Step.VERIFY_RIGHT_PARTY else exits


def build_graph(protocol: Protocol) -> CompiledStateGraph:
    """Compile the conversation graph against one approved protocol.

    The protocol is bound here rather than carried in the state: it is loaded
    once at start-up, it is the same for every call, and a copy of the approved
    collections text inside every checkpoint would be a second place it could
    differ from the file that was reviewed. Note what is *not* bound: the slot
    values, which are per-call and are computed from the profile in the state at
    the moment the utterance is said.

    ``InMemorySaver`` is one dict in one process, and that is CORRECT for the
    MVP: a call is a short-lived conversation with strictly ordered turns, and
    putting it behind Redis here would add a container and a failure mode to buy
    nothing this system can exercise. Finished calls are deliberately not
    evicted, so a repeated turn on a completed call answers 409 rather than 404.
    # ponytail: in-process sessions; swap for `AsyncPostgresSaver` when calls
    # must outlive the process that started them (BLUEPRINT §8).
    """
    graph = StateGraph(CallState)
    for step in LISTENING_STEPS:
        graph.add_node(
            step.value,
            _listen(step, protocol, RULES[step]),
            destinations=_destinations(step),
        )
    graph.add_node(_POST_OUTCOME, lambda state: _post_outcome(state, protocol))
    graph.add_node(_TRANSFER, _transferred_to_human)
    graph.add_node(_WRONG_PARTY, _not_right_party)
    graph.add_node(_NOT_REACHED, _not_reached)

    # The entry is the state's own step, so a call can be started — or restored —
    # at any step it waits at, not only at the beginning.
    graph.add_conditional_edges(
        START, lambda state: state.step.value, [step.value for step in LISTENING_STEPS]
    )
    for terminal in (_POST_OUTCOME, _TRANSFER, _WRONG_PARTY, _NOT_REACHED):
        graph.add_edge(terminal, END)

    return graph.compile(
        checkpointer=InMemorySaver(
            serde=JsonPlusSerializer(allowed_msgpack_modules=_CHECKPOINT_TYPES)
        )
    )


# --------------------------------------------------------------------------
# Driving one call
# --------------------------------------------------------------------------
#
# Three functions, all synchronous. Nothing in the graph does I/O — the nodes are
# pure and the checkpointer is a dict — so there is nothing for an event loop to
# interleave, and `invoke` from an async handler blocks for microseconds.
# ponytail: sync invoke; switch to `ainvoke` together with the Postgres saver.


def _config(call_id: UUID) -> dict[str, Any]:
    return {"configurable": {"thread_id": str(call_id)}}


def _outcome(result: dict[str, Any]) -> TurnOutcome:
    """Read what the agent says out of an invoke, paused or finished."""
    pending = result.get("__interrupt__")
    if pending:
        return pending[0].value
    return TurnOutcome(
        step=result["step"],
        agent_utterance=result["agent_utterance"],
        finished=True,
        terminal_state=result["terminal_state"],
    )


def new_call(
    profile: AccountProfile,
    *,
    case_id: str | None = None,
    step: Step = Step.VERIFY_RIGHT_PARTY,
) -> CallState:
    """A call that has not been given to the graph yet.

    ``step`` exists because the graph's entry edge reads it: a call can be
    started — or restored — at any step it waits at. That is what a durable
    session store will need, and what lets a test exercise one step without
    replaying the whole conversation before it.
    """
    return CallState(
        call_id=uuid4(),
        profile=profile,
        started_at=_utcnow(),
        step=step,
        case_id=case_id,
        identity_confirmed=step is not Step.VERIFY_RIGHT_PARTY,
    )


def start(graph: CompiledStateGraph, state: CallState) -> TurnOutcome:
    """Put ``state`` into the graph and produce the agent's first utterance."""
    return _outcome(graph.invoke(state, _config(state.call_id)))


def open_call(
    graph: CompiledStateGraph, profile: AccountProfile, *, case_id: str | None = None
) -> tuple[UUID, TurnOutcome]:
    """Start a call and produce the agent's opening utterance.

    The opening utterance is the approved ``verify_right_party`` text. Nothing
    about the debt is disclosed before identity is confirmed — not the amount,
    not the product, not the due date, not the word *dívida*. The ceiling is
    "uma pendência na sua conta conosco", and it is deliberately uninformative
    to a stranger.
    """
    state = new_call(profile, case_id=case_id)
    return state.call_id, start(graph, state)


def advance(graph: CompiledStateGraph, call_id: UUID, turn: Turn) -> TurnOutcome:
    """Apply one customer turn and return what the agent says next."""
    state = state_of(graph, call_id)
    if state is not None and state.finished:
        raise RuntimeError(f"call {call_id} has already finished")
    return _outcome(graph.invoke(Command(resume=turn), _config(call_id)))


def state_of(graph: CompiledStateGraph, call_id: UUID) -> CallState | None:
    """The current state of ``call_id``, or ``None`` if there is no such call."""
    values = graph.get_state(_config(call_id)).values
    return CallState(**values) if values else None


def force_transfer(graph: CompiledStateGraph, call_id: UUID) -> TurnOutcome:
    """End a call by handing it to a person, whatever state it is in.

    The in-flight case is an ordinary resume into the ``transferred_to_human``
    node. The finished case is not reachable unless the *approved protocol text*
    itself fails the compliance gate, and it is written straight into the
    checkpoint because there is no node left to route to — but it must still
    work, because the terminal state is how a compliance failure is recorded:
    :class:`~trail.models.CallRecord` has no violations column and must not
    gain one, so a call whose output failed an assertion can never be counted as
    a clean, fully automated completion.
    """
    state = state_of(graph, call_id)
    if state is None:
        raise RuntimeError(f"unknown call {call_id}")
    if not state.finished:
        return advance(graph, call_id, Turn(override=_TRANSFER))

    ended = _end(
        state.step, TRANSFER_TO_HUMAN_UTTERANCE, TerminalState.TRANSFERRED_TO_HUMAN
    )
    graph.update_state(_config(call_id), ended)
    return TurnOutcome(
        step=state.step,
        agent_utterance=TRANSFER_TO_HUMAN_UTTERANCE,
        finished=True,
        terminal_state=TerminalState.TRANSFERRED_TO_HUMAN,
    )


# --------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------


def build_record(
    state: CallState, protocol: Protocol, *, prompt_version: str, model: str
) -> CallRecord:
    """Build the finished record for the mocked system of record.

    ``reviewed_by`` and ``reviewed_at`` are left null and
    ``needs_specialist_review`` is pinned true by the model's type, the JSON
    Schema, and a database ``CHECK`` — three independent places, because "every
    AI output requires human verification" is a zero-tolerance requirement and
    not a convention.

    Nothing is computed here. Every field is copied out of the state as the
    rules left it, and there is no ordering key, no score and no summary: the
    record is what happened, and the specialist decides what it means.
    """
    if not state.finished or state.terminal_state is None:
        raise RuntimeError(f"call {state.call_id} has not finished")

    ended_at = state.ended_at or _utcnow()
    return CallRecord(
        call_id=state.call_id,
        account_id=state.profile.account_id,
        started_at=state.started_at,
        ended_at=ended_at,
        terminal_state=state.terminal_state,
        commitments=list(state.commitments),
        disputes=list(state.disputes),
        selected_path=state.selected_path,
        contact_channel_confirmed=state.contact_channel_confirmed,
        consent_given=state.consent_given,
        terms_confirmed=state.terms_confirmed,
        protocol_version=protocol.version,
        prompt_version=prompt_version,
        model=model,
        total_input_tokens=state.total_input_tokens,
        total_output_tokens=state.total_output_tokens,
        cost_usd=state.cost_usd,
        wall_seconds=(ended_at - state.started_at).total_seconds(),
    )
