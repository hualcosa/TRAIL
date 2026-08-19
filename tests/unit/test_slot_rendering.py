"""Speaking a customer's balance without letting a model produce the number.

The healthcare protocol this loader was ported from never had to do this: every
approved block said the same sentence to every caller, so verbatim delivery and
an exact-string allowlist were the same guarantee twice. Collections has to say
what is owed and when it was due (BLUEPRINT §3), and a wrong balance spoken
aloud blocks release (BLUEPRINT §5). The slot mechanism is how both survive:
the approved file declares ``{balance}``, the value arrives from the system of
record through a deterministic formatter, and :meth:`Protocol.render`
substitutes it by literal replacement.

So what is under test here is a narrow property with a lot resting on it:
**rendering is a pure function of the approved template and a slot mapping whose
key set the template itself declares.** Everything below is either that
function's determinism or one of the ways a caller can be told it has the wrong
mapping — because the moment a slot can be missing, unknown, or silently
ignored, "the number came from the system of record" stops being provable.

The other half of the claim — that an utterance rendered with the wrong balance
fails the compliance allowlist — belongs to ``test_compliance_assertions.py``,
next to the allowlist it exercises. This file stops at the loader.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from trail.models import Step
from trail.protocol import Protocol, load_protocol

pytestmark = pytest.mark.unit

VERSION_HEADER = "<!-- protocol_version: 9.9.9 -->"

#: The only slotted step in protocol 1.0.0, and the one the contract pins.
SLOTTED_STEP = Step.STATE_BALANCE
#: A block that says the same thing to every customer, for the contrast.
PLAIN_STEP = Step.CONFIRM_TERMS

BALANCE_SLOTS = frozenset({"product", "balance", "due_date", "days_past_due"})

#: ``{balance}`` twice on purpose: a slot is declared once however often it is
#: spoken, and every occurrence has to be substituted. A balance stated once and
#: repeated back unrendered is exactly the mixed output that would make an
#: allowlist failure look like a rendering bug.
SLOTTED_BODY = (
    "O saldo em aberto do seu {product} é de {balance}, com vencimento em "
    "{due_date}. São {days_past_due} dias de atraso. Repetindo: {balance}."
)

SLOT_VALUES = {
    "product": "empréstimo pessoal",
    "balance": "R$ 1.847,32",
    "due_date": "20 de agosto de 2026",
    "days_past_due": "12",
}


def _section(name: str, body: str) -> str:
    """One ``## <heading>`` section carrying one ```spoken`` block."""
    return f"## {name}\n\n```spoken\n{body}\n```\n"


def _protocol_text(**bodies: str) -> str:
    """Every step, with the named steps carrying a custom approved block."""
    sections = [
        _section(step.value, bodies.get(step.value, f"Texto aprovado: {step.value}."))
        for step in Step
    ]
    return "\n".join([VERSION_HEADER, "# Test protocol", "", *sections])


@pytest.fixture
def slotted(write_protocol: Callable[[str], Path]) -> Protocol:
    """A protocol whose ``state_balance`` block declares the four v1 slots."""
    text = _protocol_text(**{SLOTTED_STEP.value: SLOTTED_BODY})
    return load_protocol(write_protocol(text))


# ---------------------------------------------------------------------------
# The protocol this repository actually ships
# ---------------------------------------------------------------------------


def test_the_shipped_protocol_slots_only_the_balance_statement(
    real_protocol: Protocol,
) -> None:
    """Exactly one block in the file is customer-specific, and it is named.

    Every other approved utterance is identical for every customer, which is
    what keeps ``assert_agent_text_is_approved`` an exact-string check almost
    everywhere and confines the whole rendering surface to one paragraph a
    compliance reviewer can read in ten seconds.
    """
    assert real_protocol.slots_for(SLOTTED_STEP) == BALANCE_SLOTS
    slotted_steps = [step for step in Step if real_protocol.slots_for(step)]
    assert slotted_steps == [SLOTTED_STEP]


