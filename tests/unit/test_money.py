"""The deterministic primitives: rendering money and dates, parsing, CPF.

Two guarantees are under test here and they pull in opposite directions, which
is why they live in one file.

**Rendering is exact and machine-independent.** :func:`~trail.money.format_brl`
and :func:`~trail.money.format_date_ptbr` produce the only customer-specific
text the agent is ever allowed to speak, and the compliance allowlist compares
against their output. If either one's answer depended on a process locale, an
installed system locale or a rounding mode nobody wrote down, "we never speak a
wrong balance" would be a hope rather than a check. So the assertions below are
literal strings, not round trips through another formatter.

**Parsing refuses.** :func:`~trail.money.parse_brl` is scorer infrastructure,
never agent infrastructure, and about half of this file asserts that it returns
``None``. That is the substance of the module, not its error handling: "uns
oitocentos" is not eight hundred, "1200.50" is not a pt-BR amount, and a parser
that resolved either of them would fabricate exactly the kind of value the
entity-accuracy metric exists to catch. Each of those refusals is written down as
a named test so that a future convenience — "just strip the dots", "just take the
first number" — has to delete an assertion to land.

Everything here is pure: no fixtures, no I/O, no clock, no model.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from trail.models import Product
from trail.money import (
    format_brl,
    format_date_ptbr,
    format_days_past_due,
    format_product_ptbr,
    is_valid_cpf,
    parse_brl,
)

pytestmark = pytest.mark.unit


# Synthetic and checksum-valid, as CONTRACT §9 requires of every CPF in the
# repository. Neither belongs to anyone: 111.444.777-35 is the textbook worked
# example of the check-digit algorithm, and 123.456.789-09 is the sequence used
# as the disclosure-scanner fixture in CONTRACT §11.
VALID_CPF = "11144477735"
VALID_CPF_SEQUENTIAL = "12345678909"


# ---------------------------------------------------------------------------
# format_brl — the amount the agent speaks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("847.32", "R$ 847,32"),
        ("1200.5", "R$ 1.200,50"),
        ("0", "R$ 0,00"),
        ("0.05", "R$ 0,05"),
        ("120", "R$ 120,00"),
        ("999.99", "R$ 999,99"),
        ("1000", "R$ 1.000,00"),
        ("6000", "R$ 6.000,00"),
        ("12345.67", "R$ 12.345,67"),
        ("1000000", "R$ 1.000.000,00"),
    ],
)
def test_an_amount_renders_in_pt_br_convention(value: str, expected: str) -> None:
    """``.`` groups thousands and ``,`` opens the decimal — the Anglo inverse.

    Getting this backwards does not produce a formatting complaint, it produces
    R$ 1,20 spoken to a customer who owes R$ 1.200,00, which BLUEPRINT §5 lists as
    a zero-tolerance failure.
    """
    assert format_brl(Decimal(value)) == expected


def test_cents_are_always_spoken_even_when_they_are_zero() -> None:
    """A balance is read to a person, and "mil e duzentos reais" with the cents
    silently dropped is a different statement from the record's R$ 1.200,00."""
    assert format_brl(Decimal("1200")) == "R$ 1.200,00"


def test_rounding_is_half_up_and_not_pythons_banker_default() -> None:
    """Decimal's default is ROUND_HALF_EVEN, which sends 847.325 down to 847.32.

    The record holds exact cents, so in practice this branch only ever pads. Where
    it does round it rounds the way the customer reading their own statement
    expects, and the default being different is exactly why it is pinned here.
    """
    assert format_brl(Decimal("847.325")) == "R$ 847,33"
    assert format_brl(Decimal("847.334")) == "R$ 847,33"


def test_a_negative_amount_keeps_its_sign() -> None:
    """The 1–30 DPD segment cannot produce one — a credit balance is not in
    arrears — but dropping the sign would be a wrong amount spoken aloud, and
    silence about a case is not the same as refusing it."""
    assert format_brl(Decimal("-5")) == "-R$ 5,00"


# ---------------------------------------------------------------------------
# format_date_ptbr — the due date the agent speaks
# ---------------------------------------------------------------------------


def test_a_date_renders_in_long_pt_br_form() -> None:
    assert format_date_ptbr(date(2026, 8, 20)) == "20 de agosto de 2026"


