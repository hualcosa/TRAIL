"""Fixtures shared by both test tiers.

Two tiers, separated by marker (see ``pyproject.toml``):

``@pytest.mark.unit``
    Offline and pure. No network, no database, no API key, no Docker. Every
    test under ``tests/unit`` runs on a laptop in milliseconds, which is what
    keeps the state machine, the protocol loader and — above all — the
    compliance assertions runnable during code review.

``@pytest.mark.integration``
    Requires ``make up`` and a real API key. Skips cleanly with a reason when
    the stack is not there; never fails for the absence of infrastructure.

What lives here is the machinery both tiers need and neither owns: synthetic
account profiles, a throwaway protocol file, an in-memory stand-in for the
system of record, and a fake agent served over an :class:`httpx.MockTransport`
so the eval harness can be driven end to end without a live service or a single
model call.

**Everything customer-specific in this file is a fixed constant.** No fixture
reads the wall clock to build a profile, and that is not tidiness. A balance and
a due date are rendered into the one approved block that speaks numbers aloud,
and ``assert_agent_text_is_approved`` compares the agent's utterance against that
rendered form — so a profile whose ``due_date`` drifted with today's date would
change what the allowlist is comparing against, and produce a suite that passes
on Tuesday and fails on Wednesday for reasons no traceback would explain. See
:data:`AS_OF`.

The fake agent is a **contract stub, not a second implementation**. It answers
the endpoints in INTERFACES §3 with the shapes they promise, and it produces
whatever record its policy tells it to. It deliberately does not re-derive
terminal states from the conversation: a stub that reimplemented the state
machine would let the eval tests pass while both copies were wrong in the same
way, which is the simulator-collusion problem one layer down.
"""

from __future__ import annotations

import itertools
import json
import os
import re
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from trail.agent import machine
from trail.agent.machine import (
    NOT_RIGHT_PARTY_UTTERANCE,
    TRANSFER_TO_HUMAN_UTTERANCE,
    CallState,
    Turn,
    TurnOutcome,
    slots_for_call,
)
from trail.cases import GOLDEN_SET
from trail.config import get_settings
from trail.models import (
    AccountProfile,
    CallRecord,
    Dispute,
    MarkUnreachableRequest,
    PaymentCommitment,
    PaymentPath,
    Product,
    StartCallRequest,
    StartCallResponse,
    Step,
    SyntheticCase,
    TerminalState,
    TurnExtraction,
    TurnRequest,
    TurnResponse,
)
from trail.protocol import Protocol, load_protocol

REPO_ROOT = Path(__file__).resolve().parent.parent

FAKE_AGENT_BASE_URL = "http://agent.test"
"""Base URL of the stub agent. Nothing listens on it; MockTransport intercepts."""

FAKE_PROMPT_VERSION = "test-prompt.0"
FAKE_MODEL = "gpt-5.6-luna"
FAKE_COST_PER_CALL_USD = 0.05
"""Flat per-call spend the stub stamps on every record.

Flat on purpose: it makes ``cost_per_fully_automated_call_usd`` exactly
``0.05 × attempted / automated``, so a test can assert the denominator rule
rather than a floating-point coincidence.
"""

_STEPS: tuple[Step, ...] = tuple(Step)

_EARLY_FINISH_TURN: Mapping[TerminalState, int] = {
    # The customer turn on which the stub ends a call that never runs to the
    # closing statement. A wrong party dies on the identity answer; a refused
    # consent dies on the consent answer. Everything else runs to the end of
    # its script.
    TerminalState.NOT_RIGHT_PARTY: 1,
    TerminalState.TRANSFERRED_TO_HUMAN: 2,
}

_CLOSING_UTTERANCE: Mapping[TerminalState, str] = {
    TerminalState.NOT_RIGHT_PARTY: NOT_RIGHT_PARTY_UTTERANCE,
    TerminalState.TRANSFERRED_TO_HUMAN: TRANSFER_TO_HUMAN_UTTERANCE,
}


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _hermetic_settings() -> Iterator[None]:
    """Guarantee a constructible :class:`~trail.config.Settings` with no real key.

    ``TRAIL_LLM_API_KEY`` has no default, so any module that touches
    ``get_settings()`` would fail to import a value on a bare ``pytest`` run.
    A placeholder is installed when the environment does not already carry one;
    no unit test calls the API, and the integration tier reaches the key through
    the running containers rather than through this process.
    """
    with pytest.MonkeyPatch.context() as patch:
        if not os.environ.get("TRAIL_LLM_API_KEY"):
            patch.setenv("TRAIL_LLM_API_KEY", "unit-tests-never-call-the-api")
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """The repository root, so tests can read shipped artifacts by path."""
    return REPO_ROOT


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

