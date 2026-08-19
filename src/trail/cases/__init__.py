"""The golden set — synthetic Banco Aurora customers, fixed before building.

One versioned module per set. :data:`GOLDEN_SET` and :data:`GOLDEN_SET_VERSION`
re-export the current one, so callers name the set they want by importing
``trail.cases`` and never by reaching into a version module. A new set is a new
module with a new version stamp; the old one stays on disk, unedited, so an old
run remains reproducible.

Customer turns are scripted Brazilian Portuguese, not LLM-generated
(BLUEPRINT §6): an LLM customer and an LLM agent are drawn from the same
distribution, share their assumptions about what a reasonable answer looks like,
and conspire toward success. See :mod:`trail.cases.golden_v1` for the
conventions the set commits to — how a spoken amount and a spoken date are
transcribed into fields without being normalised, what separates a fully
automated completion from one needing a specialist callback without any rule
reading how much was promised, and why the 40% ceiling this set implies is a
ceiling and not a prediction.

:func:`demo_profile` lives here for the same reason the golden set does: it is a
synthetic customer, and there must be exactly one of it. The CLI's ``trail
chat`` and the agent service's ``GET /demo/cases`` both call this function, so
the account the demo UI offers and the account the terminal demo opens are the
same account. Two copies would drift — a balance edited in one place and not the
other — and the difference would surface as a screenshot that disagrees with a
transcript, which is the least debuggable form of the same fact.
"""

from datetime import date
from decimal import Decimal

from trail.cases.golden_v1 import GOLDEN_SET, GOLDEN_SET_VERSION
from trail.models import AccountProfile, Product

__all__ = ["GOLDEN_SET", "GOLDEN_SET_VERSION", "demo_profile"]


def demo_profile() -> AccountProfile:
    """The synthetic account used by ``trail chat`` when no ``--case`` is given.

    **Every field is a fixed constant, and none of them is derived from the
    clock.** A demo profile built with ``date.today()`` looks tidier and is a
    trap: ``days_past_due`` is validated at 1..30 (the segment *is* the scope,
    BLUEPRINT §3), so a hardcoded due date minus a moving today drifts out of
    the window and one morning ``trail chat`` stops constructing its own
    profile. Worse, until it broke it would quietly demo a different account
    every day, and a demo that is not reproducible cannot be compared to the
    run somebody screenshotted last week. 18 days past due against a due date
    of 2026-07-28 is a fact about this fixture, not about when it was run.

    Entirely fictional, like everything else about Banco Aurora. The CPF is
    invented and passes its own check digits (``trail.money.is_valid_cpf``),
    which is what the identity gate requires and is also why it must be
    invented deliberately rather than mashed on a keyboard. Brazil publishes no
    fiction-reserved telephone range the way the NANP reserves 555-01xx, so the
    number below is simply made up and labelled as such — the alternative was a
    confident claim this repository could not support.
    """
    return AccountProfile(
        account_id="BA-DEMO-001",
        full_name="Beatriz Almeida Nogueira",
        tax_id="30841752923",
        date_of_birth=date(1979, 3, 11),
        phone="+55 11 98000-0142",
        product=Product.PERSONAL_LOAN,
        balance_brl=Decimal("847.32"),
        due_date=date(2026, 7, 28),
        days_past_due=18,
    )
