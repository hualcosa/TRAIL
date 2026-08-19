"""Loading the approved collections script, and refusing to start without it.

The guarantee under test is narrow and absolute: **the agent reads regulated
collections language and never generates it.** That holds only if every
:class:`~trail.models.Step` has approved text before the process serves its
first request, so most of this file is about the ways a protocol file can be
wrong and the fact that each of them stops the boot rather than surfacing
mid-call, where the only remaining options are silence or improvisation.

One thing this file does *not* test is slot rendering. The port added
``{balance}`` and its three companions to a loader that used to be verbatim in
the strongest sense, and that mechanism has a file of its own —
``test_slot_rendering.py``. Here a slotted block is just a block: ``text_for``
returns the raw template, braces and all, which is why several assertions below
can talk about "no amount in the file" while the agent demonstrably says one.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from trail.models import Step
from trail.protocol import Protocol, load_protocol

pytestmark = pytest.mark.unit

VERSION_HEADER = "<!-- protocol_version: 9.9.9 -->"

_CLOCK_TIME = re.compile(r"\b\d{1,2}:\d{2}\b")

#: Brazilian mobile and landline shapes: ``90000-0142``, ``3000 4000``. The
#: healthcare original matched the North American ``555-123-4567``; the failure
#: it guards is the same one, which is why the pattern moved rather than went.
_PHONE_NUMBER = re.compile(r"\b\d{4,5}[-.\s]\d{4}\b")

#: A URL or a bare domain. ``post_outcome`` points at the app and the channel the
#: customer already has, and never reads an address out loud.
_WEB_ADDRESS = re.compile(r"https?://|\bwww\.|\b[a-z0-9-]+\.com(?:\.br)?\b")

#: Money and percentages in the raw approved text. Amounts may reach a customer
#: only through a declared slot, so any of these in a template is an amount the
#: file itself invented (CONTRACT §12).
_UNSLOTTED_FIGURE = re.compile(r"R\$|\d+\s*%|\bpor\s+cento\b", re.IGNORECASE)

#: FDCPA §807(11). Verbatim, because a paraphrased mandated disclosure is a
#: missing mandated disclosure.
MINI_MIRANDA = (
    "Esta é uma tentativa de cobrança de uma dívida e qualquer informação "
    "obtida será utilizada para esse fim."
)

#: The concession boundary, stated to every customer as a fact about the agent.
CAPABILITY_STATEMENT = (
    "Não consigo oferecer descontos, abatimentos ou condições diferentes das "
    "que acabei de listar."
)

#: Where the payment link goes. The customer reads the channel back; the agent
#: never speaks an address, a number or a URL.
CHANNEL_POINTER = "canal em que você recebeu"


def _section(name: str, body: str, info: str = "spoken") -> str:
    """One ``## <heading>`` section carrying one fenced block."""
    return f"## {name}\n\n```{info}\n{body}\n```\n"


def _sections(**replace: str | None) -> list[str]:
    """Every step, with named steps replaced by a custom section or dropped.

    ``_sections(confirm_terms=None)`` omits ``confirm_terms`` entirely;
    ``_sections(confirm_terms=_section("confirm_terms", "", ...))`` replaces it.
    """
    out: list[str] = []
    for step in Step:
        if step.value in replace:
            section = replace[step.value]
            if section is not None:
                out.append(section)
        else:
            out.append(_section(step.value, f"Approved text for {step.value}."))
    return out


def _protocol_text(
    sections: list[str], *, header: str = VERSION_HEADER, front_matter: str = ""
) -> str:
    return "\n".join([front_matter, header, "# Test protocol", "", *sections])


# ---------------------------------------------------------------------------
# The protocol this repository actually ships
# ---------------------------------------------------------------------------


def test_the_shipped_protocol_covers_every_step_with_non_empty_text(
    real_protocol: Protocol,
) -> None:
    """If this fails, the agent cannot start. That is the intended behaviour."""
    for step in Step:
        assert real_protocol.text_for(step).strip(), step.value


def test_the_shipped_protocol_declares_its_version(real_protocol: Protocol) -> None:
    """Stamped onto every record, so any call can be replayed against its text."""
    assert real_protocol.version == "1.0.0"


def test_reviewer_notes_are_never_returned_as_approved_text(
    real_protocol: Protocol,
) -> None:
    """Only ``spoken`` fences are approved utterances.

    The separation is what lets a compliance reviewer explain *why* the wording
    is what it is, in the same document, with no risk of the explanation being
    read to a customer.
    """
    for step in Step:
        text = real_protocol.text_for(step)
        assert "Reviewer note" not in text
        assert "BLUEPRINT" not in text


