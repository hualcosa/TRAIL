"""The built-in demo account: one of it, and independent of the clock.

``trail chat`` with no ``--case`` and ``GET /demo/cases`` both offer this
account, and they must offer the *same* account — a demo whose terminal and
whose browser disagree about what the customer owes is worse than no demo,
because the disagreement looks like a bug in the agent rather than in the
fixture. There is one function, so there is one account; these tests hold the
properties that make it usable at all.

The clock-independence tests are the substantive ones. ``days_past_due`` is
validated at 1..30, so an account whose due date is a constant and whose day
count is computed against ``date.today()`` works until it silently does not, and
in the meantime demonstrates a different customer every morning.
"""

from __future__ import annotations

from datetime import date

import pytest

from trail.agent.machine import identity_matches, slots_for_call
from trail.cases import demo_profile
from trail.models import Step, TurnExtraction
from trail.money import is_valid_cpf

pytestmark = pytest.mark.unit


def test_the_demo_account_is_the_same_account_every_time() -> None:
    """Two calls, one customer. The function is the single definition of it."""
    assert demo_profile() == demo_profile()


def test_no_field_of_the_demo_account_moves_with_the_calendar() -> None:
    """Pinned by value, not by "today minus eighteen days".

    The pair that has to agree is ``due_date`` and ``days_past_due``: both are
    rendered into the one approved block that speaks numbers aloud, so a profile
    saying "venceu em 28 de julho" and "40 dias" would be a wrong fact spoken to
    a customer. Deriving either from the clock guarantees that drift.
    """
    profile = demo_profile()

    assert profile.due_date == date(2026, 7, 28)
    assert profile.days_past_due == 18
    assert profile.date_of_birth == date(1979, 3, 11)


def test_the_demo_account_stays_inside_the_segment_that_is_the_scope() -> None:
    """1–30 DPD is the product scope (BLUEPRINT §3), enforced by the type.

    Asserted here as well because the failure it prevents arrives as a
    ``ValidationError`` from a demo command rather than as a wrong number, and a
    demo that cannot construct its own customer is the least legible way to
    discover that a constant went stale.
    """
    assert 1 <= demo_profile().days_past_due <= 30


def test_the_demo_cpf_passes_its_own_check_digits() -> None:
    """Invented, and invented deliberately.

    :func:`~trail.agent.machine.identity_matches` runs
    :func:`~trail.money.is_valid_cpf` as a condition separate from the equality
    test, so a demo CPF mashed on a keyboard would send every demo call to
    ``not_right_party`` — the correct behaviour, for a reason no assertion
    message would name.
    """
    assert is_valid_cpf(demo_profile().tax_id)


def test_the_demo_customer_can_pass_the_identity_gate() -> None:
    """The whole point of the account: a demo has to get past step one.

    The gate is deterministic and compares what the caller stated against the
    booked account, so this is the fixture proving it is internally consistent —
    surname present in the booked name, CPF matching and checksummed.
    """
    profile = demo_profile()
    stated = TurnExtraction(
        step=Step.VERIFY_RIGHT_PARTY,
        raw_utterance="Beatriz Almeida Nogueira, CPF 308.417.529-23",
        understood=True,
        stated_name=profile.full_name,
        stated_tax_id="308.417.529-23",
    )

    assert identity_matches(profile, stated) is True


def test_the_demo_account_renders_every_slot_the_balance_block_declares() -> None:
    """The account exists to be spoken, so it has to survive the formatters.

    :func:`~trail.agent.machine.slots_for_call` raises on a non-positive day
    count and on a product with no approved spoken name, which is the correct
    behaviour and a poor thing to discover live in a demo.
    """
    slots = slots_for_call(demo_profile())

    assert set(slots) == {"product", "balance", "due_date", "days_past_due"}
    assert all(value for value in slots.values())