def test_the_shipped_balance_statement_renders_with_no_braces_left(
    real_protocol: Protocol,
) -> None:
    """A brace surviving into an utterance is a template being read aloud."""
    rendered = real_protocol.render(SLOTTED_STEP, SLOT_VALUES)

    assert "{" not in rendered and "}" not in rendered
    assert SLOT_VALUES["balance"] in rendered
    assert SLOT_VALUES["due_date"] in rendered


# ---------------------------------------------------------------------------
# What a block declares
# ---------------------------------------------------------------------------


def test_a_block_that_says_the_same_thing_to_everyone_declares_no_slots(
    slotted: Protocol,
) -> None:
    assert slotted.slots_for(PLAIN_STEP) == frozenset()


def test_a_slotted_block_declares_exactly_the_names_it_uses(
    slotted: Protocol,
) -> None:
    """Declared once each, however often they are spoken."""
    assert slotted.slots_for(SLOTTED_STEP) == BALANCE_SLOTS


def test_a_stray_brace_in_prose_is_not_a_slot(
    write_protocol: Callable[[str], Path],
) -> None:
    """An unmatched brace is text, and text is spoken verbatim.

    The slot pattern cannot match across a brace, so a lone ``{`` can never
    swallow the rest of a paragraph and arrive as a slot name — which would
    turn a typo into a block demanding a value nobody knows how to supply.
    """
    body = "Confirme o valor } e a data { exatamente como falamos."
    protocol = load_protocol(write_protocol(_protocol_text(**{PLAIN_STEP.value: body})))

    assert protocol.slots_for(PLAIN_STEP) == frozenset()
    assert protocol.render(PLAIN_STEP, {}) == body


def test_a_stray_brace_beside_a_real_slot_still_renders(
    write_protocol: Callable[[str], Path],
) -> None:
    """This is the case ``str.format`` cannot survive, and the reason it is not used.

    ``"{ e o saldo é {balance}".format(balance=...)`` raises on the first brace.
    Walking the declared names and replacing each one literally does not care
    what else is in the paragraph.
    """
    body = "Anote { e confirme: o saldo é de {balance}."
    protocol = load_protocol(
        write_protocol(_protocol_text(**{SLOTTED_STEP.value: body}))
    )

    assert protocol.slots_for(SLOTTED_STEP) == frozenset({"balance"})
    assert protocol.render(SLOTTED_STEP, {"balance": "R$ 120,00"}) == (
        "Anote { e confirme: o saldo é de R$ 120,00."
    )


@pytest.mark.parametrize(
    "malformed",
    ["{Balance}", "{ balance }", "{balance-brl}", "{saldo!}", "{2}", "{}"],
)
def test_a_malformed_slot_name_refuses_to_load(
    write_protocol: Callable[[str], Path], malformed: str
) -> None:
    """A brace pair in approved text is a slot or a typo. Both stop the boot.

    Approved Brazilian Portuguese contains no literal braces, so there is no
    third reading. Loading it anyway would either read the characters out to a
    customer or leave the block one number short of what the reviewer approved.
    """
    body = f"O saldo em aberto é de {malformed}."
    path = write_protocol(_protocol_text(**{SLOTTED_STEP.value: body}))

    with pytest.raises(ValueError) as raised:
        load_protocol(path)

    message = str(raised.value)
    assert "not a valid slot name" in message
    assert SLOTTED_STEP.value in message
    assert malformed in message


def test_declared_slots_cannot_be_edited_at_runtime(slotted: Protocol) -> None:
    """Same immutability argument as the approved text itself.

    ``protocol_version`` on a record is only sufficient to replay a call if
    nothing about the protocol — text or slots — could have moved underneath the
    process that served it.
    """
    with pytest.raises(TypeError):
        slotted.slots[PLAIN_STEP] = frozenset({"balance"})  # type: ignore[index]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_text_for_returns_the_unrendered_template_on_a_slotted_block(
    slotted: Protocol,
) -> None:
    """Deliberate: a template is not an approved utterance.

    ``text_for`` stays verbatim, braces included, so a caller that owed
    ``render`` produces something the compliance allowlist rejects rather than
    something that quietly renders with whatever values were in scope.
    """
    assert slotted.text_for(SLOTTED_STEP) == SLOTTED_BODY
    assert "{balance}" in slotted.text_for(SLOTTED_STEP)