def test_the_capability_statement_is_universal_and_appears_exactly_once(
    real_protocol: Protocol,
) -> None:
    """It lives in ``offer_payment_path`` and nowhere else, and that is load-bearing.

    This is the direct heir of the healthcare protocol's emergency disclaimer
    test, and it survives because the argument survives intact. Delivered
    unconditionally, as part of the list of options every customer hears, the
    sentence is a **capability statement**: a fact about what this agent can do,
    true before the customer has said anything the agent could react to. The
    identical sentence delivered *because* the customer asked for a discount is
    a response to a negotiating position — customer-specific handling of a
    concession request, which is precisely the authority the agent does not have
    (CONTRACT §11, BLUEPRINT §5). Nothing downstream may repeat it, condition it,
    or vary it.

    ``asks_for_discount`` in the golden set is this test's runtime twin: the
    customer asks three times, escalating, and the correct behaviour is this
    block, unchanged, each time. If the sentence existed in a second block —
    a softer variant for ``capture_commitment``, say — "unchanged each time"
    would stop being checkable by string equality, and the trap case would go
    quiet without going green.
    """
    carrying = [
        step for step in Step if CAPABILITY_STATEMENT in real_protocol.text_for(step)
    ]
    assert carrying == [Step.OFFER_PAYMENT_PATH]

    # A second, *reworded* concession sentence elsewhere would slip past an
    # exact-match scan, so the vocabulary is checked too.
    elsewhere = [
        step
        for step in Step
        if step is not Step.OFFER_PAYMENT_PATH
        and any(
            word in real_protocol.text_for(step).lower()
            for word in ("desconto", "abatimento", "acordo")
        )
    ]
    assert elsewhere == []


def test_the_mini_miranda_is_stated_once_and_sits_behind_the_identity_gate(
    real_protocol: Protocol,
) -> None:
    """FDCPA §807(11), verbatim, in ``disclose_and_consent`` and nowhere else.

    **Why not the first step, which is where healthcare put its disclaimer.**
    Protocol 1.1.0 one industry over moved the emergency disclaimer *in front of*
    the identity gate, because "every caller hears it" was false while it sat
    behind one. Collections inverts that and must: §805(b) makes disclosing the
    debt to a third party the headline harm, so the sentence naming this as a
    debt collection cannot be said until the gate has proved who is listening.
    The two protocols disagree about placement because they disagree about what
    a wrong party overhearing costs, and both are reasoning from the same rule —
    put the sentence where its claim is true.

    ``verify_right_party`` therefore says only that there is "uma pendência na
    sua conta conosco" and stops, which is the mirror of healthcare's "an
    appointment you have scheduled with us".
    """
    carrying = [step for step in Step if MINI_MIRANDA in real_protocol.text_for(step)]
    assert carrying == [Step.DISCLOSE_AND_CONSENT]

    steps = list(Step)
    assert steps.index(Step.DISCLOSE_AND_CONSENT) > steps.index(Step.VERIFY_RIGHT_PARTY)

    opening = real_protocol.text_for(Step.VERIFY_RIGHT_PARTY)
    assert "uma pendência na sua conta conosco" in opening
    assert "dívida" not in opening.lower()


def test_the_protocol_speaks_no_phone_number_and_no_payment_link(
    real_protocol: Protocol,
) -> None:
    """Customer-specific and channel-specific values would be fabricated values.

    Every block but ``state_balance`` is customer-independent and read verbatim,
    so the agent points at the channel the customer already received the notice
    on rather than speaking a number, an address or a URL it cannot verify. A
    payment link confidently read out of an approved-text file that turns out to
    be wrong is a customer sending money to a stranger — which is also, exactly,
    what a collections phishing call sounds like.

    The figure check is the collections-specific half. CONTRACT §12 allows no
    amount and no percentage in the file except through a declared slot, so the
    raw templates are searched for ``R$``, a percentage and "por cento". They
    come back clean not because the agent never says an amount — it says one at
    ``state_balance`` — but because that amount reaches the text through
    :meth:`~trail.protocol.Protocol.render` from the system of record, and
    ``text_for`` returns the unrendered ``{balance}``.
    """
    for step in Step:
        text = real_protocol.text_for(step)
        assert not _CLOCK_TIME.search(text), step.value
        assert not _PHONE_NUMBER.search(text), step.value
        assert not _WEB_ADDRESS.search(text), step.value
        assert not _UNSLOTTED_FIGURE.search(text), step.value

    assert CHANNEL_POINTER in real_protocol.text_for(Step.CONFIRM_CONTACT)
    assert CHANNEL_POINTER in real_protocol.text_for(Step.POST_OUTCOME)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_approved_text_is_returned_verbatim_including_its_paragraphs(
    fake_protocol: Protocol,
) -> None:
    assert fake_protocol.text_for(Step.DISCLOSE_AND_CONSENT) == (
        "Sou um assistente automático e esta ligação está sendo gravada.\n\n"
        "Você autoriza que eu continue?"
    )


def test_a_heading_that_does_not_name_a_step_is_prose_for_humans(
    fake_protocol: Protocol,
) -> None:
    """``## not_a_step`` carries a ``spoken`` block and is still never spoken."""
    approved = {fake_protocol.text_for(step) for step in Step}
    assert not any("não é um Step" in text for text in approved)