@pytest.mark.parametrize(
    ("month", "name"),
    [
        (1, "janeiro"),
        (2, "fevereiro"),
        (3, "março"),
        (4, "abril"),
        (5, "maio"),
        (6, "junho"),
        (7, "julho"),
        (8, "agosto"),
        (9, "setembro"),
        (10, "outubro"),
        (11, "novembro"),
        (12, "dezembro"),
    ],
)
def test_every_month_has_its_pt_br_name(month: int, name: str) -> None:
    """Spelled out in the test as well as in the module, deliberately.

    ``strftime("%B")`` would pass this test on a machine with a pt-BR locale
    installed and fail on one without, which is the failure mode the literal table
    exists to remove. A second literal table here is the only way to check the
    first one without re-deriving it from the same source.
    """
    assert format_date_ptbr(date(2026, month, 15)) == f"15 de {name} de 2026"


def test_the_day_is_not_zero_padded() -> None:
    """This string is the input to a speech engine, not a log line."""
    assert format_date_ptbr(date(2026, 1, 1)) == "1 de janeiro de 2026"
    assert format_date_ptbr(date(2026, 12, 31)) == "31 de dezembro de 2026"


def test_a_leap_day_renders_like_any_other_day() -> None:
    assert format_date_ptbr(date(2028, 2, 29)) == "29 de fevereiro de 2028"


# ---------------------------------------------------------------------------
# parse_brl — digit forms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("R$ 1.200,50", "1200.50"),
        ("R$1.200,50", "1200.50"),
        ("1200,50", "1200.50"),
        ("1.200", "1200"),
        ("847", "847"),
        ("0,05", "0.05"),
        ("1.000.000,00", "1000000.00"),
        ("6.000,00", "6000.00"),
    ],
)
def test_a_written_amount_parses(text: str, expected: str) -> None:
    """The forms a customer types, or an ASR emits when it has already
    normalised digits for us."""
    assert parse_brl(text) == Decimal(expected)


def test_the_anglo_decimal_form_is_refused_rather_than_guessed() -> None:
    """``1200.50`` under the pt-BR grouping rule is a malformed thousands group.

    It is also the single most tempting thing to accept, and accepting it means
    choosing between R$ 1.200,50 and R$ 1,20 on somebody's debt. That is the
    8-becomes-80 error one industry over, and the correct answer is to score the
    utterance as unparsed and let the number be lower.
    """
    assert parse_brl("1200.50") is None


@pytest.mark.parametrize("text", ["1.2", "1.20", "1.2000", "12.34", "1234.567"])
def test_a_dot_group_that_is_not_three_digits_is_not_a_thousands_separator(
    text: str,
) -> None:
    """The grouping is enforced, which is what makes the refusal above principled
    rather than a special case for one string."""
    assert parse_brl(text) is None


def test_more_than_two_decimal_places_is_not_money() -> None:
    assert parse_brl("847,325") is None


def test_a_leading_minus_is_not_read_as_a_sign() -> None:
    """:func:`format_brl` emits one; this deliberately does not read it back.

    A customer does not say a negative amount, so a ``-`` in a transcript is far
    more likely a hyphen artefact than a sign, and treating punctuation noise as a
    value is the asymmetry this module is willing to live with.
    """
    assert parse_brl("-R$ 5,00") is None


# ---------------------------------------------------------------------------
# parse_brl — spelled-out forms, which is what a transcript actually contains
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("quinhentos", "500"),
        ("mil e duzentos", "1200"),
        ("mil", "1000"),
        ("cem", "100"),
        ("cento e cinquenta", "150"),
        ("dezenove", "19"),
        ("oitocentos e quarenta e sete", "847"),
        ("novecentos e noventa e nove", "999"),
        ("dois mil", "2000"),
        ("duzentos mil e quinhentos", "200500"),
        ("cem mil", "100000"),
        ("um milhao e duzentos mil", "1200000"),
        ("duzentas", "200"),
    ],
)
def test_a_spelled_out_numeral_parses(text: str, expected: str) -> None:
    """Additive within a group of three digits, multiplicative across scales.

    The feminine forms are here because a transcript carries the speaker's
    grammar, not the parser's.
    """
    assert parse_brl(text) == Decimal(expected)


