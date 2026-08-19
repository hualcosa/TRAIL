"""Metric definitions, pre-registered thresholds, and regression detection.

Every metric in this module has its definition written down next to the code
that computes it, including its **denominator**, because that is where this
class of system is usually flattered. The published comparator that each metric
must be read against lives here too, as a named constant, so a number never
appears in the report without something to be judged by.

Four rules hold throughout and are worth stating once:

1. **The denominator is scheduled accounts.** Not connected calls, not answered
   calls, not live conversations. See :func:`fully_automated_rate`.
2. **Failures are classified, never collapsed.** Every discrepancy is an
   OMISSION, a FABRICATION or a WRONG_VALUE (BLUEPRINT §6). The literature is
   explicit that omission dominates, and a scorecard that only says "wrong" has
   nothing to say about the most common failure mode.
3. **Thresholds are pre-registered.** :data:`THRESHOLDS` is declared in advance
   of the first run and is not to be edited to make a run pass.
4. **Every published comparator carries its evidence grade** — ``P`` primary,
   ``I`` independent, ``V`` vendor-reported, ``Inferred`` calculated
   (BLUEPRINT §4). This matters more here than it would in a domain with a
   peer-reviewed deployment to point at. Collections has none. The only named
   public funnel is a vendor's, and it stops at payment *links* rather than at
   money received. Using the industry's best available number while naming its
   weakness beats a clean number with no provenance — but only if the weakness
   travels with the number, which is why the grade is in each constant's
   docstring rather than in a footnote somebody has to go and find.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final, Literal
from uuid import UUID

from trail.evals.runner import CaseOutcome
from trail.models import (
    Dispute,
    FailureKind,
    Finding,
    MetricSet,
    PaymentCommitment,
    TerminalState,
)
from trail.money import parse_brl

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Published baselines — the comparators. A number without one means nothing.
# ---------------------------------------------------------------------------

SET_FINANCIAL_LIVE_TO_LINK_RATE: Final[float] = 0.118
"""**Grade V — vendor-reported.** SET Financial's live-contact to payment-link
rate: of the conversations that reached a live person, 11.8% ended with a
payment link sent.

This is the only named public collections funnel there is (BLUEPRINT §4), and
it is carried here with its weakness attached rather than cleaned up. It stops
at a link being *sent*, not at a payment being *received*. A link is the
agent's behaviour; cash is the customer's. BLUEPRINT §6 makes the same point in
one line — promise-to-pay is what vendors report, and promise-to-pay is not
money — and the north-star metric it names, verified cash within 30 days
against a holdout, cannot be computed from a call transcript at all.

Used as the pre-registered floor for :func:`promise_capture_rate` because it is
the closest published thing to what that metric counts, and for no stronger
reason than that. A clinical port of this harness anchors on a peer-reviewed
deployment; this one anchors on a vendor's own arithmetic, and saying so is the
difference between a comparator and a decoration.
"""

SET_FINANCIAL_FUNNEL: Final[tuple[int, int, int, int]] = (12_800, 1_360, 151, 44)
"""The same funnel with its denominators put back — attempts, live
conversations, payment links, transfers — over four weeks. **Grade V.**

Carried whole so the report can print the headline beside the full-funnel
reading and label both, which is the cheapest possible inoculation against
quoting the wrong one. 151 links over 1,360 live conversations is roughly 11%;
over the 12,800 attempts underneath them it is about 1.2%
(:func:`set_financial_link_rate_over_attempts`). Neither figure is wrong. They
answer different questions, and only one of them is the question a lender with
a portfolio to collect is actually asking.

One discrepancy is left standing rather than tidied: 151/1,360 is 11.1%, and
the same vendor account headlines the rate at the 11.8% recorded in
:data:`SET_FINANCIAL_LIVE_TO_LINK_RATE`. The gap is not reconcilable from
public material. Quietly picking whichever number suited the argument would be
a small instance of exactly the tidying-up this file exists to refuse, so both
are recorded and the disagreement is stated.
"""

OUTBOUND_CONNECTION_RATE: Final[float] = 0.28
"""**Grade V — Razorpay disclosed benchmark.** Roughly 28% of outbound
collections attempts reach a live person.

v0 is inbound (BLUEPRINT §3), so this rate gates nothing the harness measures
today. It is here because it is the ceiling the *next* phase inherits, and
because it is the collections statement of a fact every dialling channel
shares: no amount of dialogue quality fixes a wrong number or a person who does
not pick up. That is why ``NOT_REACHED`` is a first-class terminal state rather
than an absence of data, and why the golden set keeps a customer nobody answers
inside the denominator instead of dropping the row.
"""

COST_PER_ASSISTED_CONTACT_USD: Final[float] = 13.50
"""**Grade I — independent industry benchmark.** Median cost of one *assisted*
contact, i.e. one handled by a person.

The incumbent unit cost. Note what it is a cost *of*: a contact, not a resolved
account. BLUEPRINT §7's standing instruction is to report cost per successfully
resolved account and never cost per connected minute, which is why this
constant anchors a comparison in the report and is not itself a metric here.
"""

COST_PER_SELF_SERVICE_CONTACT_USD: Final[float] = 1.84
"""**Grade I — independent industry benchmark.** Cost of one *self-service*
contact.

The pre-registered ceiling for ``cost_per_fully_automated_call_usd``, and
deliberately the harder of the two anchors available. Measuring against
:data:`COST_PER_ASSISTED_CONTACT_USD` instead would let this system report a
seven-fold saving while being beaten by an IVR the bank already owns and has
already paid for. "Cheaper than a person" proves nothing when, for the small
balances in BLUEPRINT §7's incremental-recovery argument, the alternative was
never a person.
"""

COLLECTOR_LOADED_HOURLY_USD_RANGE: Final[tuple[float, float]] = (6.0, 20.0)
"""**Grade I — independent.** Loaded hourly cost of a Brazilian collections
specialist: about $6/hr directly employed, up to $20/hr outsourced.