FAKE_PROTOCOL_TEXT = """\
---
version: "0.0.0-test"
locale: pt-BR
---

<!-- protocol_version: 0.0.0-test -->

# Protocolo de teste

Prose for human reviewers. The parser ignores every word of it, including this
sentence and the ``## not_a_step`` heading below.

## not_a_step

```spoken
Este título não é um Step, então este bloco nunca é texto aprovado.
```

## verify_right_party

```spoken
Olá. Por favor, me diga o seu nome completo e o seu CPF.
```

**Reviewer note.** Never spoken. Present so the tests prove it is dropped.

## disclose_and_consent

```spoken
Sou um assistente automático e esta ligação está sendo gravada.

Você autoriza que eu continue?
```

## state_balance

```spoken
Sobre o seu {product}: o valor é {balance}, a data foi {due_date} e a diferença é de {days_past_due}.
```

## confirm_terms

```spoken
Me diga com as suas palavras: qual é o valor e qual foi a data?
```

## offer_payment_path

```spoken
Existem quatro formas de resolver isso. Qual delas você prefere?
```

## capture_commitment

```spoken
Me diga um valor e um dia.
```

## confirm_contact

```spoken
Em qual canal você recebeu o nosso aviso?
```

## post_outcome

```spoken
Era isso que eu precisava. Obrigado pelo seu tempo.
```
"""
"""A minimal, valid protocol used wherever the real approved text is beside the point.

Two properties are load-bearing and neither is decoration.

**The template is free of the vocabulary in**
:data:`trail.agent.compliance._DISCLOSURE_TERMS` — no *dívida*, no *saldo*, no
*pagamento*, no *cartão*, no *R$* — so a state-machine test exercises transitions
rather than tripping the wrong-party disclosure gate, and the opening block can
be handed to :func:`~trail.agent.compliance.check_outbound_utterance` with
``identity_confirmed=False`` and be expected to pass. Note the careful scope of
that claim: it is the *template* that is clean. The **rendered** ``state_balance``
block necessarily is not, because it states an amount of money out loud — that is
the whole purpose of the block, it is only ever spoken after the identity gate,
and the gate is what makes the difference safe. Tests that need text with real
collections content use :func:`real_protocol` instead.

**The** ``state_balance`` **block declares exactly the four slots the machine
supplies.** :func:`trail.agent.machine._say` renders that block through
:func:`~trail.agent.machine.slots_for_call` for every call that reaches step
three, and :meth:`~trail.protocol.Protocol.render` requires the declared and
supplied slot sets to match exactly in both directions. So a fixture protocol
that dropped ``{days_past_due}``, or spelled it ``{dpd}``, would not fail as a
wrong word somewhere in a transcript — it would raise ``ValueError`` inside the
graph and take every state-machine test with it. The four names here and the four
keys in ``slots_for_call`` are the same contract the shipped file is held to.
"""


@pytest.fixture(scope="session")
def real_protocol() -> Protocol:
    """The protocol the agent actually ships with.

    Loaded through :func:`~trail.protocol.load_protocol`, so this fixture is
    itself an assertion that the shipped file parses, covers every
    :class:`~trail.models.Step`, and declares no slot the code does not fill.
    """
    return load_protocol(REPO_ROOT / "protocol" / "collections_1_30_dpd.md")


@pytest.fixture
def write_protocol(tmp_path: Path) -> Iterator[Callable[[str], Path]]:
    """Write a protocol file to a temp path and return it.

    Clears :func:`~trail.protocol.load_protocol`'s cache around every write.
    The loader is ``@cache``d on the path because approved content mounted into
    a container cannot change under a running process — which is true in
    production and false in a test that rewrites a fixture.
    """
    counter = itertools.count()

    def write(text: str) -> Path:
        path = tmp_path / f"protocol_{next(counter)}.md"
        path.write_text(text, encoding="utf-8")
        load_protocol.cache_clear()
        return path

    yield write
    load_protocol.cache_clear()


@pytest.fixture
def fake_protocol_text() -> str:
    """The source of :func:`fake_protocol`, for tests that mutate it."""
    return FAKE_PROTOCOL_TEXT


@pytest.fixture
def fake_protocol(write_protocol: Callable[[str], Path]) -> Protocol:
    """A parsed :data:`FAKE_PROTOCOL_TEXT`."""
    return load_protocol(write_protocol(FAKE_PROTOCOL_TEXT))