def test_accents_do_not_decide_whether_an_amount_parses() -> None:
    """The same word reaches the scorer with and without its accent depending on
    the ASR, and which of the two arrived says nothing about the number."""
    assert parse_brl("três mil") == parse_brl("tres mil") == Decimal("3000")
    assert parse_brl("um milhão") == parse_brl("um milhao") == Decimal("1000000")


def test_case_is_not_meaningful_either() -> None:
    assert parse_brl("MIL E DUZENTOS") == Decimal("1200")


def test_a_dropped_conjunction_between_words_still_parses() -> None:
    """An ASR that swallows the unstressed "e" has not changed the number.

    "quarenta cinco" is not how anyone writes forty-five, but the token sequence
    admits exactly one reading, so refusing it would cost a point of measured
    accuracy to punish the transcriber rather than the model. Contrast
    ``test_a_word_sequence_that_is_not_a_numeral_returns_none``: those sequences
    admit no reading at all, which is a different thing entirely.
    """
    assert parse_brl("quarenta cinco") == Decimal("45")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("847 reais e 32 centavos", "847.32"),
        ("847 reais", "847"),
        ("847 reais e 5 centavos", "847.05"),
        (
            "oitocentos e quarenta e sete reais e trinta e dois centavos",
            "847.32",
        ),
        ("mil e duzentos reais", "1200"),
        ("cem reais", "100"),
        ("trinta e dois centavos", "0.32"),
        ("um real", "1"),
        (
            "tres mil e novecentos e noventa e nove reais e noventa e nove centavos",
            "3999.99",
        ),
    ],
)
def test_reais_and_centavos_compose_into_one_amount(text: str, expected: str) -> None:
    """The unit words are delimiters, and the two halves may each be digits or
    words — "847 reais e trinta e dois centavos" is a real transcript."""
    assert parse_brl(text) == Decimal(expected)


def test_digits_and_words_may_be_mixed_across_the_two_halves() -> None:
    assert parse_brl("847 reais e trinta e dois centavos") == Decimal("847.32")
    assert parse_brl("oitocentos e quarenta e sete reais e 32 centavos") == Decimal(
        "847.32"
    )


# ---------------------------------------------------------------------------
# parse_brl — the refusals. This section is the point of the function.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "abc",
        "muito dinheiro",
        "R$",
        "reais",
        "centavos",
        "reais e centavos",
    ],
)
def test_text_that_names_no_number_returns_none(text: str) -> None:
    assert parse_brl(text) is None


@pytest.mark.parametrize("text", ["uns oitocentos", "quase mil", "mais ou menos cem"])
def test_a_hedged_amount_is_not_an_amount(text: str) -> None:
    """A person says "uns oitocentos" precisely when they do not mean exactly
    eight hundred, so resolving it to 800 would invent a promise they declined to
    make. In a scorer, an unparsed utterance costs one point; a guessed one
    corrupts the metric it feeds."""
    assert parse_brl(text) is None


@pytest.mark.parametrize(
    "text",
    ["vinte e trinta", "duzentos trezentos", "dez e um", "cem e um", "sete oito"],
)
def test_a_word_sequence_that_is_not_a_numeral_returns_none(text: str) -> None:
    """A bare left fold that sums every word it recognises answers fifty for
    "vinte e trinta" — a number nobody said, produced with full confidence.

    That is why the fold enforces the grammar: one hundreds word, one
    tens-or-teens word and one units word per group, in descending magnitude, with
    "cem" (as against "cento") admitting no continuation.
    """
    assert parse_brl(text) is None


@pytest.mark.parametrize("text", ["mil mil", "dois mil mil", "mil milhoes e mil"])
def test_a_scale_may_not_repeat_or_grow(text: str) -> None:
    """Scales run strictly downwards. "mil mil" is a stutter, not a million."""
    assert parse_brl(text) is None


@pytest.mark.parametrize("text", ["duzentos e", "e duzentos", "mil e e duzentos"])
def test_a_dangling_conjunction_is_a_truncated_transcript(text: str) -> None:
    """The conjunction is structural in Portuguese numerals, so a phrase that
    opens or closes on one has lost a word. Parsing "duzentos e" as two hundred
    would supply the half the ASR dropped."""
    assert parse_brl(text) is None