def test_rendering_substitutes_every_occurrence_of_a_declared_slot(
    slotted: Protocol,
) -> None:
    rendered = slotted.render(SLOTTED_STEP, SLOT_VALUES)

    assert rendered == (
        "O saldo em aberto do seu empréstimo pessoal é de R$ 1.847,32, com "
        "vencimento em 20 de agosto de 2026. São 12 dias de atraso. "
        "Repetindo: R$ 1.847,32."
    )
    assert rendered.count("R$ 1.847,32") == 2


def test_rendering_does_not_depend_on_the_order_of_the_supplied_mapping(
    slotted: Protocol,
) -> None:
    """The declared names are walked in sorted order, so the output is a pure
    function of the template and the mapping — not of insertion order.

    It matters because the rendered string is what the compliance allowlist
    compares against, and an utterance that is only *usually* byte-identical is
    an allowlist that only usually holds.
    """
    reversed_mapping = dict(reversed(list(SLOT_VALUES.items())))

    assert slotted.render(SLOTTED_STEP, reversed_mapping) == slotted.render(
        SLOTTED_STEP, SLOT_VALUES
    )


def test_a_missing_slot_refuses_to_render_and_names_both_sets(
    slotted: Protocol,
) -> None:
    """Rendering four-fifths of a balance statement is not a partial success."""
    incomplete = {k: v for k, v in SLOT_VALUES.items() if k != "due_date"}

    with pytest.raises(ValueError) as raised:
        slotted.render(SLOTTED_STEP, incomplete)

    message = str(raised.value)
    assert "{balance, days_past_due, due_date, product}" in message
    assert "{balance, days_past_due, product}" in message
    assert "Missing: {due_date}" in message
    assert "Unknown: {}" in message


def test_an_unknown_slot_refuses_to_render_and_names_both_sets(
    slotted: Protocol,
) -> None:
    """An extra key means the approved text moved and the call site did not.

    ``str.format`` would have accepted this silently, which is the whole reason
    it is not used: a caller still supplying a ``{fee}`` the block stopped
    mentioning would keep rendering a sentence it no longer understands.
    """
    with pytest.raises(ValueError) as raised:
        slotted.render(SLOTTED_STEP, {**SLOT_VALUES, "fee": "R$ 9,90"})

    message = str(raised.value)
    assert "{balance, days_past_due, due_date, product}" in message
    assert "{balance, days_past_due, due_date, fee, product}" in message
    assert "Missing: {}" in message
    assert "Unknown: {fee}" in message


def test_an_unslotted_block_renders_to_itself_and_accepts_nothing_else(
    slotted: Protocol,
) -> None:
    """Exactness runs in both directions, including from zero."""
    assert slotted.render(PLAIN_STEP, {}) == slotted.text_for(PLAIN_STEP)

    with pytest.raises(ValueError, match="must match"):
        slotted.render(PLAIN_STEP, {"balance": "R$ 1.847,32"})


def test_a_protocol_built_by_hand_declares_no_slots_and_refuses_to_render() -> None:
    """The loader is not the only guard, and the hand-built path fails closed.

    A :class:`Protocol` assembled directly gets no slot table, so a template it
    carries is inert: ``render`` rejects every value offered for it and
    ``text_for`` hands back the braces, which the allowlist rejects in turn.
    Both roads end in a refusal to speak. Neither ends in a wrong number spoken.
    """
    hand_built = Protocol(version="hand-built", texts={SLOTTED_STEP: SLOTTED_BODY})

    assert hand_built.slots_for(SLOTTED_STEP) == frozenset()
    assert hand_built.render(SLOTTED_STEP, {}) == SLOTTED_BODY

    with pytest.raises(ValueError, match="must match"):
        hand_built.render(SLOTTED_STEP, SLOT_VALUES)