def approved_utterance(protocol: Protocol, step: Step, profile: AccountProfile) -> str:
    """What a perfect agent says at ``step``: approved text, rendered where slotted.

    The same two-line decision :func:`trail.agent.machine._say` makes, and it
    exists here for the same reason it exists there — reaching for
    :meth:`~trail.protocol.Protocol.text_for` on a slotted block returns the
    raw template, braces and all, and a stub that spoke one would be a stub whose
    every utterance quietly failed the allowlist while the test it served went
    green.

    The slot mapping comes from :func:`~trail.agent.machine.slots_for_call`,
    which is the single call site of the render-side formatters. A test helper
    that formatted the balance itself would be a second implementation of the one
    thing in this system that is not allowed to have two.
    """
    if protocol.slots_for(step):
        return protocol.render(step, slots_for_call(profile))
    return protocol.text_for(step)


# ---------------------------------------------------------------------------
# Driving the graph without a service
# ---------------------------------------------------------------------------


@dataclass
class DrivenCall:
    """One call driven straight through the conversation graph.

    The agent service resumes the graph over HTTP, one turn per request. This
    does the same thing in-process, which is what lets a state-machine test
    exercise every branch with no network, no database and no API key.
    """

    graph: Any
    call_id: UUID
    opening: TurnOutcome

    @property
    def state(self) -> CallState:
        """What the checkpointer holds for this call right now."""
        state = machine.state_of(self.graph, self.call_id)
        assert state is not None, f"no such call {self.call_id}"
        return state

    def advance(
        self, extraction: TurnExtraction, *, terms_correct: bool | None = None
    ) -> TurnOutcome:
        """Submit one customer turn.

        ``terms_correct`` is the verdict from
        :meth:`trail.agent.llm.LLMClient.judge_terms_restatement` and rides on
        the :class:`~trail.agent.machine.Turn` rather than on the extraction,
        because capture and judgement are two separate model calls: the words the
        customer used survive on the record whatever the judge decided about them.
        """
        return machine.advance(
            self.graph,
            self.call_id,
            Turn(extraction=extraction, terms_correct=terms_correct),
        )

    def override(self, override: str) -> TurnOutcome:
        """Resume without an extraction: ``retry``, or a terminal node name."""
        return machine.advance(self.graph, self.call_id, Turn(override=override))

    def force_transfer(self) -> TurnOutcome:
        return machine.force_transfer(self.graph, self.call_id)


@pytest.fixture
def graph(real_protocol: Protocol) -> Any:
    """A compiled graph over the **shipped** approved text.

    For tests whose subject is the script itself: what the agent says at each
    step, that the rendered balance is the one the record holds, that a golden-set
    case lands where it says it will. :func:`drive` is the other half of the pair
    and binds the neutral :func:`fake_protocol` instead, so a branch test is not
    also a test of Brazilian Portuguese wording.

    Function-scoped, never session-scoped. The checkpointer is one dict in one
    process and finished calls are deliberately not evicted (see
    :func:`trail.agent.machine.build_graph`), so a shared graph would let one
    test's completed call answer another test's ``state_of``.
    """
    return machine.build_graph(real_protocol)


@pytest.fixture
def drive(fake_protocol: Protocol) -> Callable[..., DrivenCall]:
    """Factory for a call in flight, optionally already at a later step.

    ``**overrides`` are written onto the state before the graph ever sees it,
    which is how a test starts a call that already needs a callback or began 90
    seconds ago.
    """
    graph = machine.build_graph(fake_protocol)

    def _drive(
        profile: AccountProfile,
        *,
        step: Step = Step.VERIFY_RIGHT_PARTY,
        case_id: str | None = None,
        **overrides: object,
    ) -> DrivenCall:
        state = machine.new_call(profile, step=step, case_id=case_id)
        for name, value in overrides.items():
            setattr(state, name, value)
        return DrivenCall(graph, state.call_id, machine.start(graph, state))

    return _drive


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

AS_OF: date = date(2026, 8, 15)
"""The fixture's stand-in for "today". Fixed, and it has to be.

``due_date`` and ``days_past_due`` are two views of the same fact and both are
rendered into the same spoken sentence, so they have to agree — a profile saying
"venceu em 3 de agosto" and "12 dias" when only nine days separate them is a
wrong fact spoken aloud, which is the failure this whole port is built around.
Deriving one from the other guarantees they agree; deriving it from
``date.today()`` would guarantee they agree *and* make the rendered utterance
different every morning, which moves the string the compliance allowlist compares
against and produces a suite that fails on a Wednesday with no code change behind
it. The value is the shipped protocol's ``last_reviewed`` date, for no deeper
reason than that a reader who wonders where it came from can find out.
"""

DEFAULT_TAX_ID = "11144477735"
"""A synthetic, checksum-valid CPF. Invented; it belongs to nobody.

Check-digit validity is not cosmetic here. :func:`trail.agent.machine.identity_matches`
runs :func:`~trail.money.is_valid_cpf` as a *separate* condition from the
equality test, so a fixture CPF that failed its own arithmetic would send every
right-party test to ``not_right_party`` — and it would do it for a reason no
assertion message would name.
"""

