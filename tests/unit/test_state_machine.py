"""The conversation state machine, branch by branch.

Read as documentation, this file answers three questions a reviewer of a
debt-collection system asks first: *where can this call end up?*, *what does the
agent say on the way?*, and *can it ever decide something about the customer?*
The answers are, respectively: five terminal states and nothing else; approved
text — rendered where the block is slotted, verbatim everywhere else — plus
three administrative sentences; and no.

The third answer is the one this port had to work for. Collections has to speak
a customer-specific amount out loud, so the agent renders one block from the
system of record, and every rule that decides where a call goes still reads
either a boolean or a field for ``None``. Not one of them reads *how much* was
promised, *which* payment path was chosen, or *what* the customer disputed.

Nothing here needs a network, a database or an API key. That is a property of
``machine.py`` — it takes a session and one extraction and returns what the
agent says next — and it is the reason these branches can be exercised
exhaustively instead of sampled.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from tests.conftest import (
    DrivenCall,
    approved_utterance,
    balance_turn,
    commitment_turn,
    complete_commitment,
    consent_turn,
    contact_turn,
    dispute_row,
    extraction,
    identity_turn,
    identity_turn_by_birth_date,
    partial_commitment,
    path_turn,
    terms_turn,
)
from trail.agent import machine
from trail.agent.compliance import check_outbound_utterance
from trail.agent.machine import (
    IDENTITY_REPROMPT_UTTERANCE,
    LISTENING_STEPS,
    NOT_RIGHT_PARTY_UTTERANCE,
    TRANSFER_TO_HUMAN_UTTERANCE,
    Turn,
    build_record,
    slots_for_call,
    usage,
)
from trail.models import (
    AccountProfile,
    LLMCallTrace,
    PaymentCommitment,
    PaymentPath,
    Step,
    TerminalState,
    TurnExtraction,
    next_step,
)
from trail.protocol import Protocol

pytestmark = pytest.mark.unit

PROMPT_VERSION = "test-prompt.0"
MODEL = "gpt-5.6-luna"

#: A second synthetic CPF, checksum-valid and belonging to nobody. It is a real
#: number in the arithmetic sense and the wrong number in the account sense,
#: which is the only way to test that the gate compares rather than validates.
ANOTHER_VALID_CPF = "11144477735"


def _needs_human_turn(step: Step, utterance: str) -> TurnExtraction:
    """A turn the approved script cannot answer, built with the words that caused it.

    Constructed field by field rather than through the shared builder because the
    *content* is the subject here: two customers who said very different things
    must produce the same routing, and the only way to show that is to let them
    say different things (CONTRACT §7).
    """
    return TurnExtraction(
        step=step,
        raw_utterance=utterance,
        understood=True,
        needs_human=True,
    )


# ---------------------------------------------------------------------------
# Opening
# ---------------------------------------------------------------------------


def test_a_new_call_opens_by_asking_who_is_on_the_line(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    session = drive(sample_profile)
    outcome = session.opening

    assert outcome.step is Step.VERIFY_RIGHT_PARTY
    assert outcome.agent_utterance == fake_protocol.text_for(Step.VERIFY_RIGHT_PARTY)
    assert outcome.finished is False
    assert outcome.terminal_state is None
    assert session.state.identity_confirmed is False
    assert session.state.finished is False


def test_the_steps_run_in_declaration_order_and_end_at_the_closing_statement() -> None:
    """Declaration order *is* conversation order, and the last step is terminal."""
    ordered = list(Step)
    assert [next_step(step) for step in ordered[:-1]] == ordered[1:]
    assert next_step(ordered[-1]) is None
    assert ordered[-1] is Step.POST_OUTCOME


def test_the_agent_listens_at_every_step_but_the_closing_statement() -> None:
    """Seven rules, one per step that waits for a reply, and no eighth.

    ``post_outcome`` is spoken and the call ends on the same turn, so it has no
    rule and no chance to read anything the customer said. Every other step has
    exactly one rule, and each of those seven is exercised below.
    """
    assert set(machine.RULES) == set(LISTENING_STEPS)
    assert len(machine.RULES) == 7
    assert Step.POST_OUTCOME not in LISTENING_STEPS
    assert set(LISTENING_STEPS) | {Step.POST_OUTCOME} == set(Step)


# ---------------------------------------------------------------------------
# verify_right_party — the hard gate
# ---------------------------------------------------------------------------


def test_a_confirmed_identity_opens_the_disclosure(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    session = drive(sample_profile, step=Step.VERIFY_RIGHT_PARTY)

    outcome = session.advance(identity_turn(sample_profile))

    assert outcome.step is Step.DISCLOSE_AND_CONSENT
    assert outcome.agent_utterance == fake_protocol.text_for(Step.DISCLOSE_AND_CONSENT)
    assert outcome.finished is False
    assert session.state.identity_confirmed is True


def test_an_identity_that_was_answered_and_did_not_match_ends_the_call(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """Both identifiers given, one of them somebody else's. That is an answer."""
    session = drive(sample_profile, step=Step.VERIFY_RIGHT_PARTY)

    outcome = session.advance(
        identity_turn(sample_profile, stated_name="Fernanda Moreira Lima"),
    )

    assert outcome.terminal_state is TerminalState.NOT_RIGHT_PARTY
    assert outcome.finished is True
    assert outcome.agent_utterance == NOT_RIGHT_PARTY_UTTERANCE
    assert session.state.identity_confirmed is False
    assert session.state.ended_at is not None