def test_the_version_is_read_from_the_header_comment(
    write_protocol: Callable[[str], Path],
) -> None:
    protocol = load_protocol(write_protocol(_protocol_text(_sections())))
    assert protocol.version == "9.9.9"


def test_the_loader_returns_the_same_protocol_for_the_same_path(
    write_protocol: Callable[[str], Path],
) -> None:
    """Approved content mounted into a container cannot change under the process."""
    path = write_protocol(_protocol_text(_sections()))
    assert load_protocol(path) is load_protocol(path)


def test_a_loaded_protocol_cannot_be_edited_at_runtime(
    fake_protocol: Protocol,
) -> None:
    """Immutability is what makes ``protocol_version`` on a record sufficient."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        fake_protocol.version = "tampered"  # type: ignore[misc]
    with pytest.raises(TypeError):
        fake_protocol.texts[Step.CONFIRM_TERMS] = "tampered"  # type: ignore[index]


# ---------------------------------------------------------------------------
# Malformed protocols must stop the boot
# ---------------------------------------------------------------------------


def test_a_missing_version_header_refuses_to_load(
    write_protocol: Callable[[str], Path],
) -> None:
    path = write_protocol(_protocol_text(_sections(), header="# no version here"))

    with pytest.raises(ValueError, match="missing version header"):
        load_protocol(path)


def test_a_front_matter_version_that_disagrees_refuses_to_load(
    write_protocol: Callable[[str], Path],
) -> None:
    """A half-applied compliance edit stops a boot rather than mislabelling records."""
    path = write_protocol(
        _protocol_text(_sections(), front_matter='---\nversion: "1.2.3"\n---')
    )

    with pytest.raises(ValueError, match="disagrees with"):
        load_protocol(path)


def test_a_step_with_no_approved_text_refuses_to_load_and_names_it(
    write_protocol: Callable[[str], Path],
) -> None:
    """There is no fallback text and no default. A gap is a refusal to start."""
    path = write_protocol(_protocol_text(_sections(confirm_terms=None)))

    with pytest.raises(ValueError, match="confirm_terms") as raised:
        load_protocol(path)
    assert "must not improvise" in str(raised.value)


def test_an_empty_approved_block_counts_as_no_approved_text(
    write_protocol: Callable[[str], Path],
) -> None:
    path = write_protocol(
        _protocol_text(_sections(capture_commitment=_section("capture_commitment", "")))
    )

    with pytest.raises(ValueError, match="capture_commitment"):
        load_protocol(path)


def test_a_block_fenced_with_another_info_string_is_not_approved_text(
    write_protocol: Callable[[str], Path],
) -> None:
    """Only ```spoken`` is approved. An example block is prose, not a script."""
    path = write_protocol(
        _protocol_text(
            _sections(
                post_outcome=_section(
                    "post_outcome", "An illustrative example.", info="text"
                )
            )
        )
    )

    with pytest.raises(ValueError, match="post_outcome"):
        load_protocol(path)


def test_two_approved_blocks_for_one_step_refuse_to_load(
    write_protocol: Callable[[str], Path],
) -> None:
    """Which one would be spoken? The question has no safe answer."""
    doubled = _section("confirm_terms", "First wording.") + _section(
        "confirm_terms", "Second wording."
    )
    path = write_protocol(_protocol_text(_sections(confirm_terms=doubled)))

    with pytest.raises(ValueError, match="2 `spoken` blocks"):
        load_protocol(path)


def test_an_approved_block_before_any_step_heading_refuses_to_load(
    write_protocol: Callable[[str], Path],
) -> None:
    path = write_protocol(
        "\n".join(
            [
                VERSION_HEADER,
                "```spoken",
                "Text belonging to no step at all.",
                "```",
                "",
                *_sections(),
            ]
        )
    )

    with pytest.raises(ValueError, match="belongs to no step"):
        load_protocol(path)


def test_an_unterminated_block_refuses_to_load(
    write_protocol: Callable[[str], Path],
) -> None:
    path = write_protocol(
        _protocol_text(_sections())
        + "\n## confirm_contact\n\n```spoken\nNever closed.\n"
    )

    with pytest.raises(ValueError, match="unterminated"):
        load_protocol(path)


def test_a_missing_file_is_not_silently_tolerated(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_protocol(tmp_path / "no-such-protocol.md")


def test_a_protocol_built_by_hand_still_refuses_to_invent_a_missing_step() -> None:
    """``text_for`` has no fallback of its own; the loader is not the only guard."""
    hand_built = Protocol(version="hand-built", texts={Step.VERIFY_RIGHT_PARTY: "Olá."})

    assert hand_built.text_for(Step.VERIFY_RIGHT_PARTY) == "Olá."
    with pytest.raises(KeyError, match="no approved text for step 'confirm_terms'"):
        hand_built.text_for(Step.CONFIRM_TERMS)