DEFAULT_BALANCE = Decimal("847.32")
"""Inside the R$ 120 – R$ 6.000 window a 1–30 DPD consumer balance lives in.

A :class:`~decimal.Decimal` literal built from a string, never from a float:
``Decimal(847.32)`` is 847.3199999999999931787…, and this is the number the agent
reads out loud.
"""

DEFAULT_DAYS_PAST_DUE = 12
"""Mid-window. 1 and 30 are the boundaries and belong in tests that mean them."""


@pytest.fixture
def make_profile() -> Callable[..., AccountProfile]:
    """Factory for synthetic delinquent accounts.

    Banco Aurora is fictional and so is everyone in these tests. Brazil has no
    equivalent of the 555-01xx range North American fiction reserves for
    telephone numbers, so these are simply invented rather than drawn from a
    protected block, and nothing in the system ever dials one.

    ``due_date`` is derived from ``days_past_due`` rather than accepted
    separately — see :data:`AS_OF` for why the pair may not be allowed to
    disagree. A test that genuinely needs an inconsistent one constructs an
    :class:`~trail.models.AccountProfile` directly and says so.
    """

    counter = itertools.count(1)

    def make(
        *,
        account_id: str | None = None,
        full_name: str = "Cliente Teste",
        tax_id: str = DEFAULT_TAX_ID,
        date_of_birth: date = date(1984, 3, 9),
        phone: str = "+55 11 90000-0199",
        product: Product = Product.PERSONAL_LOAN,
        balance_brl: Decimal = DEFAULT_BALANCE,
        days_past_due: int = DEFAULT_DAYS_PAST_DUE,
    ) -> AccountProfile:
        index = next(counter)
        return AccountProfile(
            account_id=account_id or f"AUR-TEST-{index:04d}",
            full_name=full_name,
            tax_id=tax_id,
            date_of_birth=date_of_birth,
            phone=phone,
            product=product,
            balance_brl=balance_brl,
            due_date=AS_OF - timedelta(days=days_past_due),
            days_past_due=days_past_due,
        )

    return make


@pytest.fixture
def sample_profile(make_profile: Callable[..., AccountProfile]) -> AccountProfile:
    """One synthetic customer, twelve days past due on a personal loan.

    Twelve days puts the call in the middle of the 1–30 DPD segment the protocol
    is written for (BLUEPRINT §3) — someone who mostly forgot, which is the
    population this system is scoped to and the one it has the clearest ethical
    footing with.

    The name carries three tokens with two family names, which is ordinary in
    Brazil and is the case
    :func:`trail.agent.machine._family_name_matches` exists to handle: this
    customer answers the phone as "Marina Rocha", "Marina Santos" or the whole
    thing, and all three are the right party.
    """
    return make_profile(
        account_id="AUR-TEST-0001",
        full_name="Marina Rocha Santos",
        tax_id="52998224725",
        date_of_birth=date(1984, 3, 9),
        phone="+55 11 90000-0142",
    )


@pytest.fixture
def slots(sample_profile: AccountProfile) -> dict[str, str]:
    """The slot values for :func:`sample_profile`'s ``state_balance`` utterance.

    Built by :func:`~trail.agent.machine.slots_for_call`, which is the single
    call site of the render-side formatters — so a compliance test handing this
    to ``check_outbound_utterance`` is handing it the identical dictionary the
    agent used to render what it said.

    That identity is the fixture's entire reason to exist, and getting it wrong
    is silent in the worst direction. ``assert_agent_text_is_approved`` builds its
    approved set by *rendering* slotted blocks with what it is given, and a
    slotted block whose slots are not supplied contributes **nothing** to that
    set — so calling the gate without ``slots`` makes the agent's own correctly
    rendered balance unapprovable, and every call in the suite transfers on a
    compliance violation that is really a missing fixture argument.
    """
    return slots_for_call(sample_profile)


# ---------------------------------------------------------------------------
# Turns
# ---------------------------------------------------------------------------
#
# One builder per listening step, so a test names the thing it is exercising
# rather than assembling a `TurnExtraction` field by field. They are plain
# functions rather than fixtures because a test needs several of them per call,
# in order, and parametrised by what the customer said.
#
# Note what none of them can express: there is no `hardship`, `severity`,
# `sentiment` or `reason_for_transfer` argument anywhere below, because
# `TurnExtraction` has no such field and must never gain one (CONTRACT §7). A
# customer in difficulty is `needs_human=True` and nothing else — the same bit,
# with the same downstream behaviour, as a customer who asked for a person.