def test_a_half_answered_identity_is_asked_again_rather_than_hung_up_on(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """The bug a real person found in the first minute of using this.

    Asked for a name *and* a CPF, people routinely give the name and stop —
    more so here than in the healthcare system this gate is ported from, because
    the second identifier is a national one and hesitating before reading it out
    to an automated caller is good behaviour. That is not evidence of a wrong
    party: it is somebody who has not finished answering, and hanging up on them
    both fails a recoverable call and records it in the denominator as
    unautomatable.

    The distinction the machine draws is *absent* versus *present and wrong*: an
    identifier that was never stated earns one more turn, an identifier that was
    stated and did not match does not.
    """
    session = drive(sample_profile, step=Step.VERIFY_RIGHT_PARTY)

    first = session.advance(identity_turn(sample_profile, stated_tax_id=None))

    assert first.finished is False
    assert first.terminal_state is None
    assert first.step is Step.VERIFY_RIGHT_PARTY
    assert first.agent_utterance == IDENTITY_REPROMPT_UTTERANCE
    assert session.state.identity_confirmed is False

    # The reply to the reprompt answers only what was missing, because that is
    # what people say when asked for the one thing they left out. It is combined
    # with the name from the first turn rather than judged on its own — a
    # reprompt whose answer cannot be combined with what was already said is a
    # reprompt that can never succeed.
    second = session.advance(
        identity_turn(sample_profile, stated_name=None, identity_confirmed=None),
    )

    assert second.step is Step.DISCLOSE_AND_CONSENT
    assert session.state.identity_confirmed is True
    assert session.state.stated_name == sample_profile.full_name
    assert session.state.stated_tax_id == sample_profile.tax_id


def test_a_caller_who_disclaims_being_the_customer_is_refused_despite_a_match(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """ "Sou o marido dela — ela é Marina Rocha Santos, CPF 529.982.247-25."

    Both identifiers match the account, and the caller has just said they are
    somebody else. The deterministic comparison cannot see that; the extraction
    can. This is why ``identity_confirmed`` survives as a veto after it stopped
    being a permission — it carries the one signal the comparison has no access
    to, and dropping it entirely to make accumulation work would have traded a
    real check for a convenience. Speaking about the debt to the spouse is a
    direct FDCPA third-party disclosure however cooperative they sound.
    """
    session = drive(sample_profile, step=Step.VERIFY_RIGHT_PARTY)

    outcome = session.advance(
        identity_turn(sample_profile, identity_confirmed=False),
    )

    assert outcome.terminal_state is TerminalState.NOT_RIGHT_PARTY
    assert session.state.identity_confirmed is False


def test_a_null_identity_verdict_is_not_a_denial(
    sample_profile: AccountProfile,
) -> None:
    """The veto's asymmetry, stated directly on the gate.

    ``False`` closes it; ``None`` does not open it and does not close it either —
    it is the absence of the signal rather than its negation, and it is exactly
    what an identity accumulated over two turns looks like. Requiring ``True``
    would make a two-turn identity permanently unprovable.
    """
    assert machine.identity_matches(sample_profile, identity_turn(sample_profile))
    assert machine.identity_matches(
        sample_profile, identity_turn(sample_profile, identity_confirmed=None)
    )
    assert not machine.identity_matches(
        sample_profile, identity_turn(sample_profile, identity_confirmed=False)
    )


def test_a_second_half_answer_fails_closed_like_any_other_unproven_identity(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """The retry is one extra turn, not an open invitation.

    This is the assertion that keeps the reprompt from becoming a hole: the gate
    still fails closed, it simply does so one turn later.
    """
    session = drive(sample_profile, step=Step.VERIFY_RIGHT_PARTY)
    half_answer = identity_turn(sample_profile, stated_tax_id=None)

    session.advance(half_answer)
    outcome = session.advance(half_answer)

    assert outcome.terminal_state is TerminalState.NOT_RIGHT_PARTY
    assert outcome.finished is True
    assert outcome.agent_utterance == NOT_RIGHT_PARTY_UTTERANCE
    assert session.state.identity_confirmed is False


def test_the_identity_reprompt_discloses_nothing(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
    slots: dict[str, str],
) -> None:
    """It is spoken before identity is proven, so it is held to that standard.

    Screened the way the service screens a turn utterance — with the slot
    mapping supplied, because that is what ``app._screen`` does — so this is the
    running configuration and not a friendlier one.
    """
    session = drive(sample_profile, step=Step.VERIFY_RIGHT_PARTY)

    outcome = session.advance(identity_turn(sample_profile, stated_tax_id=None))

    result = check_outbound_utterance(
        outcome.agent_utterance,
        fake_protocol,
        profile=sample_profile,
        slots=slots,
        identity_confirmed=False,
        prior_utterances=session.state.agent_transcript,
    )

    assert list(result.violations) == []


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            {"stated_name": "Fernanda Moreira Lima"},
            id="somebody-elses-name",
        ),
        pytest.param(
            {"identity_confirmed": None, "stated_name": "Fernanda Moreira Lima"},
            id="somebody-elses-name-and-no-verdict",
        ),
        pytest.param(
            {"identity_confirmed": True, "stated_name": "Fernanda Moreira Lima"},
            id="somebody-elses-name-with-the-verdict-talked-into-yes",
        ),
        pytest.param(
            {"stated_tax_id": "52998224726"},
            id="a-cpf-one-digit-out",
        ),
        pytest.param(
            {"stated_tax_id": ANOTHER_VALID_CPF},
            id="a-valid-cpf-that-belongs-to-another-account",
        ),
        pytest.param(
            {"stated_tax_id": "5299822"},
            id="a-cpf-the-transcript-cut-short",
        ),
        pytest.param(
            {"stated_tax_id": None, "stated_date_of_birth": "1984-03-10"},
            id="a-date-of-birth-one-day-out",
        ),
        pytest.param(
            {
                "stated_tax_id": None,
                "stated_date_of_birth": "nove de março de oitenta e quatro",
            },
            id="a-date-of-birth-that-does-not-parse",
        ),
    ],
)
def test_the_identity_gate_fails_closed_in_every_direction(
    drive: Callable[..., DrivenCall],
    overrides: dict[str, object],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """The gate is deterministic, and a boolean on its own does not open it.

    ``identity_confirmed`` is the model's opinion, formed from a prompt with the
    caller's own words interpolated into it — so it corroborates and it does not
    decide. What decides is a family-name match plus an exact CPF that passes its
    own check digits, or an exact date of birth where no CPF was offered, in
    ``machine.identity_matches``. A wrong CPF and a name that does not match are
    the wrong party on the first turn, because third-party disclosure of a debt
    is a direct FDCPA violation and BLUEPRINT §5's first zero-tolerance failure.

    Every case here **answered** the question and answered it wrongly. An
    identifier that was never stated is a different situation and gets one more
    turn — see
    :func:`test_a_half_answered_identity_is_asked_again_rather_than_hung_up_on`.
    That is a distinction about whether the caller has finished speaking, not a
    softening of the gate: the third case is an extraction talked into
    ``identity_confirmed: true``, and it still opens nothing.
    """
    session = drive(sample_profile, step=Step.VERIFY_RIGHT_PARTY)

    outcome = session.advance(identity_turn(sample_profile, **overrides))

    assert outcome.terminal_state is TerminalState.NOT_RIGHT_PARTY
    assert session.state.identity_confirmed is False


def test_a_nickname_with_the_right_family_name_and_cpf_is_the_right_party(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """ "Mari Santos — Marina, no documento."

    The comparison is on the family names, not the whole name, because given
    names are nicknamed constantly and turning that into a wrong-party
    termination would fail real customers at the first sentence. The CPF is the
    half that has to be exact.
    """
    session = drive(sample_profile, step=Step.VERIFY_RIGHT_PARTY)

    outcome = session.advance(identity_turn(sample_profile, stated_name="Mari Santos"))

    assert outcome.step is Step.DISCLOSE_AND_CONSENT
    assert session.state.identity_confirmed is True


def test_a_customer_who_will_not_say_a_cpf_is_verified_by_date_of_birth(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """The §9 fallback, and the customers it exists for.

    Declining to read a national identifier to an automated caller is not evasive
    behaviour; in Brazil it is good behaviour, and a gate that treated it as a
    failure would filter out the most security-literate people in the book. The
    date of birth substitutes for the CPF — never for the family name, and never
    for both.
    """
    session = drive(sample_profile, step=Step.VERIFY_RIGHT_PARTY)

    outcome = session.advance(identity_turn_by_birth_date(sample_profile))

    assert outcome.step is Step.DISCLOSE_AND_CONSENT
    assert session.state.identity_confirmed is True


def test_a_punctuated_cpf_is_the_same_cpf(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """A transcript spells a CPF both ways and the caller said one number.

    The machine strips everything but digits before comparing, which is a
    normalisation that cannot change *which* identifier was meant — the test
    every normalisation in this system has to pass.
    """
    session = drive(sample_profile, step=Step.VERIFY_RIGHT_PARTY)

    outcome = session.advance(
        identity_turn(sample_profile, stated_tax_id="529.982.247-25"),
    )

    assert outcome.step is Step.DISCLOSE_AND_CONSENT
    assert session.state.identity_confirmed is True


def test_a_stated_cpf_that_does_not_match_is_not_repaired_by_a_correct_birth_date(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """The fallback is a fallback, not a second chance.

    A stated identifier is a claim, and a wrong claim is a stronger signal than a
    missing one. The date of birth substitutes only where no CPF was offered at
    all; offering the wrong one closes the gate whatever else is right.
    """
    session = drive(sample_profile, step=Step.VERIFY_RIGHT_PARTY)

    outcome = session.advance(
        identity_turn(
            sample_profile,
            stated_tax_id=ANOTHER_VALID_CPF,
            stated_date_of_birth=sample_profile.date_of_birth.isoformat(),
        ),
    )

    assert outcome.terminal_state is TerminalState.NOT_RIGHT_PARTY


def test_a_cpf_that_fails_its_own_check_digits_is_refused(
    make_profile: Callable[..., AccountProfile],
) -> None:
    """Why ``is_valid_cpf`` is a separate condition and not an optimisation.

    The equality test alone would already reject somebody else's number. Running
    the checksum as well is what distinguishes "this is another person's CPF"
    from "this is not a CPF" — and only the second says the line is being guessed
    at. The account here is malformed and the caller repeats it back perfectly;
    the gate still refuses, because a number that fails its own arithmetic
    identifies nobody.
    """
    profile = make_profile(tax_id="12345678900")

    assert not machine.identity_matches(profile, identity_turn(profile))


def test_a_repeated_digit_cpf_is_refused(
    make_profile: Callable[..., AccountProfile],
) -> None:
    """111.111.111-11 satisfies the check-digit arithmetic and is not a CPF.

    It is also exactly what a caller produces when inventing one, which is why
    the rejection is explicit rather than left to the checksum.
    """
    profile = make_profile(tax_id="11111111111")

    assert not machine.identity_matches(profile, identity_turn(profile))


def test_an_identity_missing_either_half_is_not_an_identity(
    sample_profile: AccountProfile,
) -> None:
    """Three conjuncts, and the gate needs a name plus one of the two identifiers.

    A family name on its own proves nothing — both second identifiers absent is a
    fail — and a CPF with no name is not an identity either, because the number
    could have been read off a letter by whoever opened it.
    """
    assert not machine.identity_matches(
        sample_profile,
        identity_turn(sample_profile, stated_tax_id=None),
    )
    assert not machine.identity_matches(
        sample_profile,
        identity_turn(sample_profile, stated_name=None),
    )


def test_the_wrong_party_gate_does_not_degrade_into_a_partial_conversation(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """No balance, no consent, no commitment, no message left with whoever answered."""
    session = drive(sample_profile, step=Step.VERIFY_RIGHT_PARTY)

    session.advance(identity_turn(sample_profile, stated_name="Fernanda Moreira Lima"))
    record = build_record(
        session.state, fake_protocol, prompt_version=PROMPT_VERSION, model=MODEL
    )

    assert record.consent_given is None
    assert record.terms_confirmed is None
    assert record.contact_channel_confirmed is None
    assert record.selected_path is None
    assert record.commitments == []
    assert record.disputes == []


# ---------------------------------------------------------------------------
# _family_name_matches — the one rule the port had to loosen, and its floor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stated",
    [
        pytest.param("Marina Rocha da Silva Santos", id="the-whole-booked-name"),
        pytest.param("Marina Rocha", id="the-maternal-family-name"),
        pytest.param("Marina Santos", id="the-paternal-family-name"),
        pytest.param("Marina Silva", id="the-middle-family-name"),
        pytest.param("Mari Santos", id="a-nickname-plus-a-family-name"),
        pytest.param("marina rocha", id="lower-cased-by-the-transcript"),
    ],
)
def test_any_booked_family_name_identifies_the_same_person(stated: str) -> None:
    """Brazilian names carry two family names and people use either one.

    "Marina Rocha da Silva Santos" answers the phone as "Marina Rocha", "Marina
    Santos" or the whole thing depending on the day. Demanding the final token —
    which is what the healthcare original did, correctly, for names shaped
    given-name-plus-one-surname — would have sent a large, systematic and
    entirely legitimate slice of customers to ``not_right_party``, hardest of all
    to the people with the longest names.
    """
    assert machine._family_name_matches("Marina Rocha da Silva Santos", stated)


def test_a_shared_given_name_is_not_a_family_name_match() -> None:
    """The given name is dropped, and that is the half doing the work.

    "Maria" and "José" are among the most common given names in Brazil, so a
    wrong party who happens to share one — a spouse, a parent, an adult child —
    would clear the name half of the gate on nothing at all.
    """
    assert not machine._family_name_matches("Marina Rocha Santos", "Marina Oliveira")
    assert not machine._family_name_matches("Marina Rocha Santos", "Marina")


def test_name_particles_are_not_family_names() -> None:
    """ "da" appearing in both names proves nothing about identity.

    It is dropped by name rather than by length, because that is the correct
    instrument: "da" is not short, it is not a name.
    """
    assert not machine._family_name_matches("Marina da Silva", "João da Costa")
    assert not machine._family_name_matches("Ana dos Santos", "Pedro dos Reis")


def test_accents_are_folded_because_a_transcript_spells_a_name_both_ways() -> None:
    """ "João Sá" and "Joao Sa" are the same customer and two ASR outputs.

    Folding cannot change which name was meant, so it is safe; a gate that
    treated the two as different people would fail closed on a correct answer.
    """
    assert machine._family_name_matches("João Sá", "Joao Sa")
    assert machine._family_name_matches("João Sá", "joão sá")


def test_a_two_letter_family_name_matches_a_token_and_never_inside_a_word() -> None:
    """The floor is two characters, and it is safe only because this is not a search.

    "Sá", "Luz" and "Reis" are real Brazilian family names and the healthcare
    floor of four would have refused them. Lowering it is safe here for a reason
    that did not hold in the original: the comparison is token equality against a
    tokenised name, never a substring search, so a two-letter family name cannot
    match inside a longer word — only a caller who said that exact token.
    """
    assert machine._family_name_matches("Ana Sá", "Ana Sá")
    assert not machine._family_name_matches("Ana Sá", "Ana Sales")
    assert not machine._family_name_matches("Ana Sá", "Ana Saraiva")


def test_a_single_token_booked_name_is_compared_whole() -> None:
    """With one token there is no family name to distinguish from the given one.

    Comparing the whole thing is the honest reading; guessing which half it is
    would be inventing a fact about a person's name.
    """
    assert machine._family_name_matches("Madonna", "Madonna")
    assert not machine._family_name_matches("Madonna", "Prince")


# ---------------------------------------------------------------------------
# slots_for_call — the one customer-specific utterance
# ---------------------------------------------------------------------------


def test_slots_for_call_supplies_exactly_the_slots_the_block_declares(
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
    real_protocol: Protocol,
) -> None:
    """Four names, matched in both directions, against both protocol files.

    :meth:`~trail.protocol.Protocol.render` requires the declared and supplied
    sets to be equal, so a block edited without a change here — or the reverse —
    raises inside the graph at the first call that reaches step three rather than
    speaking a brace. Holding the fixture protocol to the same contract as the
    shipped one is what keeps the state-machine suite from passing against a
    template the agent could never render.
    """
    supplied = frozenset(slots_for_call(sample_profile))

    assert supplied == {"product", "balance", "due_date", "days_past_due"}
    assert supplied == real_protocol.slots_for(Step.STATE_BALANCE)
    assert supplied == fake_protocol.slots_for(Step.STATE_BALANCE)
    assert not any(
        real_protocol.slots_for(step) for step in Step if step is not Step.STATE_BALANCE
    )


def test_every_slot_value_comes_out_of_the_account_record(
    sample_profile: AccountProfile,
) -> None:
    """Deterministic formatters over record fields, and nothing else.

    No model output, no customer utterance, no clock and no environment — which
    is what makes the rendered sentence reproducible by a compliance reviewer
    holding only the record and the protocol file.
    """
    assert slots_for_call(sample_profile) == {
        "product": "empréstimo pessoal",
        "balance": "R$ 847,32",
        "due_date": "3 de agosto de 2026",
        "days_past_due": "12 dias",
    }
    assert slots_for_call(sample_profile) == slots_for_call(sample_profile)


def test_the_balance_is_spoken_rendered_and_never_as_a_template(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
    slots: dict[str, str],
) -> None:
    """``_say`` renders the slotted block; reaching for ``text_for`` would not."""
    session = drive(sample_profile, step=Step.STATE_BALANCE)
    spoken = session.opening.agent_utterance

    assert spoken == fake_protocol.render(Step.STATE_BALANCE, slots)
    assert spoken != fake_protocol.text_for(Step.STATE_BALANCE)
    assert "{balance}" not in spoken
    assert slots["balance"] in spoken
    assert slots["due_date"] in spoken


def test_what_the_agent_says_at_state_balance_is_what_the_allowlist_approves(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
    slots: dict[str, str],
) -> None:
    """One function, called twice, cannot disagree with itself.

    The agent renders the utterance it is about to speak and
    ``assert_agent_text_is_approved`` renders the same block to decide whether
    that utterance is approved — both through
    :func:`~trail.agent.machine.slots_for_call`. If the two ever built the
    dictionary differently, the allowlist would refuse the agent's own approved
    text and the failure would arrive as a mystery compliance violation rather
    than as a diff.
    """
    session = drive(sample_profile, step=Step.STATE_BALANCE)

    result = check_outbound_utterance(
        session.opening.agent_utterance,
        fake_protocol,
        profile=sample_profile,
        slots=slots,
        identity_confirmed=True,
    )

    assert list(result.violations) == []


def test_the_rendered_balance_is_approved_for_this_account_and_no_other(
    drive: Callable[..., DrivenCall],
    make_profile: Callable[..., AccountProfile],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """The slot mapping is per call, and the gate is per call with it.

    Screening one customer's correctly rendered utterance against another
    customer's slots must fail — the approved set is built from the record, so an
    amount that disagrees with the record matches nothing in it. This is the same
    machinery that refuses a hallucinated figure, exercised from the direction
    that is easy to get wrong: both utterances are rendered, both are grammatical
    approved text, and only one of them is true of this account.
    """
    other = make_profile(balance_brl=Decimal("1200.50"))
    session = drive(sample_profile, step=Step.STATE_BALANCE)

    result = check_outbound_utterance(
        session.opening.agent_utterance,
        fake_protocol,
        profile=other,
        slots=slots_for_call(other),
        identity_confirmed=True,
    )

    assert not result.passed


# ---------------------------------------------------------------------------
# disclose_and_consent
# ---------------------------------------------------------------------------


def test_granted_consent_opens_the_balance(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
    slots: dict[str, str],
) -> None:
    session = drive(sample_profile, step=Step.DISCLOSE_AND_CONSENT)

    outcome = session.advance(consent_turn(given=True))

    assert outcome.step is Step.STATE_BALANCE
    assert outcome.agent_utterance == fake_protocol.render(Step.STATE_BALANCE, slots)
    assert session.state.consent_given is True


def test_refused_consent_hands_the_call_to_a_person(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """Consent that can be talked past is not consent, and there is no second ask.

    The agent has no approved text for one, which is the strongest form the rule
    can take: the capability does not exist rather than being a rule a model is
    asked to respect.
    """
    session = drive(sample_profile, step=Step.DISCLOSE_AND_CONSENT)

    outcome = session.advance(consent_turn(given=False))

    assert outcome.terminal_state is TerminalState.TRANSFERRED_TO_HUMAN
    assert outcome.finished is True
    assert outcome.agent_utterance == TRANSFER_TO_HUMAN_UTTERANCE
    assert session.state.consent_given is False


def test_an_absent_consent_verdict_is_treated_as_a_refusal(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    session = drive(sample_profile, step=Step.DISCLOSE_AND_CONSENT)

    outcome = session.advance(extraction(Step.DISCLOSE_AND_CONSENT))

    assert outcome.terminal_state is TerminalState.TRANSFERRED_TO_HUMAN
    assert session.state.consent_given is False


# ---------------------------------------------------------------------------
# state_balance — the step that asserts rather than collects
# ---------------------------------------------------------------------------


def test_acknowledging_the_balance_collects_nothing_and_moves_on(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """No rule and no callback: the customer is acknowledging figures read to them.

    A customer who takes the block's invitation to disagree is saying something
    the approved script cannot answer, so it arrives as ``needs_human`` and
    ``_listen`` transfers before this rule runs. Writing a ``Dispute`` row here as
    well would be a second, quieter copy of the same fact, and the two could
    disagree.
    """
    session = drive(sample_profile, step=Step.STATE_BALANCE)

    outcome = session.advance(balance_turn())

    assert outcome.step is Step.CONFIRM_TERMS
    assert session.state.needs_callback is False
    assert session.state.disputes == []


# ---------------------------------------------------------------------------
# Transfer — the same behaviour whatever provoked it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("step", LISTENING_STEPS, ids=lambda step: step.value)
def test_a_non_routine_turn_transfers_from_any_step(
    drive: Callable[..., DrivenCall],
    step: Step,
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """``needs_human`` is checked before anything step-specific.

    The agent hands the call over *without classifying what it heard*
    (CONTRACT §7): the routing bit carries no reason, no severity and no
    category, so the behaviour cannot vary with the content. Hardship,
    vulnerability, an explicit dispute and a request for a person all arrive here
    and all leave through the same exit.
    """
    session = drive(sample_profile, step=step)

    outcome = session.advance(extraction(step, needs_human=True))

    assert outcome.terminal_state is TerminalState.TRANSFERRED_TO_HUMAN
    assert outcome.finished is True
    assert outcome.agent_utterance == TRANSFER_TO_HUMAN_UTTERANCE
    assert outcome.step is step


def test_hardship_and_a_request_for_a_person_are_the_same_bit(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """THE BOUNDARY. Two very different sentences, one indistinguishable outcome.

    BLUEPRINT §5 makes a missed hardship cue a zero-tolerance failure and §7
    refuses to automate hardship negotiation, and the resolution is that **the
    duty is to route to a human, not to classify**. So "perdi o emprego" and "quero
    falar com uma pessoa" produce the same transfer, in the same words, at the
    same step, and neither record says which happened. The moment the routing bit
    carries *why*, this system is classifying a vulnerable customer in a
    debt-collection context — which is the collections shape of the red-flag
    detector the healthcare original refused to build.

    This test fails the moment anyone adds a reason to the transfer.
    """
    hardship = drive(sample_profile, step=Step.OFFER_PAYMENT_PATH)
    asks_for_a_person = drive(sample_profile, step=Step.OFFER_PAYMENT_PATH)

    hardship_turn = hardship.advance(
        _needs_human_turn(
            Step.OFFER_PAYMENT_PATH,
            "eu perdi o emprego no mês passado e não tenho como pagar nada agora",
        ),
    )
    request_turn = asks_for_a_person.advance(
        _needs_human_turn(
            Step.OFFER_PAYMENT_PATH,
            "prefiro falar com uma pessoa, por favor",
        ),
    )

    assert hardship_turn.agent_utterance == request_turn.agent_utterance
    assert hardship_turn.terminal_state == request_turn.terminal_state
    assert hardship_turn.step == request_turn.step
    assert hardship.state.needs_callback == asks_for_a_person.state.needs_callback


def test_every_transfer_says_the_same_words_whatever_caused_it(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """A transfer that varied with the reason would be the agent classifying."""
    refused = drive(sample_profile, step=Step.DISCLOSE_AND_CONSENT)
    non_routine = drive(sample_profile, step=Step.CAPTURE_COMMITMENT)
    forced = drive(sample_profile, step=Step.CONFIRM_CONTACT)

    outcomes = [
        refused.advance(consent_turn(given=False)),
        non_routine.advance(extraction(Step.CAPTURE_COMMITMENT, needs_human=True)),
        forced.force_transfer(),
    ]

    assert {outcome.agent_utterance for outcome in outcomes} == {
        TRANSFER_TO_HUMAN_UTTERANCE
    }
    assert {outcome.terminal_state for outcome in outcomes} == {
        TerminalState.TRANSFERRED_TO_HUMAN
    }


def test_the_transfer_utterance_can_be_spoken_before_identity_is_proven(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
    slots: dict[str, str],
) -> None:
    """ "quem é? me tira dessa lista" on the opening turn, and what it may hear.

    ``_listen`` transfers on any ``needs_human`` extraction at *any* step,
    ``verify_right_party`` included, so the hand-off sentence is reachable with
    identity still unproven — which is why the institution's name was stripped
    out of it. The disclosure scanner does not list "Banco Aurora", so nothing
    would have caught this; it is a judgement, and this is the test that holds it.
    """
    session = drive(sample_profile, step=Step.VERIFY_RIGHT_PARTY)

    outcome = session.advance(extraction(Step.VERIFY_RIGHT_PARTY, needs_human=True))

    assert outcome.terminal_state is TerminalState.TRANSFERRED_TO_HUMAN
    assert session.state.identity_confirmed is False
    assert "Aurora" not in outcome.agent_utterance

    result = check_outbound_utterance(
        outcome.agent_utterance,
        fake_protocol,
        profile=sample_profile,
        slots=slots,
        identity_confirmed=False,
        prior_utterances=session.state.agent_transcript,
    )

    assert list(result.violations) == []


def test_a_dispute_survives_the_transfer_it_causes(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """ "esse valor está errado, eu já paguei" — said on the turn that transfers.

    An explicit dispute is both a thing to record and a thing the approved script
    cannot answer, so it systematically arrives with ``needs_human`` on the same
    turn. ``_listen`` writes ``commitments`` and ``disputes`` **before** the
    ``needs_human`` branch for exactly this case: capturing after it meant the
    record reached the specialist with the dispute deleted, and the specialist
    then phoned a customer to ask a question the customer had already answered.

    The ordering changes no routing — the transfer still happens, in the same
    words, for the same non-reason. It only stops the exit from being lossy.
    """
    session = drive(sample_profile, step=Step.CAPTURE_COMMITMENT)

    outcome = session.advance(
        extraction(
            Step.CAPTURE_COMMITMENT,
            needs_human=True,
            disputes=[dispute_row()],
        ),
    )
    record = build_record(
        session.state, fake_protocol, prompt_version=PROMPT_VERSION, model=MODEL
    )

    assert outcome.terminal_state is TerminalState.TRANSFERRED_TO_HUMAN
    assert [dispute.subject for dispute in record.disputes] == ["o valor"]
    assert record.disputes[0].detail == "já paguei"
    assert record.disputes[0].source_utterance


def test_a_commitment_survives_the_transfer_it_arrives_with(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """The other half of the same ordering, and the one a rule could have hidden.

    A customer who names an amount and then asks for a person has still named an
    amount. It is written down once — both fields carry ``operator.add``
    reducers, so a second write would append the same row twice.
    """
    session = drive(sample_profile, step=Step.CAPTURE_COMMITMENT)

    session.advance(
        extraction(
            Step.CAPTURE_COMMITMENT,
            needs_human=True,
            commitments=[complete_commitment(sample_profile)],
        ),
    )

    assert len(session.state.commitments) == 1
    assert session.state.commitments[0].amount == "R$ 847,32"


def test_a_turn_the_service_could_not_extract_hands_the_call_to_a_person(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """No extraction is not an empty extraction; it is a turn nobody understood."""
    session = drive(sample_profile, step=Step.OFFER_PAYMENT_PATH)

    outcome = machine.advance(session.graph, session.call_id, Turn())

    assert outcome.terminal_state is TerminalState.TRANSFERRED_TO_HUMAN
    assert outcome.agent_utterance == TRANSFER_TO_HUMAN_UTTERANCE


# ---------------------------------------------------------------------------
# confirm_terms — callback rule 1
# ---------------------------------------------------------------------------


def test_a_correct_restatement_confirms_the_terms_and_moves_on(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    session = drive(sample_profile, step=Step.CONFIRM_TERMS)

    outcome = session.advance(terms_turn(sample_profile), terms_correct=True)

    assert session.state.terms_confirmed is True
    assert session.state.needs_callback is False
    assert outcome.step is Step.OFFER_PAYMENT_PATH


def test_a_wrong_restatement_re_reads_the_rendered_approved_text_and_asks_again(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
    slots: dict[str, str],
) -> None:
    """The retry is the approved figures read again, never a correction.

    The agent never composes a correction, never states the amount in different
    words and never meets a near-miss halfway: a paraphrased balance is a
    fabricated figure however close it lands, and "helping" a customer converge
    on a number is the agent asserting a fact it cannot verify. The only fact it
    holds is the one already in the block, and the only correction available is
    to read that block again — **rendered**, with this account's real amount,
    exactly as it was read the first time.
    """
    session = drive(sample_profile, step=Step.CONFIRM_TERMS)

    outcome = session.advance(terms_turn(sample_profile), terms_correct=False)

    assert outcome.finished is False
    assert outcome.step is Step.CONFIRM_TERMS
    assert session.state.step is Step.CONFIRM_TERMS
    assert outcome.agent_utterance == (
        fake_protocol.render(Step.STATE_BALANCE, slots)
        + "\n\n"
        + fake_protocol.text_for(Step.CONFIRM_TERMS)
    )
    assert "{balance}" not in outcome.agent_utterance
    assert slots["balance"] in outcome.agent_utterance
    assert session.state.terms_confirmed is None
    assert session.state.needs_callback is False


def test_the_re_read_is_still_approved_text(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
    slots: dict[str, str],
) -> None:
    """Two approved blocks concatenated, which is why the allowlist allows that.

    The gate accepts a concatenation of approved blocks precisely because this
    retry exists. Screening it here is what keeps the allowance honest: the
    concatenation the machine actually produces is the one the gate was widened
    for, and nothing wider.
    """
    session = drive(sample_profile, step=Step.CONFIRM_TERMS)

    outcome = session.advance(terms_turn(sample_profile), terms_correct=False)
    result = check_outbound_utterance(
        outcome.agent_utterance,
        fake_protocol,
        profile=sample_profile,
        slots=slots,
        identity_confirmed=True,
    )

    assert list(result.violations) == []


def test_a_correct_restatement_on_the_second_attempt_still_confirms(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    session = drive(sample_profile, step=Step.CONFIRM_TERMS)

    session.advance(terms_turn(sample_profile), terms_correct=False)
    outcome = session.advance(terms_turn(sample_profile), terms_correct=True)

    assert session.state.terms_confirmed is True
    assert session.state.needs_callback is False
    assert outcome.step is Step.OFFER_PAYMENT_PATH


def test_a_second_wrong_restatement_is_recorded_and_the_call_continues(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """CALLBACK RULE 1. An unconfirmed restatement is information, not an abort.

    The pressure on a system whose headline metric is fully-automated rate is to
    record this as confirmed and move on. It is recorded as false, the call
    requires a specialist callback, and the remaining steps still run — the
    payment path, the commitment and the contact channel are worth collecting
    either way.
    """
    session = drive(sample_profile, step=Step.CONFIRM_TERMS)

    session.advance(terms_turn(sample_profile), terms_correct=False)
    outcome = session.advance(terms_turn(sample_profile), terms_correct=False)

    assert session.state.terms_confirmed is False
    assert session.state.needs_callback is True
    assert outcome.finished is False
    assert outcome.step is Step.OFFER_PAYMENT_PATH


def test_rule_one_reads_the_verdict_and_never_the_restatement(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """RULE 1 READS A BOOLEAN, AND WHAT WAS SAID CANNOT CHANGE IT.

    A customer who reproduced the figures perfectly and a customer who said
    "sei lá, uns cem reais" are the same call to this rule once the judge has
    returned the same verdict. That matters here more than almost anywhere else:
    BLUEPRINT §6's fairness stratification says the customers most likely to be
    misheard are the ones the duty exists to protect, and this is the step where
    an ASR error turns into a record. A rule that graded the restatement itself
    would be grading how articulate the customer was.
    """
    outcomes = (
        (False, Step.CONFIRM_TERMS),
        (True, Step.OFFER_PAYMENT_PATH),
    )
    for verdict, expected in outcomes:
        exact = drive(sample_profile, step=Step.CONFIRM_TERMS)
        vague = drive(sample_profile, step=Step.CONFIRM_TERMS)

        exact_turn = exact.advance(terms_turn(sample_profile), terms_correct=verdict)
        vague_turn = vague.advance(
            terms_turn(
                sample_profile,
                restated_amount="uns cem reais",
                restated_date="sei lá",
            ),
            terms_correct=verdict,
        )

        assert exact_turn.step is expected
        assert exact_turn.step == vague_turn.step
        assert exact_turn.agent_utterance == vague_turn.agent_utterance
        assert exact.state.terms_confirmed == vague.state.terms_confirmed
        assert exact.state.needs_callback == vague.state.needs_callback


# ---------------------------------------------------------------------------
# offer_payment_path — callback rule 2
# ---------------------------------------------------------------------------


def test_a_chosen_payment_path_leaves_nothing_outstanding(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    session = drive(sample_profile, step=Step.OFFER_PAYMENT_PATH)

    outcome = session.advance(path_turn(PaymentPath.PAYMENT_LINK))

    assert session.state.selected_path is PaymentPath.PAYMENT_LINK
    assert session.state.needs_callback is False
    assert outcome.step is Step.CAPTURE_COMMITMENT


def test_no_chosen_payment_path_requires_a_callback(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """CALLBACK RULE 2, and a null path is a completeness failure and nothing more.

    It does not record that the customer refused, or hesitated, or asked for
    something outside the four approved paths.
    """
    session = drive(sample_profile, step=Step.OFFER_PAYMENT_PATH)

    outcome = session.advance(path_turn(None))

    assert session.state.selected_path is None
    assert session.state.needs_callback is True
    assert outcome.step is Step.CAPTURE_COMMITMENT


def test_rule_two_reads_whether_a_path_was_named_and_never_which_one(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """RULE 2 READS PRESENCE. All four paths are worth the same to this machine.

    Choosing instalments over paying now changes nothing about routing, the
    queue, or anything else — and that symmetry is what keeps the approved
    capability statement ("não consigo oferecer descontos...") a statement about
    the system rather than a response to what a particular customer asked for.
    """
    outcomes = []
    for path in PaymentPath:
        session = drive(sample_profile, step=Step.OFFER_PAYMENT_PATH)
        outcome = session.advance(path_turn(path))
        outcomes.append((outcome.step, outcome.agent_utterance, session.state))

    steps = {step for step, _utterance, _state in outcomes}
    utterances = {utterance for _step, utterance, _state in outcomes}
    callbacks = {state.needs_callback for _step, _utterance, state in outcomes}
    paths = {state.selected_path for _step, _utterance, state in outcomes}

    assert steps == {Step.CAPTURE_COMMITMENT}
    assert len(utterances) == 1
    assert callbacks == {False}
    assert paths == set(PaymentPath)


# ---------------------------------------------------------------------------
# capture_commitment — callback rule 3, the concession boundary
# ---------------------------------------------------------------------------


def test_a_commitment_missing_a_field_requires_a_specialist_callback(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """CALLBACK RULE 3. The rule is completeness, and it reads two fields for ``None``.

    A row without an amount or without a date is a row the specialist has to
    phone about — not because of *how much* was promised, but because the call
    did not finish writing it down.
    """
    session = drive(sample_profile, step=Step.CAPTURE_COMMITMENT)

    session.advance(commitment_turn(partial_commitment()))

    assert session.state.needs_callback is True
    assert [row.amount for row in session.state.commitments] == ["mil e duzentos"]
    assert session.state.commitments[0].date is None


def test_a_complete_commitment_leaves_nothing_outstanding(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """The half of the rule that makes the primary metric reachable at all.

    Six of the fifteen golden-set cases expect ``completed_no_callback`` and all
    of them record a promise to pay, so a rule that flagged commitment *presence*
    would pin ``fully_automated_rate`` at zero and make the only way to score
    above it a failure to write a promise down.
    """
    session = drive(sample_profile, step=Step.CAPTURE_COMMITMENT)

    session.advance(commitment_turn(complete_commitment(sample_profile)))

    assert session.state.needs_callback is False
    assert len(session.state.commitments) == 1


def test_rule_three_reads_field_nullity_and_never_the_amount(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """THE CONCESSION BOUNDARY, AND THE UNCOMFORTABLE HALF OF IT.

    A customer promising **R$ 4.000,00 with a date** comes out fully automated. A
    customer promising **R$ 40,00 who will not name a day** does not. Same rule,
    opposite outcomes, and the only thing separating them is whether a field is
    present — the values are never read and the sizes never compared.

    That is uncomfortable and it is correct. Deciding *which amounts* warrant
    human review is customer-specific logic and this agent holds no threshold,
    nor should it: a threshold is precisely the "how much is this account worth"
    judgement that CONTRACT §7 refuses and that ``CallRecord`` has no field to
    carry. The healthcare system this rule is ported from made the same trade one
    industry over, reading a medication row for a missing dose, unit or frequency
    and never for what the drug was. Same shape, same reason, different regulator.

    This test fails the moment anyone adds an amount threshold.
    """
    large = drive(sample_profile, step=Step.CAPTURE_COMMITMENT)
    small = drive(sample_profile, step=Step.CAPTURE_COMMITMENT)

    large.advance(
        commitment_turn(
            PaymentCommitment(
                amount="R$ 4.000,00",
                date="dia 20",
                method=PaymentPath.SCHEDULE,
                source_utterance="pago quatro mil no dia 20",
            )
        ),
    )
    small.advance(
        commitment_turn(
            PaymentCommitment(
                amount="R$ 40,00",
                date=None,
                method=None,
                source_utterance="consigo uns quarenta reais, mas não sei o dia",
            )
        ),
    )

    large_end = large.advance(contact_turn(confirmed=True))
    small_end = small.advance(contact_turn(confirmed=True))

    assert large_end.terminal_state is TerminalState.COMPLETED_NO_CALLBACK
    assert small_end.terminal_state is TerminalState.COMPLETED_NEEDS_CALLBACK
    assert large_end.agent_utterance == small_end.agent_utterance


def test_a_customer_who_promises_nothing_leaves_nothing_outstanding(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """An empty list has no row missing a field.

    The rule is about the rows that exist, not about whether the customer made a
    promise at all — a customer who commits to nothing has left nothing
    half-written, and reading their silence as a callback would be the rule
    starting to judge the outcome rather than the record.
    """
    session = drive(sample_profile, step=Step.CAPTURE_COMMITMENT)

    session.advance(commitment_turn())

    assert session.state.commitments == []
    assert session.state.needs_callback is False


def test_a_dispute_is_recorded_without_changing_where_the_call_goes(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """A recorded dispute does not, on its own, require a callback.

    The specialist reviews every record regardless. Making a dispute trigger one
    would be the agent assessing that dispute's merit — routing on the content of
    what the customer said — and it is the tempting direction, because nothing
    feels risky about being a little more careful with someone who says they
    already paid. "Já paguei", "esse valor não é meu" and "eu nunca peguei esse
    empréstimo" are three different facts for the specialist and this machine is
    not permitted to decide which is serious, let alone which is true. FDCPA
    §809(b) cease-collection-on-dispute is a person's action, taken on a record
    that reached them exactly like every other record.
    """
    session = drive(sample_profile, step=Step.CAPTURE_COMMITMENT)

    outcome = session.advance(
        commitment_turn(
            complete_commitment(sample_profile),
            disputes=[dispute_row(subject="o valor", detail="já paguei em julho")],
        ),
    )

    assert [dispute.subject for dispute in session.state.disputes] == ["o valor"]
    assert session.state.needs_callback is False
    assert outcome.step is Step.CONFIRM_CONTACT


# ---------------------------------------------------------------------------
# unresolved — callback rule 5
# ---------------------------------------------------------------------------


def test_a_turn_the_record_could_not_carry_whole_requires_a_callback(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """CALLBACK RULE 5. ``unresolved`` is read as a bit and never as a reason.

    It carries a question the approved text cannot answer, and an amount or a
    date the customer named but could not pin down — which produces no row at
    all, because the value would have to be guessed and a guessed figure in a
    payment plan is the most dangerous fabrication in this system.
    """
    session = drive(sample_profile, step=Step.CAPTURE_COMMITMENT)

    session.advance(
        commitment_turn(complete_commitment(sample_profile), unresolved=True),
    )

    assert session.state.needs_callback is True


def test_rule_five_reads_the_bit_and_nothing_about_what_was_left_open(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """RULE 5 READS NULLITY IN ITS PUREST FORM: one boolean, no reason field.

    The two calls below carry byte-identical content and differ only in the bit.
    There is nowhere on a :class:`~trail.models.TurnExtraction` to say *what*
    was unresolved, and that absence is the guarantee — the moment the bit
    carried a reason, the agent would be classifying the customer it could not
    finish serving.
    """
    resolved = drive(sample_profile, step=Step.CAPTURE_COMMITMENT)
    unresolved = drive(sample_profile, step=Step.CAPTURE_COMMITMENT)

    row = complete_commitment(sample_profile)
    resolved_turn = resolved.advance(commitment_turn(row))
    unresolved_turn = unresolved.advance(commitment_turn(row, unresolved=True))

    assert resolved_turn.step == unresolved_turn.step
    assert resolved_turn.agent_utterance == unresolved_turn.agent_utterance
    assert resolved.state.needs_callback is False
    assert unresolved.state.needs_callback is True
    assert not [name for name in TurnExtraction.model_fields if "reason" in name]


# ---------------------------------------------------------------------------
# confirm_contact — callback rule 4
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "confirmed",
    [
        pytest.param(False, id="probably-my-mobile"),
        pytest.param(None, id="no-answer-at-all"),
    ],
)
def test_a_contact_channel_that_was_not_confirmed_requires_a_callback(
    drive: Callable[..., DrivenCall],
    confirmed: bool | None,
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """CALLBACK RULE 4, and it fails closed.

    "Provavelmente chega no meu celular" is not a confirmation, and the failure
    it guards is a payment link sent to a stale channel — which reads to the
    customer as the bank ignoring a promise they made minutes earlier, and shows
    up in the numbers as a repeat contact nobody can explain. This is an
    administrative delivery fact and not a judgement about the customer: an
    unanswered question is not a channel.
    """
    session = drive(sample_profile, step=Step.CONFIRM_CONTACT)

    outcome = session.advance(contact_turn(confirmed=confirmed))

    assert session.state.contact_channel_confirmed is confirmed
    assert outcome.terminal_state is TerminalState.COMPLETED_NEEDS_CALLBACK


def test_rule_four_reads_the_confirmation_and_never_which_channel(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """RULE 4 READS ONE BOOLEAN. WhatsApp and SMS are the same call to it.

    The agent has no approved customer-specific text for a phone number and never
    speaks one; the customer reads back the channel they already have. Which one
    it is belongs to the delivery system, not to the routing rule.
    """
    whatsapp = drive(sample_profile, step=Step.CONFIRM_CONTACT)
    sms = drive(sample_profile, step=Step.CONFIRM_CONTACT)

    whatsapp_end = whatsapp.advance(
        contact_turn(confirmed=True, notes="recebeu no WhatsApp"),
    )
    sms_end = sms.advance(contact_turn(confirmed=True, notes="recebeu por SMS"))

    assert whatsapp_end.terminal_state == sms_end.terminal_state
    assert whatsapp_end.agent_utterance == sms_end.agent_utterance
    assert whatsapp.state.needs_callback is False
    assert sms.state.needs_callback is False


# ---------------------------------------------------------------------------
# Completion and the five exits
# ---------------------------------------------------------------------------


def test_a_call_with_nothing_outstanding_completes_without_a_callback(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    session = drive(sample_profile, step=Step.CONFIRM_CONTACT)

    outcome = session.advance(contact_turn(confirmed=True))

    assert outcome.finished is True
    assert outcome.step is Step.POST_OUTCOME
    assert outcome.agent_utterance == fake_protocol.text_for(Step.POST_OUTCOME)
    assert outcome.terminal_state is TerminalState.COMPLETED_NO_CALLBACK


def test_a_call_that_left_something_unresolved_completes_needing_a_callback(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """The flag changes the measurement and changes no routing.

    Every record reaches the same specialist queue in ``started_at`` order either
    way, and the approved closing text tells every customer, out loud, that a
    specialist reviews the call without exception. What the flag decides is the
    terminal state, which is what separates an honest fully-automated rate from
    the vendor habit of reporting promise-to-pay and calling it money.
    """
    session = drive(sample_profile, step=Step.CONFIRM_CONTACT, needs_callback=True)

    outcome = session.advance(contact_turn(confirmed=True))

    assert outcome.terminal_state is TerminalState.COMPLETED_NEEDS_CALLBACK
    assert outcome.agent_utterance == fake_protocol.text_for(Step.POST_OUTCOME)


def test_a_finished_call_refuses_another_turn(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    session = drive(sample_profile, step=Step.CONFIRM_CONTACT)
    session.advance(contact_turn(confirmed=True))

    with pytest.raises(RuntimeError, match="already finished"):
        session.advance(contact_turn(confirmed=True))


def test_the_graph_has_five_exits_and_every_one_is_reachable(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """Five terminal states, all first-class, all driven here from the outside.

    ``not_reached`` is on the list for the same reason it was one industry over:
    outbound connects at about 28% (BLUEPRINT §4), and a rate computed over
    answered calls would quietly delete the rest of the book. A terminal state
    that only ever appears in a fixture is not an outcome, it is a comment.
    """
    reached: set[TerminalState] = set()

    clean = drive(sample_profile, step=Step.CONFIRM_CONTACT)
    reached.add(clean.advance(contact_turn(confirmed=True)).terminal_state)

    callback = drive(sample_profile, step=Step.CONFIRM_CONTACT)
    reached.add(callback.advance(contact_turn(confirmed=False)).terminal_state)

    transfer = drive(sample_profile, step=Step.DISCLOSE_AND_CONSENT)
    reached.add(transfer.advance(consent_turn(given=False)).terminal_state)

    wrong_party = drive(sample_profile, step=Step.VERIFY_RIGHT_PARTY)
    reached.add(
        wrong_party.advance(
            identity_turn(sample_profile, stated_name="Fernanda Moreira Lima")
        ).terminal_state
    )

    unreached = drive(sample_profile)
    reached.add(unreached.override("not_reached").terminal_state)

    assert reached == set(TerminalState)


# ---------------------------------------------------------------------------
# not_reached, and the turn that could not be processed
# ---------------------------------------------------------------------------


def test_an_unreached_customer_is_a_recorded_outcome_not_missing_data(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """Marked by the caller, never inferred, and it keeps the account in the denominator.

    A voice agent cannot fix a wrong phone number, and dropping unreached accounts
    from the primary metric's denominator is the same self-flattery as reporting
    promise-to-pay and calling it money. Nothing is spoken: an unanswered call
    produces no utterance, and an empty utterance discloses nothing.
    """
    session = drive(sample_profile)

    outcome = session.override("not_reached")
    record = build_record(
        session.state, fake_protocol, prompt_version=PROMPT_VERSION, model=MODEL
    )

    assert session.state.finished is True
    assert outcome.agent_utterance == ""
    assert record.terminal_state is TerminalState.NOT_REACHED
    assert record.needs_specialist_review is True


def test_a_model_call_the_service_could_not_complete_re_asks_the_same_question(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """``retry`` holds the call open, keeps the cost, and repeats the block verbatim.

    The customer's answer never reached the record, so the question is still
    open and the agent re-asks it in the same approved words. The tokens are
    already spent and are accounted for; nothing is added to the transcript,
    because a turn the service could not process produced no exchange to record.
    """
    session = drive(sample_profile, step=Step.CAPTURE_COMMITMENT)
    asked = session.opening.agent_utterance

    outcome = machine.advance(
        session.graph,
        session.call_id,
        Turn(override="retry", total_input_tokens=120, cost_usd=0.004),
    )

    assert outcome.finished is False
    assert outcome.step is Step.CAPTURE_COMMITMENT
    assert outcome.agent_utterance == asked
    assert session.state.total_input_tokens == 120
    assert session.state.cost_usd == pytest.approx(0.004)
    assert session.state.commitments == []


# ---------------------------------------------------------------------------
# force_transfer — both branches
# ---------------------------------------------------------------------------


def test_a_call_in_flight_can_be_handed_to_a_person_at_any_point(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """The in-flight branch: an ordinary resume into the transfer node.

    This is what the service does when the compliance gate refuses an utterance —
    the words never reach the customer, so they are not transcript, and the call
    ends the same way every other transfer ends.
    """
    session = drive(sample_profile, step=Step.OFFER_PAYMENT_PATH)

    outcome = session.force_transfer()

    assert outcome.finished is True
    assert outcome.terminal_state is TerminalState.TRANSFERRED_TO_HUMAN
    assert outcome.agent_utterance == TRANSFER_TO_HUMAN_UTTERANCE
    assert session.state.finished is True
    assert session.state.agent_transcript == []


def test_a_finished_call_can_still_be_recorded_as_a_transfer(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """The finished branch, which is how a compliance failure is recorded at all.

    Unreachable unless the *approved protocol text* itself fails the gate — there
    is no node left to route to, so the outcome is written straight into the
    checkpoint. It must still work, because :class:`~trail.models.CallRecord`
    has no violations column and must not gain one: the terminal state is the
    only place the consequence can live, and a call whose output failed an
    assertion can never be counted as a clean, fully automated completion.
    """
    session = drive(sample_profile, step=Step.CONFIRM_CONTACT)
    completed = session.advance(contact_turn(confirmed=True))
    assert completed.terminal_state is TerminalState.COMPLETED_NO_CALLBACK

    outcome = session.force_transfer()
    record = build_record(
        session.state, fake_protocol, prompt_version=PROMPT_VERSION, model=MODEL
    )

    assert outcome.finished is True
    assert outcome.terminal_state is TerminalState.TRANSFERRED_TO_HUMAN
    assert outcome.agent_utterance == TRANSFER_TO_HUMAN_UTTERANCE
    assert session.state.terminal_state is TerminalState.TRANSFERRED_TO_HUMAN
    assert record.terminal_state is TerminalState.TRANSFERRED_TO_HUMAN


def test_force_transfer_refuses_a_call_it_has_never_seen(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """An unknown call is an error, never a silently invented transfer."""
    session = drive(sample_profile)

    with pytest.raises(RuntimeError, match="unknown call"):
        machine.force_transfer(session.graph, uuid4())


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def test_every_record_leaves_the_agent_unreviewed(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """Nothing finalises itself. Every AI output requires human verification."""
    session = drive(sample_profile, step=Step.CONFIRM_CONTACT)
    session.advance(contact_turn(confirmed=True))

    record = build_record(
        session.state, fake_protocol, prompt_version=PROMPT_VERSION, model=MODEL
    )

    assert record.needs_specialist_review is True
    assert record.reviewed_by is None
    assert record.reviewed_at is None


def test_a_record_cannot_be_built_for_a_call_still_in_flight(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    session = drive(sample_profile)

    with pytest.raises(RuntimeError, match="has not finished"):
        build_record(
            session.state, fake_protocol, prompt_version=PROMPT_VERSION, model=MODEL
        )


def test_the_record_carries_the_versions_that_produced_it(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """A call can only be replayed against the exact text it was given."""
    session = drive(
        sample_profile,
        step=Step.CONFIRM_CONTACT,
        started_at=datetime.now(timezone.utc) - timedelta(seconds=90),
    )
    session.advance(contact_turn(confirmed=True))

    record = build_record(
        session.state, fake_protocol, prompt_version=PROMPT_VERSION, model=MODEL
    )

    assert record.protocol_version == fake_protocol.version
    assert record.prompt_version == PROMPT_VERSION
    assert record.model == MODEL
    assert record.account_id == sample_profile.account_id
    assert record.wall_seconds == pytest.approx(90, abs=5)


def test_cached_input_tokens_are_counted_into_the_call_total(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """``total_input_tokens`` is all the input the model processed.

    The split the economics post needs — served from cache versus paid for in
    full — lives on the per-call :class:`~trail.models.LLMCallTrace` rows.
    """
    session = drive(sample_profile)
    trace = LLMCallTrace(
        call_id=session.state.call_id,
        step=Step.CAPTURE_COMMITMENT,
        prompt_version=PROMPT_VERSION,
        model=MODEL,
        request_json={},
        response_json={},
        input_tokens=300,
        output_tokens=120,
        cache_read_input_tokens=700,
        cost_usd=0.0065,
        latency_ms=1200,
        created_at=datetime.now(timezone.utc),
    )

    folded = usage([trace])
    assert folded["total_input_tokens"] == 1000
    assert folded["total_output_tokens"] == 120
    assert folded["cost_usd"] == pytest.approx(0.0065)

    # And the graph adds it to the call's running totals, which is the half a
    # unit test of the arithmetic alone would not have caught.
    machine.advance(
        session.graph,
        session.call_id,
        Turn(extraction=identity_turn(sample_profile), **folded),
    )

    assert session.state.total_input_tokens == 1000
    assert session.state.cost_usd == pytest.approx(0.0065)


# ---------------------------------------------------------------------------
# The whole call
# ---------------------------------------------------------------------------


def test_the_happy_path_speaks_every_approved_block_exactly_once(
    drive: Callable[..., DrivenCall],
    sample_profile: AccountProfile,
    fake_protocol: Protocol,
) -> None:
    """One cooperative customer, start to finish, and the shape that scores.

    Seven customer turns — one per step that waits for a reply — and eight agent
    utterances, each of them a protocol block delivered in declaration order:
    seven verbatim and one, ``state_balance``, rendered from this account's own
    record. ``post_outcome`` is spoken and the call ends on the same turn, so it
    is a step the agent speaks and never one it waits at.

    The terminal state is ``completed_no_callback``: right party verified,
    consent given, terms restated correctly, a payment path chosen, one promise
    to pay recorded whole, contact channel confirmed. This is the only shape that
    moves the primary metric, and asserting it here is what stops the state
    machine and the golden set drifting into two different callback rules.
    """
    session = drive(sample_profile)
    opening = session.opening
    spoken = [opening.agent_utterance]

    turns: list[tuple[Step, TurnExtraction, bool | None]] = [
        (Step.VERIFY_RIGHT_PARTY, identity_turn(sample_profile), None),
        (Step.DISCLOSE_AND_CONSENT, consent_turn(given=True), None),
        (Step.STATE_BALANCE, balance_turn(), None),
        (Step.CONFIRM_TERMS, terms_turn(sample_profile), True),
        (Step.OFFER_PAYMENT_PATH, path_turn(PaymentPath.PAY_NOW), None),
        (
            Step.CAPTURE_COMMITMENT,
            commitment_turn(complete_commitment(sample_profile)),
            None,
        ),
        (Step.CONFIRM_CONTACT, contact_turn(confirmed=True), None),
    ]

    outcome = opening
    for step, turn, verdict in turns:
        assert session.state.step is step
        outcome = session.advance(turn, terms_correct=verdict)
        spoken.append(outcome.agent_utterance)

    assert spoken == [
        approved_utterance(fake_protocol, step, sample_profile) for step in Step
    ]
    assert outcome.finished is True
    assert outcome.terminal_state is TerminalState.COMPLETED_NO_CALLBACK

    record = build_record(
        session.state, fake_protocol, prompt_version=PROMPT_VERSION, model=MODEL
    )
    assert record.consent_given is True
    assert record.terms_confirmed is True
    assert record.contact_channel_confirmed is True
    assert record.selected_path is PaymentPath.PAY_NOW
    assert [row.amount for row in record.commitments] == ["R$ 847,32"]
    assert record.disputes == []