def test_cents_beyond_two_digits_are_refused() -> None:
    """The phrase "cento e cinquenta centavos" is well-formed Portuguese and
    unambiguous arithmetic, and nobody states a balance that way. The centavos
    field is two digits; anything wider is a transcript this parser has no
    confident reading of."""
    assert parse_brl("cento e cinquenta centavos") is None


@pytest.mark.parametrize(
    "text",
    [
        "1.200,50 reais e 30 centavos",
        # The one that got through the first implementation. Its reais half is
        # numerically integral, so a test written as "the whole part has a
        # fraction" passes it — and the parser silently resolved the conflict in
        # favour of the second figure. The refusal has to key on whether the
        # speaker *wrote* a decimal group, not on what that group evaluated to.
        "847,00 reais e 30 centavos",
        "1200,00 reais e 99 centavos",
    ],
)
def test_an_utterance_that_states_its_cents_twice_is_refused(text: str) -> None:
    """There is no reading of "1.200,50 reais e 30 centavos" that is not a
    choice between two numbers the speaker gave."""
    assert parse_brl(text) is None


def test_trailing_sentence_punctuation_is_not_part_of_the_number() -> None:
    """Stripping it is safe against the thousands separator, because a pt-BR
    amount ends in a digit."""
    assert parse_brl("quinhentos.") == Decimal("500")
    assert parse_brl("R$ 1.200,50.") == Decimal("1200.50")
    assert parse_brl("1.200") == Decimal("1200")


# ---------------------------------------------------------------------------
# The round trip — what the agent speaks is what the scorer reads back
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "0",
        "0.05",
        "1",
        "9.99",
        "120",
        "847.32",
        "1200.5",
        "3999.99",
        "6000",
        "12345.67",
        "1000000",
    ],
)
def test_a_rendered_amount_parses_back_to_itself(value: str) -> None:
    """The two halves of the module have to agree on the convention.

    A customer who reads the balance back exactly as the agent rendered it must
    score as a correct restatement. If the formatter and the parser disagreed
    about where the dots go, ``terms_confirmation_rate`` would fall for a reason
    that has nothing to do with the customer or the model — a measurement bug
    reported as a quality result, which is the failure this repository is least
    willing to publish.
    """
    original = Decimal(value)
    assert parse_brl(format_brl(original)) == original


def test_the_round_trip_holds_across_the_1_to_30_dpd_balance_range() -> None:
    """CONTRACT §15 puts every golden-set balance between R$ 120 and R$ 6.000, so
    that is the range where a separator bug would actually be met in a run."""
    value = Decimal("120.00")
    while value <= Decimal("6000.00"):
        assert parse_brl(format_brl(value)) == value
        value += Decimal("137.13")


# ---------------------------------------------------------------------------
# is_valid_cpf — the second identifier on the right-party gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cpf", [VALID_CPF, VALID_CPF_SEQUENTIAL, "52998224725"])
def test_a_checksum_valid_cpf_is_accepted(cpf: str) -> None:
    assert is_valid_cpf(cpf) is True


def test_a_wrong_first_check_digit_is_rejected() -> None:
    """Digit ten is the weighted sum of the first nine. Change it and the number
    is not a CPF, however plausible it looks written down."""
    assert is_valid_cpf("12345678919") is False


def test_a_wrong_second_check_digit_is_rejected() -> None:
    """Digit eleven is checked independently of digit ten, so a number that
    passes the first test still has to pass the second."""
    assert is_valid_cpf("12345678900") is False


@pytest.mark.parametrize(
    "cpf", ["00000000000", "11111111111", "55555555555", "99999999999"]
)
def test_a_repeated_digit_cpf_is_rejected_even_though_it_checksums(cpf: str) -> None:
    """This is the reason the rule exists, and it is worth being explicit.

    Every ``ddddddddddd`` satisfies both check digits — the weighted sums are
    proportional to the repeated digit, so the arithmetic can never object. They
    are placeholder values, universally treated as invalid in Brazil, and a
    right-party gate that accepted one would be verifying identity against a
    string any caller can produce while knowing nothing at all about the account.
    """
    assert is_valid_cpf(cpf) is False