def extraction(step: Step, **fields: object) -> TurnExtraction:
    """One customer turn as the model would have written it down.

    ``understood=True`` by default: these build the *routine* turn for a step, and
    a test that wants an unintelligible one says so.
    """
    return TurnExtraction(
        step=step,
        raw_utterance=f"<resposta roteirizada em {step.value}>",
        understood=True,
        **fields,  # type: ignore[arg-type]
    )


def identity_turn(profile: AccountProfile, **overrides: object) -> TurnExtraction:
    """A ``verify_right_party`` turn in which the right person stated both identifiers.

    The gate is deterministic, so a test that wants to open the call has to supply
    what the person actually said — the model's ``identity_confirmed`` alone is
    not enough and is not meant to be (see
    :func:`trail.agent.machine.identity_matches`). The CPF is given as bare
    digits, which is one of the spellings a transcript produces; the machine
    strips non-digits before comparing, so the punctuated form works equally and
    is worth a test of its own.
    """
    fields: dict[str, object] = {
        "identity_confirmed": True,
        "stated_name": profile.full_name,
        "stated_tax_id": profile.tax_id,
    }
    fields.update(overrides)
    return extraction(Step.VERIFY_RIGHT_PARTY, **fields)


def identity_turn_by_birth_date(
    profile: AccountProfile, **overrides: object
) -> TurnExtraction:
    """The §9 fallback: a customer who will not say a CPF over the phone.

    Declining to read a national identifier to an automated caller is good
    behaviour rather than evasive behaviour, so the date of birth substitutes for
    the CPF — never for the name, and never for both. The golden set exercises
    this shape too (``no_cpf_over_phone``); it is here so a machine test can reach
    it without the golden set.
    """
    fields: dict[str, object] = {
        "stated_tax_id": None,
        "stated_date_of_birth": profile.date_of_birth.isoformat(),
    }
    fields.update(overrides)
    return identity_turn(profile, **fields)


def consent_turn(*, given: bool = True, **overrides: object) -> TurnExtraction:
    """A ``disclose_and_consent`` turn. ``given=False`` is a transfer, not a retry."""
    fields: dict[str, object] = {"consent_given": given}
    fields.update(overrides)
    return extraction(Step.DISCLOSE_AND_CONSENT, **fields)


def balance_turn(**overrides: object) -> TurnExtraction:
    """A ``state_balance`` turn: the customer acknowledging figures read to them.

    Carries no field of its own, and that is the step's shape rather than an
    omission — ``state_balance`` asserts, it does not collect. A customer who
    takes the block's invitation to disagree is saying something the approved
    script cannot answer, which is ``needs_human`` and a transfer.
    """
    return extraction(Step.STATE_BALANCE, **overrides)


def terms_turn(profile: AccountProfile, **overrides: object) -> TurnExtraction:
    """A ``confirm_terms`` turn restating exactly what the agent rendered.

    The restatement is **captured, not judged**: whether it was correct rides on
    ``Turn.terms_correct``, from a separate model call, so a test can hand the
    machine a perfect restatement with a ``False`` verdict and watch the retry —
    which is what the ASR fairness question in BLUEPRINT §6 actually looks like
    from the machine's side.

    The values come from :func:`~trail.agent.machine.slots_for_call` so that
    "restated correctly" means the customer said the words the agent said, rather
    than words a test author formatted independently and hoped matched.
    """
    rendered = slots_for_call(profile)
    fields: dict[str, object] = {
        "restated_amount": rendered["balance"],
        "restated_date": rendered["due_date"],
    }
    fields.update(overrides)
    return extraction(Step.CONFIRM_TERMS, **fields)


def path_turn(
    path: PaymentPath | None = PaymentPath.PAY_NOW, **overrides: object
) -> TurnExtraction:
    """An ``offer_payment_path`` turn. ``None`` is the rule-2 callback.

    All four paths are worth the same to the machine, so which one is named
    changes nothing about routing — passing a different member here should move
    exactly one field on the record and nothing else.
    """
    fields: dict[str, object] = {"selected_path": path}
    fields.update(overrides)
    return extraction(Step.OFFER_PAYMENT_PATH, **fields)


def commitment_turn(
    *commitments: PaymentCommitment, **overrides: object
) -> TurnExtraction:
    """A ``capture_commitment`` turn carrying zero or more promises to pay."""
    fields: dict[str, object] = {"commitments": list(commitments)}
    fields.update(overrides)
    return extraction(Step.CAPTURE_COMMITMENT, **fields)


def contact_turn(
    *, confirmed: bool | None = True, **overrides: object
) -> TurnExtraction:
    """A ``confirm_contact`` turn. Anything but ``True`` is the rule-4 callback."""
    fields: dict[str, object] = {"contact_channel_confirmed": confirmed}
    fields.update(overrides)
    return extraction(Step.CONFIRM_CONTACT, **fields)