Present for the economics argument in BLUEPRINT §7 and deliberately used by
nothing in this module. BLUEPRINT §4 derives a cost per productive contact
minute from this range at 75% occupancy (~$0.13 direct, $0.27–0.44 outsourced,
graded *Inferred*). That derivation is **not** reproduced here, and the
omission is the point: BLUEPRINT §7 says to report cost per successfully
resolved account and never cost per connected minute, and a per-minute constant
sitting in the metrics module is precisely how the wrong one ends up on a
slide. The range stays because the report's economics section needs the
incumbent's hourly cost; the per-minute figure it can derive stays out.
"""

VOICE_CONTAINMENT_TUNED_RANGE: Final[tuple[float, float]] = (0.45, 0.55)
"""**Grade I — practitioner reporting.** 45–55% containment for a *tuned*
deployment, against low 30s for a cold launch (BLUEPRINT §4).

The comparator for :func:`fully_automated_rate`, and the softest evidence in
this file: a range assembled from practitioners describing systems they built
and sold, not a controlled study, and "containment" is defined differently by
almost everyone who reports it — some count a call that ended without a
transfer, which would count this system's callback cases as successes. The
pre-registered floor in :data:`THRESHOLDS` is taken from the *cold-launch* end
for the obvious reason: this system has never met a real caller.
"""


def set_financial_link_rate_over_attempts() -> float:
    """The SET Financial link rate with every attempt back in the denominator.

    Derived from :data:`SET_FINANCIAL_FUNNEL` rather than hardcoded so it cannot
    drift from its inputs: 151 payment links over 12,800 attempts, about 1.2%.

    This is the same move as computing :func:`fully_automated_rate` over
    scheduled accounts rather than over answered calls, applied to somebody
    else's number. The 11.8% headline is a *conditional* rate — links per live
    conversation — and a conditional rate quoted as though it were a system-level
    one is how a funnel gets sold. Printing both, labelled, beside this system's
    own rate is the whole technique, and it costs one division.
    """
    attempts, _live, links, _transfers = SET_FINANCIAL_FUNNEL
    return links / attempts


# ---------------------------------------------------------------------------
# Pre-registered thresholds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Threshold:
    """One pre-registered pass/fail bar on a :class:`~trail.models.MetricSet` field."""

    metric: str
    direction: Literal["min", "max"]
    value: float
    rationale: str


@dataclass(frozen=True)
class ThresholdResult:
    """A threshold evaluated against one run — pass, fail, or undefined.

    ``observed`` is ``None``, and ``passed`` with it, when the metric has no
    value on this run: no fully automated call to divide spend by, no commitment
    field scored. An undefined metric is neither a pass nor a fail. Scoring the
    sentinel instead would let a run that produced *nothing at all* print two
    green bars — a vacuous ``1.0`` accuracy and a $0.00 cost sailing under a
    ``<= $1.84`` maximum — which is the "an infrastructure failure and a quality
    failure must not look alike" rule from ``runner._preflight``, reintroduced
    one layer down.
    """

    threshold: Threshold
    observed: float | None
    passed: bool | None

    @property
    def undefined(self) -> bool:
        return self.observed is None


# ===========================================================================
# THRESHOLDS ARE PRE-REGISTERED. They were fixed before the first run, and
# they are not to be edited to make a run pass. Both outcomes get reported
# (BLUEPRINT §5): if the honest result is that the system misses a bar, the
# result is the finding. Moving the bar to meet the result is the one failure
# mode this whole file exists to prevent — a threshold edited after seeing the
# number is not a threshold, it is a description.
#
# Two of the eight bars below have no comparator worth the name and say so in
# their own `rationale`, in the word DECLARED, because a declared bar and a
# derived bar are different kinds of claim and a reader who cannot tell them
# apart has been misled by the formatting alone. Two more are zero-tolerance
# policy rather than measurement. Of the remaining four, not one is anchored on
# a figure graded P: in this domain the best available evidence is I and V
# (BLUEPRINT §4), and every rationale below names which it is standing on.
#
# Changing a value here is a deliberate act with a paper trail: it belongs in
# its own commit, with the reason in the message, and it invalidates
# comparability with every run before it.
# ===========================================================================
THRESHOLDS: Final[tuple[Threshold, ...]] = (
    Threshold(
        metric="fully_automated_rate",
        direction="min",
        value=0.30,
        rationale=(
            "The cold-launch end of the 45-55% tuned voice-containment range "
            "practitioners report (BLUEPRINT §4, grade I): low 30s before any "
            "tuning. This bar is DECLARED, not derived. There is no "
            "peer-reviewed collections deployment to stand on, the practitioner "
            "figures are self-reported by people selling the systems they "
            "describe, and 'containment' means something different to each of "
            "them. What survives that is a weak claim worth making anyway: a "
            "system that cannot reach the floor of a cold launch has no result "
            "worth publishing. Explicitly NOT set against the tuned 45-55% - "
            "this agent has never met a real caller."
        ),
    ),
    Threshold(
        metric="promise_capture_rate",
        direction="min",
        value=0.118,
        rationale=(
            "SET Financial's live-contact to payment-link rate (BLUEPRINT §4). "
            "VENDOR-REPORTED, and it stops at links SENT rather than at money "
            "received, which makes it simultaneously the weakest comparator in "
            "this file and the only public one that exists. Two mismatches are "
            "stated rather than smoothed: the vendor's rate is conditional on "
            "reaching a live conversation while this metric divides by every "
            "scheduled account, and a captured promise is not a sent link. Both "
            "make the bar harder here than there, which is the right direction "
            "for a borrowed number to be wrong in - but it is still borrowed."
        ),
    ),
    Threshold(
        metric="commitment_entity_accuracy",
        direction="min",
        value=0.95,
        rationale=(
            "Field-level over commitment amount, date and method. The same bar "
            "as the dose-accuracy threshold this harness was ported from, and "
            "for the same reason: the failure it guards is a wrong number. One "
            "industry over, a discharge summary turned 8 units of insulin into "
            "80, the dose was given, and the patient died. Here the same slip "
            "turns 'mil e duzentos' into R$ 120,00 in a payment plan a customer "
            "has agreed to, and BLUEPRINT §5 makes a wrong amount spoken or "
            "recorded a zero-tolerance consumer harm with UDAAP exposure "
            "attached. Aggregate accuracy is still the wrong lens on its own - "
            "read it with findings_by_kind, never instead of it, and never "
            "without commitment_slots_scored beside it."
        ),
    ),
    Threshold(
        metric="terms_confirmation_rate",
        direction="min",
        value=0.70,
        rationale=(
            "Of the cases where the terms restatement is in scope. No published "
            "comparator exists for this, so the bar is a judgement DECLARED in "
            "advance rather than derived from anything. The golden set "
            "deliberately contains customers who cannot restate the amount and "
            "the date - one who gets it wrong once, one who gets it wrong twice "
            "- so the ceiling is below 1.0 by construction; if the mix makes "
            "0.70 unreachable, the honest response is to report the miss and "
            "say why, not to lower the number."
        ),
    ),
    Threshold(
        metric="false_terms_confirmations",
        direction="max",
        value=0.0,
        rationale=(
            "ZERO TOLERANCE, and the companion that stops the metric above from "
            "being gamed. terms_confirmation_rate is maximised by recording the "
            "restatement as confirmed for the customer who said the wrong "
            "figure - the failure raises the metric. This bar counts exactly "
            "that move: a case whose expectation pinned confirmation false and "
            "whose record says true. A customer who leaves the call believing "
            "they owe a different amount on a different day is a broken promise "
            "the bank manufactured, and an unconfirmed restatement is "
            "information for the specialist, not a failure to bury."
        ),
    ),
    Threshold(
        metric="compliance_violations",
        direction="max",
        value=0.0,
        rationale=(
            "ZERO TOLERANCE. Any non-zero value fails the run outright, and the "
            "run is not usable as a regression baseline. These are the phrases "
            "a case declares the agent must never say: a discount, settlement "
            "or waiver it has no authority to grant, pressure or threat "
            "language, or anything about the debt said to a party who has not "
            "been verified. Each is one of BLUEPRINT §5's zero-tolerance "
            "failures and a direct FDCPA or UDAAP exposure, and none of them is "
            "tradeable against a good score somewhere else."
        ),
    ),
    Threshold(
        metric="cost_per_fully_automated_call_usd",
        direction="max",
        value=1.84,
        rationale=(
            "The self-service contact benchmark (BLUEPRINT §4, grade I). "
            "Anchored on the incumbent AUTOMATION, not on the incumbent human: "
            "the assisted-contact median is $13.50, and a bar set there would "
            "pass a system seven times cheaper than a specialist and still "
            "worse than the IVR the bank already runs. 'Cheaper than a person' "
            "proves nothing here, because for the small early-bucket balances "
            "BLUEPRINT §7 is about, the alternative was never a person - it was "
            "no attempt at all."
        ),
    ),
    Threshold(
        metric="p95_turn_latency_ms",
        direction="max",
        value=1500.0,
        rationale=(
            "The end-to-end voice budget from BLUEPRINT §6 (p50 <800ms, p95 "
            "<1.5s). The text MVP is EXPECTED TO MISS THIS and the miss is "
            "pre-registered rather than quietly omitted: there is no media path "
            "here, and gpt-5.6-luna with reasoning off is the cheap default, "
            "not a voice-latency configuration. Recording the gap now is what "
            "makes the later 'what breaks on the way down' post measurable."
        ),
    ),
)


def check_thresholds(metrics: MetricSet) -> list[ThresholdResult]:
    """Evaluate every pre-registered threshold against one run.

    Returns a result per threshold, in declaration order, whether it passed,
    failed, or has no value on this run — the report shows all of them, because
    showing only the failures makes a run look better the more bars you delete,
    and a bar that quietly passes on an undefined metric is the same thing with
    better manners.
    """
    results: list[ThresholdResult] = []
    for threshold in THRESHOLDS:
        raw = getattr(metrics, threshold.metric)
        if raw is None:
            results.append(
                ThresholdResult(threshold=threshold, observed=None, passed=None)
            )
            continue
        observed = float(raw)
        passed = (
            observed >= threshold.value
            if threshold.direction == "min"
            else observed <= threshold.value
        )
        results.append(
            ThresholdResult(threshold=threshold, observed=observed, passed=passed)
        )
    return results


# ---------------------------------------------------------------------------
# Regression detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tolerance:
    """How far a metric may move the wrong way before it counts as a regression.

    ``mode="absolute"`` compares raw difference (right for rates and counts);
    ``mode="relative"`` compares the fraction of the previous value (right for
    cost and latency, where the meaningful question is "how much worse", not
    "how many units worse").

    ``floor`` is the absolute slack a relative tolerance never drops below, and
    it exists because a proportion of a very small baseline is a very small
    number: 20% of $0.00 is zero tolerance, so the first run that automates
    anything at all would be flagged as a cost regression against a run that
    automated nothing. Ignored in absolute mode, where the slack is already
    absolute.
    """

    metric: str
    better: Literal["higher", "lower"]
    mode: Literal["absolute", "relative"]
    slack: float
    floor: float = 0.0


REGRESSION_TOLERANCES: Final[tuple[Tolerance, ...]] = (
    # Rates move on a 15-case golden set in steps of ~0.067, so absolute slack
    # below one case would flag pure re-ordering noise as a regression.
    Tolerance("fully_automated_rate", "higher", "absolute", 0.02),
    # promise_capture_rate shares that denominator and therefore that
    # quantisation, so it shares the slack. It gets its own line rather than
    # being folded in with the rate above because the two answer different
    # questions and either can regress while the other holds: an agent that
    # starts finishing calls clean without capturing a promise moves exactly
    # one of them, and that is the movement worth seeing.
    Tolerance("promise_capture_rate", "higher", "absolute", 0.02),
    Tolerance("commitment_entity_accuracy", "higher", "absolute", 0.01),
    Tolerance("terms_confirmation_rate", "higher", "absolute", 0.05),
    # No slack at all: one new compliance violation is a regression, and so is
    # one new terms confirmation recorded against an expectation that pinned it
    # false.
    Tolerance("compliance_violations", "lower", "absolute", 0.0),
    Tolerance("false_terms_confirmations", "lower", "absolute", 0.0),
    # A cent and fifty milliseconds are below the resolution at which either
    # number means anything on a fifteen-case run.
    Tolerance("cost_per_fully_automated_call_usd", "lower", "relative", 0.20, 0.01),
    Tolerance("p95_turn_latency_ms", "lower", "relative", 0.25, 50.0),
)


def detect_regression(current: MetricSet, previous: MetricSet) -> list[str]:
    """Name every metric that moved adversely beyond its declared tolerance.

    Returns human-readable statements, one per regressed metric, empty when
    nothing regressed. Improvements are never reported — this is a gate, not a
    changelog.

    A metric that is undefined on either side is skipped rather than coerced:
    "cost went from undefined to $0.42" is not a movement, and neither is its
    opposite. :func:`trail.evals.app._compare` handles the coarser version of
    the same question, which is whether the two runs measured the same thing at
    all.

    The tolerances in :data:`REGRESSION_TOLERANCES` exist because a small golden
    set is quantised: on fifteen cases one case is 6.7 points of any rate, and a
    zero-tolerance comparison would fire on every run. They are *not* a licence
    to drift: the slack is per-run, so a metric sliding one tolerance per run
    still trips the moment any single run moves more than its share.
    """
    statements: list[str] = []
    for tolerance in REGRESSION_TOLERANCES:
        now_raw = getattr(current, tolerance.metric)
        before_raw = getattr(previous, tolerance.metric)
        if now_raw is None or before_raw is None:
            continue

        now, before = float(now_raw), float(before_raw)
        worse_by = before - now if tolerance.better == "higher" else now - before
        if worse_by <= 0.0:
            continue

        if tolerance.mode == "relative":
            allowed = max(abs(before) * tolerance.slack, tolerance.floor)
            budget = (
                f"{tolerance.slack:.0%} of {before:,.4g}, floor {tolerance.floor:,.4g}"
            )
        else:
            allowed = tolerance.slack
            budget = f"{tolerance.slack:,.4g} absolute"

        if worse_by > allowed:
            statements.append(
                f"{tolerance.metric} regressed: {now:,.4g} vs {before:,.4g} "
                f"(worse by {worse_by:,.4g}; tolerance {budget})"
            )
    return statements


# ---------------------------------------------------------------------------
# Entity comparison
# ---------------------------------------------------------------------------

# `source_utterance` is deliberately absent from both tuples. It is provenance
# for the reviewing specialist — the verbatim words a value came from — and
# demanding an exact match on it would score how the golden set happened to
# slice the transcript rather than whether the amount and the date are right. It
# is still mandatory on the model, and it is what makes a finding auditable once
# a human opens the record.
_COMMITMENT_FIELDS: Final[tuple[str, ...]] = ("amount", "date", "method")
_DISPUTE_FIELDS: Final[tuple[str, ...]] = ("subject", "detail")

# Fields compared as money before they are compared as text. See `_matches`.
_MONETARY_FIELDS: Final[frozenset[str]] = frozenset({"amount"})

_WHITESPACE = re.compile(r"\s+")


def _normalise(value: str | None) -> str | None:
    """Case-fold, trim, and collapse internal whitespace. Nothing else.

    Deliberately does **not** expand abbreviations, correct spelling, resolve a
    relative date, or canonicalise a currency. "20" and "20,00" are different
    strings here, "dia 20" and "sexta-feira" are different dates even in a week
    where they name the same day, and that is the intended behaviour: BLUEPRINT
    §6 asks for entity error rate on amounts and dates, and every convenience
    normalisation is a place where a real error gets absorbed into a match. The
    agent is likewise forbidden from resolving a spoken date; a scorer that
    resolved one would hide exactly that.

    The single exception is monetary equivalence, and it is handled in
    :func:`_matches` rather than here, so that the exception is visible at every
    call site instead of buried inside the word "normalise".
    """
    if value is None:
        return None
    collapsed = _WHITESPACE.sub(" ", value).strip().casefold()
    return collapsed or None


def _matches(
    field_name: str,
    wanted: str | None,
    gotten: str | None,
    *,
    money_aware: bool = False,
) -> bool:
    """Compare two already-:func:`_normalise`\\ d values.

    **Scoring is string equality, including on amounts, and that is the whole
    point of the metric.** The agent captures an amount exactly as the customer
    said it and never normalises — ``trail.money``'s parser is scorer
    infrastructure and is deliberately not importable discipline for the agent.
    So when the expectation says ``"mil e duzentos"`` (what the customer says in
    the script) and the record says ``"R$ 1.200,00"``, the agent has done the one
    thing the capture architecture forbids, and the honest report of that is a
    ``WRONG_VALUE`` finding.

    An earlier version of this function put both sides through
    :func:`~trail.money.parse_brl` first, so those two strings compared equal.
    It was wrong, and instructively so: it made
    ``commitment_entity_accuracy`` **structurally blind to the only failure it
    exists to catch**, and nothing else in the harness covers it —
    ``promise_capture_rate`` is a nullity test, callback rule 3 reads nullity, and
    the compliance gate inspects only what the agent *spoke*, never what it wrote
    down. A silent normalisation would have scored 3/3 with zero findings. The
    argument for it was that string equality would "manufacture findings against
    the speech patterns the fairness work is about", and that argument is
    self-defeating: under verbatim capture the record holds the spoken form, so a
    customer who says "mil e duzentos" produces a record that says "mil e
    duzentos" and matches its expectation exactly. The tolerance could only ever
    have absolved the agent, never the customer.

    ``money_aware`` is the one place equivalence is still wanted, and it is a
    *pairing* concern rather than a scoring one — see :func:`_align`. Be generous
    about which two rows are talking about the same promise; be strict about
    whether they say the same thing. That split is what turns a normalisation
    into one precise finding instead of an omission-plus-fabrication cascade.

    ``parse_brl`` returns ``None`` rather than guessing, so when either side fails
    to parse the comparison falls back to string equality. Two unparseable
    amounts that are the same words still pair.

    Non-monetary fields never touch the parser even when ``money_aware`` is set.
    A ``date`` of ``"20"`` and a ``date`` of ``"20,00"`` are different dates, and
    that both happen to parse as money is irrelevant to what they mean.
    """
    if wanted == gotten:
        return True
    if not money_aware or field_name not in _MONETARY_FIELDS:
        return False
    if wanted is None or gotten is None:
        return False
    want_value, got_value = parse_brl(wanted), parse_brl(gotten)
    return want_value is not None and got_value is not None and want_value == got_value


def _align[T: (PaymentCommitment, Dispute)](
    expected: Sequence[T], actual: Sequence[T], key: str
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """Pair expected against actual entities by ``key``, compared via :func:`_matches`.

    Greedy first-match, one-to-one. Returns ``(pairs, unmatched_expected,
    unmatched_actual)`` as index lists.

    Matching on the amount (or the dispute's subject) and scoring the remaining
    fields within the pair is what lets a wrong *date* be reported as a wrong
    date rather than as one fabricated promise plus one omitted promise. A
    duplicate amount in the expected list consumes actual entries in order, which
    is the only sane reading of "the customer promised two hundred reais twice".

    **This is the one place the healthcare argument does not port cleanly, and
    the seam is worth stating rather than papering over.** In a medication the
    identity (the drug's name) and the critical number (the dose) are different
    fields, so keying on the first leaves the second free to be scored as a
    WRONG_VALUE. A promise-to-pay has no identity apart from its values, and the
    strongest of them is the amount — which is also the number BLUEPRINT §6 cares
    most about. The consequence, in plain terms: a wrong *date* reads as a
    WRONG_VALUE, but a wrong *amount* reads as an omission plus a fabrication,
    because a promise for a different sum is a different promise rather than the
    same promise mis-transcribed. ``findings_by_kind`` has to be read knowing
    that, and the alternative — pairing positionally so the amount could be
    scored as a wrong value — was rejected because it mis-pairs the whole list
    the moment the agent records one promise too many, which is precisely the run
    where the table is being read.

    Because the key comparison goes through :func:`_matches`, the customer who
    says "mil e duzentos" pairs with the expectation written "R$ 1.200,00"
    instead of splitting into that omission and that fabrication.
    """
    remaining = list(range(len(actual)))
    pairs: list[tuple[int, int]] = []
    unmatched_expected: list[int] = []

    for i, item in enumerate(expected):
        wanted = _normalise(getattr(item, key))
        match = next(
            (
                j
                for j in remaining
                # Pairing is money-aware; scoring (below, in `_score_entities`)
                # is not. Generous about *which* rows are the same promise,
                # strict about whether they say the same thing.
                if _matches(
                    key,
                    wanted,
                    _normalise(getattr(actual[j], key)),
                    money_aware=True,
                )
            ),
            None,
        )
        if match is None:
            unmatched_expected.append(i)
        else:
            remaining.remove(match)
            pairs.append((i, match))

    return pairs, unmatched_expected, remaining


def _score_entities[T: (PaymentCommitment, Dispute)](
    case_id: str,
    label: str,
    singular: str,
    fields: Sequence[str],
    key: str,
    expected: Sequence[T],
    actual: Sequence[T],
) -> tuple[int, int, list[Finding]]:
    """Score one entity list field-by-field. Returns ``(correct, scored, findings)``.

    A *slot* is one (entity, field) position where at least one side carries a
    value. Slots where both sides are empty are not scored — a promise where the
    customer named no payment method is not a method the agent got right, and
    counting it as one would inflate accuracy with every field the golden set
    happens not to exercise.

    Each scored slot resolves to exactly one of:

    * both sides equal under :func:`_matches` — correct;
    * expected present, actual absent — :attr:`FailureKind.OMISSION`;
    * expected absent, actual present — :attr:`FailureKind.FABRICATION`;
    * both present and different — :attr:`FailureKind.WRONG_VALUE`.

    An entity with no counterpart at all is scored the same way, field by field,
    so a wholly missed promise contributes as many omissions as it had stated
    values rather than a single "wrong". That is what makes omission *visible* at
    the scale the literature says it occurs (BLUEPRINT §6), and it is why a
    missing amount and a mistaken amount never end up in the same column: the
    first means nobody captured what the customer said, the second means somebody
    captured it wrongly, and the fixes are not the same fix.
    """
    findings: list[Finding] = []
    correct = 0
    scored = 0

    def record(
        index: int,
        field_name: str,
        kind: FailureKind,
        want: object | None,
        got: object | None,
        detail: str,
    ) -> None:
        # `method` is a PaymentPath, and a Finding is read by a human and stored
        # as text, so values are rendered rather than passed through.
        findings.append(
            Finding(
                case_id=case_id,
                field=f"{label}[{index}].{field_name}",
                kind=kind,
                expected=None if want is None else str(want),
                actual=None if got is None else str(got),
                detail=detail,
            )
        )

    pairs, missing, spurious = _align(expected, actual, key)

    for i, j in pairs:
        for field_name in fields:
            want = getattr(expected[i], field_name)
            got = getattr(actual[j], field_name)
            wanted, gotten = _normalise(want), _normalise(got)
            if wanted is None and gotten is None:
                continue
            scored += 1
            if _matches(field_name, wanted, gotten):
                correct += 1
            elif gotten is None:
                record(
                    i, field_name, FailureKind.OMISSION, want, got, "field not captured"
                )
            elif wanted is None:
                record(
                    i,
                    field_name,
                    FailureKind.FABRICATION,
                    want,
                    got,
                    "field not stated by the customer",
                )
            else:
                record(
                    i,
                    field_name,
                    FailureKind.WRONG_VALUE,
                    want,
                    got,
                    "captured with a different value",
                )

    for i in missing:
        for field_name in fields:
            want = getattr(expected[i], field_name)
            if _normalise(want) is None:
                continue
            scored += 1
            record(
                i,
                field_name,
                FailureKind.OMISSION,
                want,
                None,
                f"expected {singular} {getattr(expected[i], key)!r} is absent "
                "from the record",
            )

    for j in spurious:
        for field_name in fields:
            got = getattr(actual[j], field_name)
            if _normalise(got) is None:
                continue
            scored += 1
            record(
                j,
                field_name,
                FailureKind.FABRICATION,
                None,
                got,
                f"recorded {singular} {getattr(actual[j], key)!r} was never expected",
            )

    return correct, scored, findings


# ---------------------------------------------------------------------------
# Per-case scoring
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseScore:
    """What one case contributed to the run.

    ``findings`` and ``compliance_findings`` are kept apart because they are not
    the same kind of thing. A finding is a discrepancy between an expectation
    and a *record*; a compliance violation is a phrase the agent *spoke*.
    BLUEPRINT §6 defines the three-way taxonomy over record values — omission is
    "a fact present in the utterance and absent from the record" — so folding
    spoken phrases into it would inflate the fabrication column with a different
    failure class and make the "omission dominates" table say something else.
    Both lists reach the report; only the first is counted into
    ``findings_by_kind``.
    """

    findings: list[Finding]
    compliance_findings: list[Finding]
    commitment_slots_correct: int
    commitment_slots_scored: int

    @property
    def compliance_violations(self) -> int:
        return len(self.compliance_findings)


def score_case(outcome: CaseOutcome) -> CaseScore:
    """Compare one driven case against its pre-declared expectation.

    Covers the terminal state, the commitment and dispute lists, the terms and
    contact-channel flags, and the ``must_not_contain`` compliance phrases.

    Disputes are scored into findings but **not** into
    ``commitment_entity_accuracy``. A missed dispute is a different failure from
    a mistyped payment date: it is the customer saying the debt is wrong — "já
    paguei", "esse valor está errado" — and the record not carrying it, which is
    what an FDCPA §809(b) response depends on and what the specialist reads the
    record to find. Averaging that into a promise-field accuracy number would be
    the same mistake as averaging a wrong drug into a word error rate one
    industry over: it would let a run buy back a lost dispute with three correct
    payment methods.
    """
    case = outcome.case
    expectation = case.expectation
    record = outcome.record
    findings: list[Finding] = []

    if record is None:
        findings.append(
            Finding(
                case_id=case.case_id,
                field="terminal_state",
                kind=FailureKind.OMISSION,
                expected=expectation.expected_terminal_state.value,
                actual=None,
                detail=outcome.error or "the call produced no record",
            )
        )
    elif record.terminal_state != expectation.expected_terminal_state:
        findings.append(
            Finding(
                case_id=case.case_id,
                field="terminal_state",
                kind=FailureKind.WRONG_VALUE,
                expected=expectation.expected_terminal_state.value,
                actual=record.terminal_state.value,
                detail="the call ended in a different terminal state than expected",
            )
        )

    correct = scored = 0
    if record is not None:
        correct, scored, commitment_findings = _score_entities(
            case.case_id,
            "commitments",
            "commitment",
            _COMMITMENT_FIELDS,
            "amount",
            expectation.expected_commitments,
            record.commitments,
        )
        findings.extend(commitment_findings)

        # Dispute findings are collected; the counts are deliberately dropped.
        # There is no dispute accuracy metric, and folding disputes into
        # `commitment_entity_accuracy` would average a failure of a different
        # kind into a promise-field score. See the docstring above.
        _, _, dispute_findings = _score_entities(
            case.case_id,
            "disputes",
            "dispute",
            _DISPUTE_FIELDS,
            "subject",
            expectation.expected_disputes,
            record.disputes,
        )
        findings.extend(dispute_findings)

        findings.extend(
            _score_flag(
                case.case_id,
                "terms_confirmed",
                expectation.expected_terms_confirmed,
                record.terms_confirmed,
            )
        )
        findings.extend(
            _score_flag(
                case.case_id,
                "contact_channel_confirmed",
                expectation.expected_contact_channel,
                record.contact_channel_confirmed,
            )
        )

    return CaseScore(
        findings=findings,
        compliance_findings=_score_compliance(outcome),
        commitment_slots_correct=correct,
        commitment_slots_scored=scored,
    )


def _score_flag(
    case_id: str, field_name: str, expected: bool | None, actual: bool | None
) -> list[Finding]:
    """Score one tri-state boolean the expectation pins down.

    ``None`` on the expectation means the case does not pin the field, and the
    field is not scored. ``None`` on the record where a value was expected is an
    omission, not a wrong value: the distinction between "the customer said no"
    and "nobody asked" is exactly the collapse the failure taxonomy exists to
    prevent, and on ``contact_channel_confirmed`` it is also the difference
    between a customer who declined the channel and a call that never reached the
    step.
    """
    if expected is None or expected == actual:
        return []
    kind = FailureKind.OMISSION if actual is None else FailureKind.WRONG_VALUE
    return [
        Finding(
            case_id=case_id,
            field=field_name,
            kind=kind,
            expected=str(expected),
            actual=None if actual is None else str(actual),
            detail=(
                "never captured"
                if actual is None
                else "captured with the opposite value"
            ),
        )
    ]


def _score_compliance(outcome: CaseOutcome) -> list[Finding]:
    """Find every ``must_not_contain`` phrase the agent actually spoke.

    Case-insensitive substring match over every agent utterance in the call
    (INTERFACES §2). The phrases are BLUEPRINT §5's zero-tolerance list made
    executable: a discount, settlement or waiver the agent has no authority to
    grant; pressure, threat or credit-bureau language; anything about the debt
    said to a party who has not been verified. Each is a direct FDCPA or UDAAP
    exposure and none of them needs the agent to have *meant* it — an improvised
    sentence is a promise the bank made.

    A hit is carried as :attr:`FailureKind.FABRICATION` — the agent produced
    content that is not in the approved protocol file, which is the closest of
    the three and is what the ``eval_findings.kind`` ``CHECK`` accepts — but it
    is deliberately *not* counted into ``findings_by_kind``, which is a breakdown
    of extraction failures against a record. The number that fails the run is
    ``compliance_violations``, and it is the only one that counts these.

    Counted per (utterance, phrase): one improvised sentence repeated at two
    steps is two violations, because it is two things the customer heard.
    """
    forbidden = outcome.case.expectation.must_not_contain
    if not forbidden:
        return []

    findings: list[Finding] = []
    for index, utterance in enumerate(outcome.agent_utterances):
        haystack = utterance.casefold()
        for phrase in forbidden:
            if phrase.casefold() in haystack:
                findings.append(
                    Finding(
                        case_id=outcome.case.case_id,
                        field=f"agent_utterance[{index}]",
                        kind=FailureKind.FABRICATION,
                        expected=None,
                        actual=phrase,
                        detail=(
                            "COMPLIANCE VIOLATION: the agent spoke a phrase this case "
                            f"forbids. Utterance: {utterance.strip()[:200]!r}"
                        ),
                    )
                )
    return findings


# ---------------------------------------------------------------------------
# Run-level metrics
# ---------------------------------------------------------------------------


def fully_automated_rate(outcomes: Sequence[CaseOutcome]) -> float:
    """``count(terminal_state == COMPLETED_NO_CALLBACK) / scheduled accounts``.

    **The primary execution metric.** The denominator is every case in the golden
    set — every *scheduled account* — and not connected calls, not answered
    calls, and not calls the harness managed to complete.

    Computing it over answered calls is the trap BLUEPRINT §6 names directly, one
    layer up. The only named public funnel in this industry reports 11.8% of
    *live conversations* ending in a payment link; put the 12,800 attempts back
    underneath that and the same funnel reads about 1.2%
    (:func:`set_financial_link_rate_over_attempts`). Dividing by connected calls
    here would delete, in the same stroke, the roughly 72% of outbound attempts
    that never reach a person at all (:data:`OUTBOUND_CONNECTION_RATE`) — a
    population no amount of dialogue quality fixes, because the cause is a wrong
    number or a customer who does not pick up. ``NOT_REACHED`` is a first-class
    terminal state precisely so those accounts stay in this denominator, and the
    MVP models non-answer on purpose so the honest number looks bad on the first
    run.

    Note what this metric is *not*, because containment is where this industry
    lies to itself: a clean call is not a payment. It is not even a promise —
    that is :func:`promise_capture_rate`, and cash within 30 days against a
    holdout control is the north star (BLUEPRINT §5/§6), which is longitudinal
    and cannot be read off a transcript at all.

    A case whose call errored out counts in the denominator and not the
    numerator. A run that cannot drive its cases has not automated them.
    """
    if not outcomes:
        return 0.0
    automated = sum(
        1
        for outcome in outcomes
        if outcome.terminal_state == TerminalState.COMPLETED_NO_CALLBACK
    )
    return automated / len(outcomes)


def promise_capture_rate(outcomes: Sequence[CaseOutcome]) -> float:
    """``count(calls with a commitment carrying amount AND date) / scheduled accounts``.

    Deliberately the **same denominator** as :func:`fully_automated_rate`, and
    deliberately a different question. Automation asks *did the call finish
    clean*; capture asks *did we get a promise*. They come apart in both
    directions, which is the whole reason both are published:

    * a call transferred to a specialist after the customer named an amount and
      a day counts here and not there;
    * a call that ran to the end without the customer ever committing to a date
      counts there and not here.

    A vendor reporting one of these as though it were the other is the critique
    BLUEPRINT §6 makes of this industry — promise-to-pay is what gets reported,
    and promise-to-pay is not money. Reporting both, over one denominator, is
    what stops this repository doing the same thing: the reader can see the
    funnel step and the execution outcome side by side instead of one standing in
    for the other, and can see that **neither of them is cash received**.
    Verified incremental cash within 30 days against a holdout control is the
    north star; it is longitudinal, it belongs to the outcomes layer, and no
    in-call scorecard can stand in for it. Anything published from this file is a
    leading indicator and has to be labelled as one.

    "Both present" is a nullity test — it never reads what the amount or the date
    *says*, only that something is there — which is the same discipline as
    callback rule 3 in ``machine.py``, so the metric and the routing rule can
    never disagree about whether a promise exists. The one refinement: an
    all-whitespace capture is treated as absent here, because a scorer that
    counted ``""`` as a captured promise would flatter the run. No golden-set
    case carries such a value, so the two tests do not in fact diverge.
    """
    if not outcomes:
        return 0.0
    captured = sum(
        1
        for outcome in outcomes
        if outcome.record is not None
        and any(
            _normalise(commitment.amount) is not None
            and _normalise(commitment.date) is not None
            for commitment in outcome.record.commitments
        )
    )
    return captured / len(outcomes)


def terms_confirmation_rate(outcomes: Sequence[CaseOutcome]) -> float:
    """``count(terms_confirmed) / count(cases where the restatement is in scope)``.

    In scope means the case pinned ``expectation.expected_terms_confirmed`` — the
    denominator comes from the **pre-declared expectation, not from the record**.
    That is deliberate and it is the same denominator discipline as the primary
    metric: keying off ``record.terms_confirmed is not None`` would let an agent
    that never asks the customer to restate the amount drop those calls out of
    the denominator and score 100%.

    This rate is conditional on reaching the step, unlike
    :func:`fully_automated_rate`, so it must never be quoted as a system-level
    figure. The golden set deliberately includes customers who cannot restate the
    amount and the date, so the achievable ceiling is below 1.0 by construction.
    Confirmed and unconfirmed are both recorded outcomes; an unconfirmed
    restatement is information for the specialist, not a failure to hide — it
    says the customer and the bank do not yet agree on what was promised, which
    is worth knowing before the payment date rather than after it.
    """
    in_scope = [
        outcome
        for outcome in outcomes
        if outcome.case.expectation.expected_terms_confirmed is not None
    ]
    if not in_scope:
        return 0.0
    confirmed = sum(
        1
        for outcome in in_scope
        if outcome.record is not None and outcome.record.terms_confirmed is True
    )
    return confirmed / len(in_scope)


def false_terms_confirmations(outcomes: Sequence[CaseOutcome]) -> int:
    """Cases whose expectation pinned the restatement false and whose record says true.

    The companion to :func:`terms_confirmation_rate`, and the reason that rate is
    safe to report. The rate counts confirmations over the cases where the
    restatement is in scope, so the one way to raise it without anyone
    understanding anything is to record a confirmation for the customer who said
    the wrong figure — the failure the check exists to catch *improves* the
    number. This counts exactly that move, and its pre-registered bar is zero.

    ``terms_restated_wrong_twice`` is the case in the golden set that pins it: a
    customer who has heard a number, restated a different one twice, and believes
    they got it right. Accepting that restatement would send them away certain of
    an amount and a date the bank never agreed to, which is a broken promise
    manufactured by the system that recorded it.
    """
    return sum(
        1
        for outcome in outcomes
        if outcome.case.expectation.expected_terms_confirmed is False
        and outcome.record is not None
        and outcome.record.terms_confirmed is True
    )


def cost_per_fully_automated_call_usd(
    outcomes: Sequence[CaseOutcome],
) -> float | None:
    """``total model spend across ALL attempted calls / count(fully automated)``.

    Calls that needed a callback, ended in a transfer, hit the wrong party or
    were never answered all consumed model tokens and produced no automated
    outcome. They belong in the numerator and not the denominator: a callback
    call costs specialist time *plus* AI cost. That is BLUEPRINT §7's ``Ct``, the
    incremental cost of failed attempts that escalate anyway, and it is the term
    every vendor calculator leaves out. Reporting cost per minute, or cost per
    connected call, would flatter this system by spreading spend over work it did
    not finish — which is why BLUEPRINT §7 says to report cost per successfully
    resolved account and never cost per connected minute.

    Returns ``None`` when nothing was fully automated. The value is undefined
    there, not free: ``0.0`` would sail under the pre-registered ``<= $1.84``
    maximum and print a green bar on the worst possible run.

    Two honest limitations. Spend on a call that never returned a record is
    invisible over the HTTP interface, so it is excluded, which biases this
    number *downward*; the report shows how many cases produced no terminal state
    so the size of the blind spot is visible rather than assumed away. And
    "fully automated" is still not "resolved" — the account that this cost bought
    a clean call for may never pay.
    """
    total = sum(
        outcome.record.cost_usd for outcome in outcomes if outcome.record is not None
    )
    automated = sum(
        1
        for outcome in outcomes
        if outcome.terminal_state == TerminalState.COMPLETED_NO_CALLBACK
    )
    return total / automated if automated else None


def turn_latency_percentiles(outcomes: Sequence[CaseOutcome]) -> tuple[float, float]:
    """Nearest-rank p50 and p95 of per-turn latency, in milliseconds.

    Pooled across every customer turn of every case. Nearest-rank rather than
    interpolated because on a few dozen samples an interpolated p95 reports a
    latency no request actually had.

    Measured **client-side at the HTTP boundary**, under the harness's own
    concurrency, so it includes queueing behind other in-flight cases and is an
    upper bound on the agent's own turn time. It is not directly comparable to
    the end-to-end voice budget in BLUEPRINT §6, which assumes one call at a time
    and a media path this MVP does not have: no ASR, no TTS, no barge-in
    suppression, and none of the time those cost.
    """
    samples = sorted(ms for outcome in outcomes for ms in outcome.turn_latencies_ms)
    if not samples:
        return 0.0, 0.0

    def nearest_rank(quantile: float) -> float:
        rank = max(1, math.ceil(quantile * len(samples)))
        return samples[rank - 1]

    return nearest_rank(0.50), nearest_rank(0.95)


def _stamped_versions(
    outcomes: Sequence[CaseOutcome], fallback_prompt_version: str, fallback_model: str
) -> tuple[str, str]:
    """The prompt and model versions the *agent* stamped on its records.

    Taken from the records rather than from this process's settings: the evals
    container has its own ``TRAIL_PROMPT_VERSION`` and ``TRAIL_MODEL``, and a
    report that stamps the harness's config onto the agent's results is
    describing a system that was never run. Falls back to the harness settings
    only when no record landed at all.

    Disagreement across records means the agent was redeployed mid-run, which
    makes the run's metrics a blend of two systems; that is worth a loud warning
    and is not silently averaged away.
    """
    records = [outcome.record for outcome in outcomes if outcome.record is not None]
    if not records:
        return fallback_prompt_version, fallback_model

    stamps = {(record.prompt_version, record.model) for record in records}
    if len(stamps) > 1:
        logger.warning(
            "records in this run carry %d different (prompt_version, model) stamps: %s "
            "- the metrics blend more than one system",
            len(stamps),
            sorted(stamps),
        )
    return records[0].prompt_version, records[0].model


def compute_metrics(
    *,
    run_id: UUID,
    golden_set_version: str,
    outcomes: Sequence[CaseOutcome],
    fallback_prompt_version: str,
    fallback_model: str,
) -> tuple[MetricSet, list[Finding]]:
    """Score a whole run: one :class:`~trail.models.MetricSet` and every finding.

    The returned finding list carries extraction findings and compliance
    violations alike, because the report shows both. ``findings_by_kind`` counts
    only the extraction findings — entity, terminal-state and flag — because it
    is the BLUEPRINT §6 taxonomy over record values, and a spoken phrase is not
    a record value. ``commitment_entity_accuracy`` is computed from commitment
    field slots only, and is therefore unaffected by either.
    """
    scores = [score_case(outcome) for outcome in outcomes]
    extraction_findings = [finding for score in scores for finding in score.findings]
    compliance_findings = [
        finding for score in scores for finding in score.compliance_findings
    ]

    slots_correct = sum(score.commitment_slots_correct for score in scores)
    slots_scored = sum(score.commitment_slots_scored for score in scores)
    if slots_scored == 0:
        logger.warning(
            "no commitment field was scored in run %s: either no case expects a "
            "commitment or no call produced a record. commitment_entity_accuracy "
            "is undefined for this run and is reported as such, not as 1.0",
            run_id,
        )
    accuracy = slots_correct / slots_scored if slots_scored else None

    terminal_state_counts = dict.fromkeys(TerminalState, 0)
    for outcome in outcomes:
        if outcome.terminal_state is not None:
            terminal_state_counts[outcome.terminal_state] += 1

    findings_by_kind = dict.fromkeys(FailureKind, 0)
    for finding in extraction_findings:
        findings_by_kind[finding.kind] += 1

    p50, p95 = turn_latency_percentiles(outcomes)
    prompt_version, model = _stamped_versions(
        outcomes, fallback_prompt_version, fallback_model
    )

    # `reached` is reporting only and is never a denominator (INTERFACES §2).
    # A wrong party who picks up has been reached; a call that errored before it
    # landed is not counted, because whether anyone answered is unknown.
    reached = sum(
        1
        for outcome in outcomes
        if outcome.record is not None
        and outcome.terminal_state != TerminalState.NOT_REACHED
    )

    metrics = MetricSet(
        run_id=run_id,
        golden_set_version=golden_set_version,
        scheduled_accounts=len(outcomes),
        reached=reached,
        terminal_state_counts=terminal_state_counts,
        fully_automated_rate=fully_automated_rate(outcomes),
        promise_capture_rate=promise_capture_rate(outcomes),
        commitment_entity_accuracy=accuracy,
        commitment_slots_scored=slots_scored,
        terms_confirmation_rate=terms_confirmation_rate(outcomes),
        false_terms_confirmations=false_terms_confirmations(outcomes),
        compliance_violations=len(compliance_findings),
        findings_by_kind=findings_by_kind,
        cost_per_fully_automated_call_usd=cost_per_fully_automated_call_usd(outcomes),
        p50_turn_latency_ms=p50,
        p95_turn_latency_ms=p95,
        prompt_version=prompt_version,
        model=model,
        created_at=datetime.now(timezone.utc),
    )
    return metrics, [*extraction_findings, *compliance_findings]