@pytest.mark.parametrize(
    "cpf", ["", "1", "1114447773", "111444777350", "1114447773500"]
)
def test_a_cpf_of_the_wrong_length_is_rejected(cpf: str) -> None:
    """Length is checked before the checksum so a truncated transcript can never
    index past the end of a shorter string and raise instead of answering."""
    assert is_valid_cpf(cpf) is False


@pytest.mark.parametrize(
    "cpf",
    [
        "111.444.777-35",
        "111 444 777 35",
        "1114447773a",
        " 11144477735",
        "11144477735 ",
        "١١١٤٤٤٧٧٧٣٥",
    ],
)
def test_anything_that_is_not_eleven_bare_ascii_digits_is_rejected(cpf: str) -> None:
    """Separators are the caller's problem, and that asymmetry is deliberate.

    ``identity_matches`` strips to digits before calling, so this function never
    has to guess whether an unexpected character was formatting or a transcription
    error — exactly one place in the codebase decides what a digit is. The last
    case is Arabic-Indic digits, which ``str.isdigit`` and ``\\d`` both accept and
    the explicit ``[0-9]`` class does not; a Brazilian national identifier written
    in another script is not a value the rest of the system can compare against
    the booked one.
    """
    assert is_valid_cpf(cpf) is False


def test_the_formatted_spelling_of_a_valid_cpf_is_still_rejected_here() -> None:
    """Stated as its own case because it is the one that looks like a bug.

    ``"111.444.777-35"`` is a perfectly valid CPF and this function says ``False``
    for it. That is correct at this layer and correct at the call site, which
    strips first. Making the validator tolerant would move the stripping decision
    to wherever anyone happened to need it next.
    """
    assert is_valid_cpf(VALID_CPF) is True
    assert is_valid_cpf("111.444.777-35") is False


# ---------------------------------------------------------------------------
# format_product_ptbr and format_days_past_due
# ---------------------------------------------------------------------------
#
# The other two slot renderers. They exist so that
# `protocol/collections_1_30_dpd.md` never has to branch: a template that said
# "dia(s)" would have a speech synthesiser voice the parentheses, and a template
# that switched on the product would be a template a compliance reviewer has to
# simulate rather than read.


@pytest.mark.parametrize(
    ("product", "expected"),
    [
        (Product.PERSONAL_LOAN, "empréstimo pessoal"),
        (Product.CREDIT_CARD, "cartão de crédito"),
    ],
)
def test_each_product_has_one_customer_facing_name(
    product: Product, expected: str
) -> None:
    assert format_product_ptbr(product) == expected


def test_every_product_in_the_book_is_speakable() -> None:
    """A product the agent cannot name is a product the call cannot describe.

    Written as a sweep over the enum rather than as two assertions so that adding
    a third product to the book fails here, at the formatter, instead of failing
    at the first customer who holds one.
    """
    for product in Product:
        assert format_product_ptbr(product)


@pytest.mark.parametrize(
    ("days", "expected"),
    [(1, "1 dia"), (2, "2 dias"), (12, "12 dias"), (30, "30 dias")],
)
def test_the_day_count_agrees_with_its_noun(days: int, expected: str) -> None:
    assert format_days_past_due(days) == expected


@pytest.mark.parametrize("days", [0, -1, -30])
def test_a_non_positive_day_count_is_refused_rather_than_rendered(days: int) -> None:
    """The segment is 1–30 days past due and ``AccountProfile`` pins it with
    ``ge=1``, so a non-positive count is a broken record reaching the one step
    that speaks numbers aloud. "0 dias de atraso" said to someone who is not in
    arrears is the wrong-fact-spoken-aloud failure this module exists to prevent,
    and it is better to fail the call than to say it."""
    with pytest.raises(ValueError, match="at least 1"):
        format_days_past_due(days)


def test_the_day_count_renders_digits_and_not_words() -> None:
    """Deliberate, and the one place a reader may expect otherwise.

    Every rendered slot is read by the same synthesiser, and a file that spelled
    one number and printed another would be inconsistent in exactly the place —
    money, dates, counts — where a compliance reviewer is checking consistency.
    ``parse_brl`` is what handles a *customer* saying "doze"; the agent never has
    to say it.
    """
    assert format_days_past_due(12) == "12 dias"
    assert "doze" not in format_days_past_due(12)