def complete_commitment(profile: AccountProfile) -> PaymentCommitment:
    """A commitment row with nothing left for a specialist to phone about.

    Amount and date both present, which is all rule 3 reads. It does not read
    *how much*: this row and one promising a tenth of it are the same row to the
    machine, and that is the property
    :func:`trail.agent.machine._capture_commitment` exists to preserve.
    """
    rendered = slots_for_call(profile)
    return PaymentCommitment(
        amount=rendered["balance"],
        date=rendered["due_date"],
        method=PaymentPath.PAY_NOW,
        source_utterance=(
            f"posso pagar {rendered['balance']} no dia {rendered['due_date']}"
        ),
    )


def partial_commitment(amount: str = "mil e duzentos") -> PaymentCommitment:
    """An amount the customer named without a day. Rule 3's callback.

    The amount is spelled out because that is what a transcript produces, and
    because the agent stores it exactly as said: "mil e duzentos" is not turned
    into ``1200`` anywhere on the capture path. Only the eval scorer parses it.
    """
    return PaymentCommitment(
        amount=amount,
        date=None,
        source_utterance=f"acho que consigo uns {amount}, mas ainda não sei o dia",
    )


def dispute_row(subject: str = "o valor", detail: str | None = "já paguei") -> Dispute:
    """Something the customer said about the debt, recorded and never characterised.

    A :class:`~trail.models.Dispute` triggers no callback of its own and no
    special routing. It is also, on the turn it appears, something the approved
    script cannot answer — so the same turn normally carries ``needs_human`` and
    transfers, and ``machine._listen`` writes the row *before* it takes that exit
    so the specialist reads the words that caused it.
    """
    return Dispute(
        subject=subject,
        detail=detail,
        source_utterance=f"esse {subject} está errado, {detail}",
    )


# ---------------------------------------------------------------------------
# The mocked system of record
# ---------------------------------------------------------------------------


@dataclass
class RecordStore:
    """An in-memory stand-in for the ``call_records`` table.

    Exists so a test can ask the two questions the real table is shaped to
    answer without a database: *did this call land a record?* and *what does the
    specialist queue look like?*
    """

    records: dict[UUID, CallRecord] = field(default_factory=dict)

    def add(self, record: CallRecord) -> None:
        self.records[record.call_id] = record

    def get(self, call_id: UUID) -> CallRecord | None:
        return self.records.get(call_id)

    def specialist_queue(self) -> list[CallRecord]:
        """Unreviewed records, oldest first.

        One queue, one ordering key, no filtering by terminal state and no
        risk-based ordering — the shape ``call_records_specialist_queue_idx``
        enforces in the schema and BLUEPRINT §7 requires of the design. Sorting
        this list by balance would be a one-line change here and the whole
        argument lost; there is nothing on a :class:`~trail.models.CallRecord`
        to sort it by, and that absence is the guarantee.
        """
        return sorted(
            (record for record in self.records.values() if record.reviewed_at is None),
            key=lambda record: record.started_at,
        )


@pytest.fixture
def record_store() -> RecordStore:
    """A fresh in-memory record store."""
    return RecordStore()


# ---------------------------------------------------------------------------
# The fake agent
# ---------------------------------------------------------------------------

_TURNS_PATH = re.compile(r"^/calls/(?P<call_id>[0-9a-fA-F-]{36})/turns$")
_UNREACHABLE_PATH = re.compile(r"^/calls/(?P<call_id>[0-9a-fA-F-]{36})/unreachable$")
_CALL_PATH = re.compile(r"^/calls/(?P<call_id>[0-9a-fA-F-]{36})$")

#: Post-processes the record the stub would otherwise return, so a test can
#: degrade a perfect agent one field at a time.
AgentPolicy = Callable[[SyntheticCase, CallRecord], CallRecord]


@dataclass
class _StubSession:
    call_id: UUID
    case: SyntheticCase
    started_at: datetime
    turns_received: int = 0
    finished: bool = False


