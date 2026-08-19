"""Deterministic domain primitives: Brazilian money, dates, and the CPF checksum.

Pure functions, standard library only, no I/O, no configuration. Everything here
is decided by arithmetic and a lookup table, and that is the point: these are the
values the agent is permitted to **speak**, and BLUEPRINT §5 makes "wrong balance
/ fee / date spoken aloud" a zero-tolerance failure. A slot value produced by a
model is a value nobody can reproduce; a slot value produced by :func:`format_brl`
is one a compliance reviewer can recompute from the system of record with a
calculator.

Three of these four functions render and one parses, and the split is the whole
design:

* :func:`format_brl` and :func:`format_date_ptbr` are the **only** producers of
  customer-specific spoken text in the system. ``Protocol.render`` walks the slots
  a ```spoken`` block declares and substitutes their output verbatim; the
  compliance allowlist then compares the *rendered* string. An amount that differs
  from the record therefore fails the gate before the words leave the service,
  which is what makes "we never speak a wrong balance" a property of the running
  system rather than a claim about it.
* :func:`parse_brl` is used **only by the eval scorer**, never by the agent. The
  agent records what the customer said exactly as said and never normalises it:
  "mil e duzentos" is stored as ``"mil e duzentos"``. That is the same refusal the
  healthcare original made when it declined to infer an omitted dose unit —
  normalisation is precisely where a real amount error gets absorbed into a match,
  and an agent that normalises has quietly moved the error out of the scorer's
  reach. The scorer needs a number to compare, so it parses; the agent needs
  nothing of the kind, so it does not.

:func:`parse_brl` returns ``None`` rather than a guess, and that is not
defensiveness. "uns oitocentos" ("about eight hundred") is not eight hundred, and
a parser that resolved it to ``800`` would be manufacturing exactly the class of
fabrication the scorer exists to detect. Refusing to parse costs a point of
measured entity accuracy; guessing costs the credibility of every number in the
report.

``locale`` and ``babel`` were both considered for the formatters and both
rejected. ``locale.setlocale`` is process-global, mutates state for every other
thread, and depends on a ``pt_BR.UTF-8`` locale having been generated in the
image — it would make the output of a pure function depend on the container it
runs in, and the compliance allowlist compares against that output. ``babel``
would be a dependency resolved, locked and shipped to earn one thousands
separator and twelve month names, both of which are twenty lines below.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise
from typing import Final

from trail.models import Product

__all__ = [
    "format_brl",
    "format_date_ptbr",
    "format_days_past_due",
    "format_product_ptbr",
    "is_valid_cpf",
    "parse_brl",
]


# ---------------------------------------------------------------------------
# 1. Rendering — the slot values the agent is allowed to speak
# ---------------------------------------------------------------------------

_CENTS: Final = Decimal("0.01")


def format_brl(value: Decimal) -> str:
    """Render ``value`` as Brazilian currency: ``Decimal("1200.5")`` → ``"R$ 1.200,50"``.

    pt-BR inverts the Anglo separators — ``.`` groups thousands, ``,`` opens the
    decimal — and both are always present: money is spoken to a person, so cents
    are padded rather than dropped.

    The argument is a :class:`~decimal.Decimal` and nothing else. Balances arrive
    from the system of record as exact cents and a float would introduce a
    representation error into the one number in this system that is not allowed to
    have one. Quantisation is :data:`~decimal.ROUND_HALF_UP`, not Python's default
    banker's rounding: in practice the record already holds two decimal places so
    this only ever pads, but where it does round it should round the way the
    customer reading their statement expects, and ``ROUND_HALF_EVEN`` sending
    ``847.325`` to ``847.32`` is a surprise nobody asked for.

    A negative value renders as ``"-R$ 5,00"``. The 1–30 DPD segment does not
    produce one — a credit balance is not in arrears — but silently emitting
    ``"R$ 5,00"`` for a negative would be a wrong amount spoken aloud, so the sign
    is carried rather than dropped. :func:`parse_brl` deliberately does not read it
    back; see there.
    """
    quantised = value.quantize(_CENTS, rounding=ROUND_HALF_UP)
    sign = "-" if quantised < 0 else ""
    whole, _, cents = f"{abs(quantised):.2f}".partition(".")
    # Group with the C-locale thousands separator, then swap it for the pt-BR
    # one. Doing it in this order keeps the grouping logic in the standard
    # library instead of in a hand-rolled loop over reversed digit triples.
    grouped = f"{int(whole):,}".replace(",", ".")
    return f"{sign}R$ {grouped},{cents}"


_MONTHS_PT_BR: Final[tuple[str, ...]] = (
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
)


def format_date_ptbr(value: date) -> str:
    """Render ``value`` in long pt-BR form: ``date(2026, 8, 20)`` → ``"20 de agosto de 2026"``.

    The month names are a literal table rather than ``strftime("%B")`` for the same
    reason ``locale`` is absent from this module: ``%B`` reads the process locale,
    so the identical call returns ``"August"`` on a developer's laptop and
    ``"agosto"`` in the container, and the difference surfaces as a compliance
    allowlist failure on a rendered utterance rather than as anything legible.

    The day is unpadded (``"1 de janeiro"``, not ``"01"``) because the rendered
    string is read to a customer, and the ordinal ``1º`` is deliberately not used:
    it is correct written Portuguese but it is a character a TTS engine may or may
    not voice, and this text is the input to speech.
    """
    return f"{value.day} de {_MONTHS_PT_BR[value.month - 1]} de {value.year}"


_PRODUCT_PT_BR: Final[dict[Product, str]] = {
    Product.PERSONAL_LOAN: "empréstimo pessoal",
    Product.CREDIT_CARD: "cartão de crédito",
}


def format_product_ptbr(product: Product) -> str:
    """Render ``product`` as the customer-facing name spoken in ``state_balance``.

    A closed table over a closed enum, so the customer hears one of exactly two
    phrases and both were reviewed as approved content. The mapping lives here
    rather than in ``protocol/collections_1_30_dpd.md`` for the reason the day
    count does: the template would otherwise have to branch, and a template that
    branches is a template a reviewer has to simulate rather than read.

    ``KeyError`` on an unmapped member is deliberate and is the same fail-fast
    posture as :meth:`trail.protocol.Protocol.text_for`. Adding a product to
    the book without deciding what the agent calls it out loud should stop the
    call, not improvise a name for a financial product.
    """
    return _PRODUCT_PT_BR[product]


def format_days_past_due(days: int) -> str:
    """Render an elapsed-days count with its noun: ``1`` → ``"1 dia"``, ``12`` → ``"12 dias"``.

    The noun is part of the value rather than part of the template. Portuguese
    number agreement is a deterministic function of an integer, so it belongs in a
    formatter next to the currency and date ones; the alternative is a template
    reading ``"dia(s)"`` and a speech synthesiser voicing the parentheses.

    Digits, not words, and for the same reason :func:`format_brl` emits digits:
    every rendered slot in this system is read by the same synthesiser, and a file
    that spelled one number and printed another would be inconsistent in the one
    place — money and dates — where consistency is what a compliance reviewer is
    checking. The scorer's :func:`parse_brl` is what handles the *customer* saying
    "doze"; the agent never has to.

    Negative and zero are refused rather than rendered. The segment is 1–30 days
    past due (BLUEPRINT §3) and ``AccountProfile`` pins it with ``ge=1``, so a
    non-positive count is a broken record reaching the one step that speaks
    numbers aloud — and "0 dias de atraso" spoken to someone who is not in arrears
    is exactly the wrong-fact-spoken-aloud failure this module exists to prevent.
    """
    if days < 1:
        raise ValueError(f"days_past_due must be at least 1, got {days!r}")
    return f"{days} dia" if days == 1 else f"{days} dias"


# ---------------------------------------------------------------------------
# 2. Parsing — scorer only. See the module docstring.
# ---------------------------------------------------------------------------

# pt-BR numerals, one table per magnitude class. Written without accents because
# every candidate string is accent-folded first: a transcript may arrive as
# "três" or "tres" depending on the ASR, and both mean three. That is the only
# normalisation applied to the words themselves — it cannot change which number
# is meant, which is the test every normalisation in this module has to pass.
_UNITS: Final[dict[str, int]] = {
    "zero": 0,
    "um": 1,
    "uma": 1,
    "dois": 2,
    "duas": 2,
    "tres": 3,
    "quatro": 4,
    "cinco": 5,
    "seis": 6,
    "sete": 7,
    "oito": 8,
    "nove": 9,
}

# 11–19 are irregular in Portuguese exactly as they are in English: they are not
# "ten and one", they are their own words, and a fold that tried to compose them
# from _TENS and _UNITS would accept "dez e um" — which nobody says.
_TEENS: Final[dict[str, int]] = {
    "dez": 10,
    "onze": 11,
    "doze": 12,
    "treze": 13,
    "quatorze": 14,
    "catorze": 14,
    "quinze": 15,
    "dezesseis": 16,
    "dezessete": 17,
    "dezoito": 18,
    "dezenove": 19,
}

_TENS: Final[dict[str, int]] = {
    "vinte": 20,
    "trinta": 30,
    "quarenta": 40,
    "cinquenta": 50,
    "sessenta": 60,
    "setenta": 70,
    "oitenta": 80,
    "noventa": 90,
}

# Feminine forms are here because "duzentas" is what a speaker says when the noun
# is feminine, and a transcript carries the speaker's grammar, not the parser's.
# "cem" and "cento" are the same hundred with different distributions — "cem"
# stands alone, "cento" takes a continuation — and _fold_words enforces that.
_HUNDREDS: Final[dict[str, int]] = {
    "cem": 100,
    "cento": 100,
    "duzentos": 200,
    "duzentas": 200,
    "trezentos": 300,
    "trezentas": 300,
    "quatrocentos": 400,
    "quatrocentas": 400,
    "quinhentos": 500,
    "quinhentas": 500,
    "seiscentos": 600,
    "seiscentas": 600,
    "setecentos": 700,
    "setecentas": 700,
    "oitocentos": 800,
    "oitocentas": 800,
    "novecentos": 900,
    "novecentas": 900,
}

# Stops at millions. A 1–30 DPD consumer balance lives between R$ 120 and
# R$ 6.000 (CONTRACT §15); "bilhao" in that context is a transcription error, and
# accepting it would be parsing a number the domain cannot produce.
_SCALES: Final[dict[str, int]] = {
    "mil": 1_000,
    "milhao": 1_000_000,
    "milhoes": 1_000_000,
}

_CURRENCY_WORDS: Final[frozenset[str]] = frozenset({"reais", "real"})
_CENTAVO_WORDS: Final[frozenset[str]] = frozenset({"centavos", "centavo"})
_CONJUNCTION: Final = "e"

# A leading currency symbol only. "R$" in the middle of an utterance is not a
# form anyone speaks, and stripping it globally would let "reais R$ 30" through.
_CURRENCY_PREFIX_RE: Final = re.compile(r"\A\s*r\$\s*")

# The pt-BR digit form. The grouping is enforced rather than tolerated: a `.`
# separates groups of exactly three, so "1.200" is one thousand two hundred and
# "1.2" is not a number. That strictness is what makes the Anglo form "1200.50"
# fail instead of silently becoming R$ 1.200,50 or R$ 1,20 depending on which
# convention the reader assumed. Two decimal places at most — a third digit
# after the comma is not money.
_DIGIT_AMOUNT_RE: Final = re.compile(
    r"""\A
    (?P<whole> [0-9]{1,3} (?: \.[0-9]{3} )+ | [0-9]+ )
    (?: , (?P<cents> [0-9]{1,2}) )?
    \Z""",
    re.VERBOSE,
)


def parse_brl(text: str) -> Decimal | None:
    """Parse a spoken or written BRL amount, or return ``None``.

    Accepts the forms a Brazilian speech transcript actually produces::

        "R$ 1.200,50"  "1200,50"  "1.200"  "847 reais e 32 centavos"
        "mil e duzentos"  "quinhentos"
        "oitocentos e quarenta e sete reais e trinta e dois centavos"

    and returns ``None`` for everything else. **``None`` is a result, not a
    failure.** This function is scorer infrastructure (see the module docstring),
    and in a scorer an unparsed utterance costs one point of measured entity
    accuracy while a guessed one corrupts the metric it feeds. "uns oitocentos",
    "quase mil" and "vinte e trinta" all return ``None`` because none of them is a
    number, and the first two are hedges a person uses precisely when they do not
    mean an exact figure.

    Two refusals are worth naming because they look like conveniences:

    * ``"1200.50"`` does not parse. It is the Anglo form, and under the pt-BR
      grouping rule ``.`` introduces a group of exactly three digits, so it is
      malformed rather than ambiguous. Guessing between R$ 1.200,50 and R$ 1,20 on
      a debt amount is the 8-becomes-80 error one industry over.
    * A leading ``-`` does not parse, even though :func:`format_brl` emits one. A
      customer does not say a negative amount, so a minus sign in a transcript is
      far more likely a hyphen artefact than a sign, and reading it as a sign would
      turn punctuation noise into a value.

    Accent folding and case folding are applied; nothing else about the words is
    changed. Cents are bounded to 0–99 — the field is two digits, and "cento e
    cinquenta centavos" is a well-formed phrase that no one uses for a balance.
    """
    normalised = _normalise_transcript(text)
    if not normalised:
        return None

    split = _split_reais_centavos(normalised.split())
    if split is None:
        return None
    whole_tokens, cents_tokens = split

    whole = Decimal(0) if not whole_tokens else _parse_quantity(whole_tokens)
    if whole is None:
        return None
    if cents_tokens is None:
        return whole

    cents = _parse_quantity(cents_tokens)
    if cents is None or cents != cents.to_integral_value() or not 0 <= cents <= 99:
        return None
    # "1.200,50 reais e 30 centavos" states the cents twice and disagrees with
    # itself. There is no reading of it that is not a guess.
    #
    # The test is whether the reais half *wrote* a decimal group, not whether the
    # value it produced happens to be integral: "847,00 reais e 30 centavos" is
    # the same double statement and its whole part is numerically a whole number,
    # so integrality alone would let it through and silently resolve the conflict
    # in favour of the second figure.
    if _states_cents(whole_tokens):
        return None
    return Decimal(f"{int(whole)}.{int(cents):02d}")


def _states_cents(tokens: Sequence[str]) -> bool:
    """Whether ``tokens`` is a digit amount that carries an explicit decimal group.

    Only the single-token digit form can: a spelled-out phrase has no way to say
    cents without the word "centavos", which :func:`_split_reais_centavos` has
    already consumed by the time this runs.
    """
    if len(tokens) != 1:
        return False
    match = _DIGIT_AMOUNT_RE.match(tokens[0])
    return match is not None and match["cents"] is not None


def _normalise_transcript(text: str) -> str:
    """Case-fold, strip accents and the ``R$`` prefix, collapse whitespace.

    Accent stripping is done by decomposing to NFD and dropping the combining
    marks, which handles "três"/"tres" and "milhões"/"milhoes" in one rule instead
    of doubling every table entry. Trailing sentence punctuation goes too: a
    transcript ends in a full stop and the full stop is not part of the number.
    Note that the right-strip is safe against the thousands separator — "1.200"
    ends in a digit, so nothing is removed.
    """
    decomposed = unicodedata.normalize("NFD", text.casefold())
    unaccented = "".join(c for c in decomposed if not unicodedata.combining(c))
    without_symbol = _CURRENCY_PREFIX_RE.sub("", unaccented, count=1)
    return " ".join(without_symbol.rstrip(".,!? ").split())


def _split_reais_centavos(
    tokens: Sequence[str],
) -> tuple[Sequence[str], Sequence[str] | None] | None:
    """Split ``tokens`` into the reais part and the centavos part.

    Returns ``(whole, cents)`` where ``cents`` is ``None`` if the utterance names
    no centavos, or ``None`` overall if the shape is malformed. The unit words
    themselves are the delimiters and are consumed here, so the numeral fold below
    never has to know that "reais" exists.

    An empty whole part is legal — "trinta e dois centavos" is thirty-two centavos
    of nothing else — but an empty cents part after the word "centavos" is not:
    "reais e centavos" names no number at all.
    """
    if tokens[-1] in _CENTAVO_WORDS:
        head = tokens[:-1]
        # Last occurrence: the currency word closes the reais part, and searching
        # from the right means a stray earlier one cannot capture the split.
        index = next(
            (i for i in range(len(head) - 1, -1, -1) if head[i] in _CURRENCY_WORDS),
            None,
        )
        whole, cents = (
            (head[:index], head[index + 1 :]) if index is not None else ([], head)
        )
        if cents and cents[0] == _CONJUNCTION:
            cents = cents[1:]
        return None if not cents else (whole, cents)

    if tokens[-1] in _CURRENCY_WORDS:
        head = tokens[:-1]
        return None if not head else (head, None)

    return (tokens, None)


def _parse_quantity(tokens: Sequence[str]) -> Decimal | None:
    """Parse one numeral phrase: a single digit token, or spelled-out words."""
    if not tokens:
        return None
    if len(tokens) == 1:
        digits = _parse_digit_amount(tokens[0])
        if digits is not None:
            return digits
    return _parse_spelled(tokens)


def _parse_digit_amount(token: str) -> Decimal | None:
    """Parse ``"1.200,50"`` / ``"1200,50"`` / ``"1.200"`` / ``"847"``."""
    match = _DIGIT_AMOUNT_RE.match(token)
    if match is None:
        return None
    whole = match["whole"].replace(".", "")
    cents = match["cents"]
    return Decimal(whole if cents is None else f"{whole}.{cents}")


def _parse_spelled(tokens: Sequence[str]) -> Decimal | None:
    """Parse a spelled-out pt-BR numeral phrase.

    The conjunction "e" is structural in Portuguese numerals ("mil e duzentos",
    "quarenta e sete") rather than optional, so it is skipped by the fold — but its
    *placement* is checked here, because a phrase that opens or closes on a
    conjunction, or doubles one, is a truncated transcript. Parsing "duzentos e" as
    two hundred would be inventing the half of the number the ASR dropped.
    """
    if tokens[0] == _CONJUNCTION or tokens[-1] == _CONJUNCTION:
        return None
    if any(a == b == _CONJUNCTION for a, b in pairwise(tokens)):
        return None
    total = _fold_words(tokens)
    return None if total is None else Decimal(total)


def _fold_words(tokens: Sequence[str]) -> int | None:
    """Fold pt-BR numeral words left-to-right into an integer, or ``None``.

    Portuguese numerals are additive within a group of three digits and
    multiplicative across scales, so the fold carries two accumulators: ``group``
    for the hundreds/tens/units being assembled, and ``total`` for the groups
    already multiplied out by "mil" or "milhão".

    The grammar is enforced, not merely tolerated, and that is the difference
    between this and the four-line version. A bare left fold that sums every token
    it recognises accepts "vinte e trinta" and answers fifty — a number nobody
    said, produced with full confidence, which is the fabrication class this whole
    repository is built to refuse. So each group admits at most one hundreds word,
    one tens-or-teens word and one units word, in descending magnitude; "cem" (as
    against "cento") admits no continuation at all; a teen consumes its own units
    place; and each scale must be strictly smaller than the last, which is what
    rejects "mil mil".
    """
    total = 0
    group = 0
    has_hundred = has_ten = has_unit = False
    is_exact_cem = is_teen = False
    last_scale: int | None = None
    consumed = False

    for token in tokens:
        if token == _CONJUNCTION:
            continue

        if token in _HUNDREDS:
            if has_hundred or has_ten or has_unit:
                return None
            has_hundred = True
            is_exact_cem = token == "cem"
            group += _HUNDREDS[token]
        elif token in _TEENS:
            if is_exact_cem or has_ten or has_unit:
                return None
            has_ten = is_teen = True
            group += _TEENS[token]
        elif token in _TENS:
            if is_exact_cem or has_ten or has_unit:
                return None
            has_ten = True
            group += _TENS[token]
        elif token in _UNITS:
            if is_exact_cem or is_teen or has_unit:
                return None
            has_unit = True
            group += _UNITS[token]
        elif token in _SCALES:
            scale = _SCALES[token]
            if last_scale is not None and scale >= last_scale:
                return None
            # A bare "mil" is one thousand: Portuguese omits the "um" that English
            # also omits in "a thousand". An explicit group multiplies instead.
            total += (group if has_hundred or has_ten or has_unit else 1) * scale
            last_scale = scale
            group = 0
            has_hundred = has_ten = has_unit = is_exact_cem = is_teen = False
        else:
            return None

        consumed = True

    return total + group if consumed else None


# ---------------------------------------------------------------------------
# 3. CPF — the second identifier on the right-party gate
# ---------------------------------------------------------------------------

# `[0-9]` rather than `\d`, and no `str.isdigit`: both of those are true for
# Unicode digits from other scripts, and a validator for a Brazilian national
# identifier that accepted Arabic-Indic digits would be accepting a string the
# rest of the system cannot compare against the booked value.
_CPF_RE: Final = re.compile(r"[0-9]{11}")

_CPF_LENGTH: Final = 11


def is_valid_cpf(digits: str) -> bool:
    """True if ``digits`` is a checksum-valid CPF, given as eleven bare digits.

    Separators are the caller's problem: :func:`is_valid_cpf` takes what
    ``identity_matches`` has already stripped, so it never has to guess whether an
    unexpected character was formatting or a transcription error. ``"123.456.789-09"``
    is therefore ``False`` here and correct at the call site — that asymmetry is
    deliberate, and it keeps exactly one place in the codebase deciding what a
    digit is.

    The algorithm is the standard two-check-digit weighted sum: the first nine
    digits weighted 10..2 give digit ten, the first ten weighted 11..2 give digit
    eleven, each remainder taken as ``sum * 10 mod 11`` with ten collapsing to
    zero.

    Repeated-digit CPFs are rejected separately, and the reason is that they
    *pass*: ``"11111111111"`` satisfies both check digits, as does every other
    ``ddddddddddd``. They are placeholder values, universally treated as invalid in
    Brazil, and a right-party gate that accepted one would be verifying identity
    against a string a caller can produce without knowing anything about the
    account. This is the only rule here that is convention rather than arithmetic,
    which is why it is stated rather than folded into the loop.

    CONTRACT §9 makes this one of three conditions on the identity gate, all of
    which must hold. It is a check on the *form* of the number and nothing more: a
    valid CPF that does not equal the booked one is still the wrong party.
    """
    if _CPF_RE.fullmatch(digits) is None:
        return False
    if digits == digits[0] * _CPF_LENGTH:
        return False

    numbers = [int(d) for d in digits]
    for position in (9, 10):
        weighted = sum(n * (position + 1 - i) for i, n in enumerate(numbers[:position]))
        if (weighted * 10) % 11 % 10 != numbers[position]:
            return False
    return True