@dataclass
class FakeAgent:
    """An agent that satisfies the HTTP contract without a model or a database.

    By default it is a *perfect* agent: every record it returns is exactly what
    the case's :class:`~trail.models.CaseExpectation` declares, and every word
    it speaks is approved protocol text — verbatim where the block is verbatim,
    and rendered from the case's own :class:`~trail.models.AccountProfile`
    where the block is slotted. A run against it is therefore the harness
    measuring itself, and the expected result is the golden set's own arithmetic
    — 6 of 15 fully automated, no findings, no compliance violations. ``policy``
    is how a test breaks exactly one thing and watches the scorecard move.

    The four failure knobs are the four ways ``runner.py`` promises never to
    raise: an agent that is down (``healthy``), one that errors on a turn
    (``turn_status``), one that finishes without a record
    (``omit_record_on_finish``), and one that talks past the end of the script
    (``never_finish``). Each is used by exactly one test.

    Simplifications, stated so nobody mistakes this for the real machine: it
    speaks one approved block per turn in declaration order and clamps at the
    last step rather than modelling a terms-restatement retry, and it decides
    when to end from the expected terminal state rather than from what the
    customer said.
    """

    protocol: Protocol
    cases: Mapping[str, SyntheticCase]
    store: RecordStore
    policy: AgentPolicy | None = None
    healthy: bool = True
    omit_record_on_finish: bool = False
    turn_status: int | None = None
    never_finish: bool = False

    requests: list[tuple[str, str]] = field(default_factory=list)
    sessions: dict[UUID, _StubSession] = field(default_factory=dict)

    @property
    def transport(self) -> httpx.MockTransport:
        """A transport bound to this stub, for injecting into a client it builds."""
        return httpx.MockTransport(self.handle)

    # -- HTTP ---------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Route one request. The signature :class:`httpx.MockTransport` wants."""
        method, path = request.method, request.url.path
        self.requests.append((method, path))

        if method == "GET" and path == "/healthz":
            if not self.healthy:
                return httpx.Response(503, json={"detail": "agent is starting"})
            return httpx.Response(200, json={"status": "ok"})

        if method == "POST" and path == "/calls":
            return self._start_call(request)

        match = _TURNS_PATH.match(path)
        if method == "POST" and match:
            return self._submit_turn(request, UUID(match["call_id"]))

        match = _UNREACHABLE_PATH.match(path)
        if method == "POST" and match:
            return self._mark_unreachable(request, UUID(match["call_id"]))

        match = _CALL_PATH.match(path)
        if method == "GET" and match:
            record = self.store.get(UUID(match["call_id"]))
            if record is None:
                return httpx.Response(404, json={"detail": "no finished record"})
            return httpx.Response(200, json=record.model_dump(mode="json"))

        return httpx.Response(404, json={"detail": f"no route for {method} {path}"})

    # -- Endpoints ----------------------------------------------------------

    def _say(self, session: _StubSession, step: Step) -> str:
        """The approved utterance for ``step``, rendered from this case's account."""
        return approved_utterance(self.protocol, step, session.case.profile)

    def _start_call(self, request: httpx.Request) -> httpx.Response:
        body = StartCallRequest.model_validate(json.loads(request.content))
        if body.case_id is None or body.case_id not in self.cases:
            return httpx.Response(
                422, json={"detail": f"unknown case_id {body.case_id!r}"}
            )

        session = _StubSession(
            call_id=uuid4(),
            case=self.cases[body.case_id],
            started_at=datetime.now(timezone.utc),
        )
        self.sessions[session.call_id] = session
        return httpx.Response(
            201,
            json=StartCallResponse(
                call_id=session.call_id,
                step=Step.VERIFY_RIGHT_PARTY,
                agent_utterance=self._say(session, Step.VERIFY_RIGHT_PARTY),
            ).model_dump(mode="json"),
        )

    def _submit_turn(self, request: httpx.Request, call_id: UUID) -> httpx.Response:
        if self.turn_status is not None:
            return httpx.Response(
                self.turn_status, json={"detail": "injected agent failure"}
            )

        body = TurnRequest.model_validate(json.loads(request.content))
        if body.call_id != call_id:
            return httpx.Response(400, json={"detail": "call_id mismatch"})

        session = self.sessions.get(call_id)
        if session is None:
            return httpx.Response(404, json={"detail": f"unknown call_id {call_id}"})
        if session.finished:
            return httpx.Response(409, json={"detail": "call has already finished"})

        session.turns_received += 1
        terminal = session.case.expectation.expected_terminal_state
        finish_at = _EARLY_FINISH_TURN.get(terminal, len(session.case.scripted_turns))

        if self.never_finish or session.turns_received < finish_at:
            step = _STEPS[min(session.turns_received, len(_STEPS) - 1)]
            return httpx.Response(
                200,
                json=TurnResponse(
                    call_id=call_id,
                    step=step,
                    agent_utterance=self._say(session, step),
                    finished=False,
                ).model_dump(mode="json"),
            )

        session.finished = True
        record = self._build_record(session, terminal)
        closing = _CLOSING_UTTERANCE.get(terminal)
        step = (
            _STEPS[max(session.turns_received - 1, 0)]
            if closing is not None
            else Step.POST_OUTCOME
        )
        return httpx.Response(
            200,
            json=TurnResponse(
                call_id=call_id,
                step=step,
                agent_utterance=closing or self._say(session, Step.POST_OUTCOME),
                finished=True,
                terminal_state=terminal,
                record=None if self.omit_record_on_finish else record,
            ).model_dump(mode="json"),
        )

    def _mark_unreachable(
        self, request: httpx.Request, call_id: UUID
    ) -> httpx.Response:
        body = MarkUnreachableRequest.model_validate(json.loads(request.content))
        if body.call_id != call_id:
            return httpx.Response(400, json={"detail": "call_id mismatch"})

        session = self.sessions.get(call_id)
        if session is None:
            return httpx.Response(404, json={"detail": f"unknown call_id {call_id}"})
        if session.finished:
            return httpx.Response(409, json={"detail": "call has already finished"})

        session.finished = True
        record = self._build_record(session, TerminalState.NOT_REACHED)
        return httpx.Response(200, json=record.model_dump(mode="json"))

    # -- Records ------------------------------------------------------------

    def _build_record(
        self, session: _StubSession, terminal: TerminalState
    ) -> CallRecord:
        """Exactly what the expectation declares, and not one field more.

        ``selected_path`` is left null even on a completed call, because
        :class:`~trail.models.CaseExpectation` does not pin it and inventing a
        value here would be the stub asserting something the golden set never
        agreed to. The scorer does not read it either — which is the point:
        anything this stub fills in that the fixture did not declare is a place
        the harness could agree with itself and call it a measurement.
        """
        expectation = session.case.expectation
        conversed = terminal not in {
            TerminalState.NOT_RIGHT_PARTY,
            TerminalState.NOT_REACHED,
        }
        ended_at = datetime.now(timezone.utc)
        record = CallRecord(
            call_id=session.call_id,
            account_id=session.case.profile.account_id,
            started_at=session.started_at,
            ended_at=ended_at,
            terminal_state=terminal,
            commitments=(list(expectation.expected_commitments) if conversed else []),
            disputes=list(expectation.expected_disputes) if conversed else [],
            contact_channel_confirmed=(
                expectation.expected_contact_channel if conversed else None
            ),
            consent_given=(
                terminal is not TerminalState.TRANSFERRED_TO_HUMAN
                if conversed
                else None
            ),
            terms_confirmed=(
                expectation.expected_terms_confirmed if conversed else None
            ),
            protocol_version=self.protocol.version,
            prompt_version=FAKE_PROMPT_VERSION,
            model=FAKE_MODEL,
            total_input_tokens=1_000,
            total_output_tokens=100,
            cost_usd=FAKE_COST_PER_CALL_USD,
            wall_seconds=(ended_at - session.started_at).total_seconds(),
        )
        if self.policy is not None:
            record = self.policy(session.case, record)
        self.store.add(record)
        return record


#: What ``make_agent`` hands back: a client wired to the stub, and the stub.
AgentHarness = tuple[httpx.AsyncClient, FakeAgent]

#: The ``make_agent`` fixture itself. Test modules import this for annotation
#: (``from conftest import MakeAgent``), which pytest makes possible by putting
#: ``tests/`` on ``sys.path`` when it loads this file.
MakeAgent = Callable[..., AgentHarness]


@pytest.fixture
async def make_agent(
    real_protocol: Protocol, record_store: RecordStore
) -> AsyncIterator[Callable[..., AgentHarness]]:
    """Build an :class:`httpx.AsyncClient` wired to a :class:`FakeAgent`.

    The stub speaks the *real* protocol, because the phrases a golden-set case
    forbids are checked against what the agent actually said — and a stub
    reading placeholder text would make every ``must_not_contain`` assertion
    vacuous. That matters more here than it did one industry over: the
    ``wrong_party`` case forbids the balance and the product, and both of those
    reach a customer only through a rendered slot, so a stub that spoke the raw
    template would satisfy the assertion by saying ``{balance}``.
    """
    opened: list[httpx.AsyncClient] = []

    def make(
        *,
        cases: Sequence[SyntheticCase] = GOLDEN_SET,
        policy: AgentPolicy | None = None,
        healthy: bool = True,
        omit_record_on_finish: bool = False,
        turn_status: int | None = None,
        never_finish: bool = False,
    ) -> AgentHarness:
        agent = FakeAgent(
            protocol=real_protocol,
            cases={case.case_id: case for case in cases},
            store=record_store,
            policy=policy,
            healthy=healthy,
            omit_record_on_finish=omit_record_on_finish,
            turn_status=turn_status,
            never_finish=never_finish,
        )
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(agent.handle),
            base_url=FAKE_AGENT_BASE_URL,
        )
        opened.append(client)
        return client, agent

    yield make
    for client in opened:
        await client.aclose()
