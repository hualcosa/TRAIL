"""Renders an eval run with ``rich``.

Two rules shape everything below.

**Every number is printed next to its comparator.** A fully-automated rate of
41% is meaningless on its own; beside the cold-launch end of the voice
containment range practitioners report it is a result, and beside SET
Financial's flattering 11.8% "live-to-link" headline it is an argument. A cost
per automated call is meaningless until it sits next to what one self-service
contact already costs the bank. The comparator column is not decoration — it is
the only thing that makes the scorecard a claim rather than a readout.

Collections makes that rule harder to obey than the domain this harness was
ported from, and the report says so rather than hiding it. There is no
peer-reviewed deployment here to stand beside. The best available public
figures are vendor-reported or practitioner-reported (BLUEPRINT §4), so every
comparator below carries its **evidence grade** in the cell with it. A number
whose provenance travels one column away from it will eventually be quoted
without it.

**Compliance violations are impossible to miss.** They render in red, above the
metrics table, before anything else the reader might feel good about. A run
with a compliance violation has crossed the boundary BLUEPRINT §5 draws in
zero-tolerance terms — a concession the agent has no authority to grant,
pressure or threat language, or the debt disclosed to a party who was never
verified — and nothing else on the page trades against that.
"""

from __future__ import annotations

from datetime import datetime

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from trail.evals.metrics import (
    COST_PER_ASSISTED_CONTACT_USD,
    COST_PER_SELF_SERVICE_CONTACT_USD,
    OUTBOUND_CONNECTION_RATE,
    SET_FINANCIAL_LIVE_TO_LINK_RATE,
    VOICE_CONTAINMENT_TUNED_RANGE,
    ThresholdResult,
    check_thresholds,
    set_financial_link_rate_over_attempts,
)
from trail.models import EvalRun, EvalRunStatus, FailureKind, MetricSet, TerminalState

MAX_FINDINGS_SHOWN = 25
"""Findings are truncated in the console; the full list lives in Postgres."""

_STATUS_STYLE = {
    EvalRunStatus.RUNNING: "yellow",
    EvalRunStatus.COMPLETED: "green",
    EvalRunStatus.FAILED: "bold red",
}

_KIND_NOTE = {
    FailureKind.OMISSION: "a fact the customer stated is absent from the record",
    FailureKind.FABRICATION: "a value in the record the customer never stated",
    FailureKind.WRONG_VALUE: "present in both, and different",
}


def _optional(value: float | None, template: str) -> str:
    """Render a metric that may have no value on this run.

    "undefined" rather than a zero or a one, everywhere it appears. A rate with
    an empty denominator is not a perfect score and a cost with no automated
    calls to divide by is not free, and printing either as a number is how a run
    that produced nothing comes to look like a run that went well.
    """
    return "undefined" if value is None else template.format(value)


def render_report(run: EvalRun, console: Console | None = None) -> None:
    """Print a full scorecard for ``run``.

    Safe to call on an unfinished or failed run: without metrics it prints the
    header and says so, rather than inventing zeros that would read as a
    quality result.
    """
    out = console or Console()
    out.print(_header(run))

    if run.metrics is None:
        out.print(
            Panel(
                Text(
                    "This run produced no metrics. A FAILED run without metrics means "
                    "the harness could not drive the golden set at all — an "
                    "infrastructure failure, not a quality result. Check the evals "
                    "container logs and whether the agent was reachable.",
                    style="bold red"
                    if run.status is EvalRunStatus.FAILED
                    else "yellow",
                ),
                title="no metrics",
                border_style="red" if run.status is EvalRunStatus.FAILED else "yellow",
            )
        )
        return

    metrics = run.metrics
    results = check_thresholds(metrics)

    out.print(_compliance_banner(metrics))
    out.print(_metrics_table(metrics, results))
    out.print(_terminal_state_table(metrics))
    out.print(_taxonomy_table(metrics))
    if run.findings:
        out.print(_findings_table(run))
    out.print(_regression_panel(run))
    out.print(_footnotes(metrics, results))


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _header(run: EvalRun) -> Panel:
    """Run identity: who ran, against which golden set, with which model."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim")
    grid.add_column()

    status = Text(run.status.value, style=_STATUS_STYLE[run.status])
    grid.add_row("run", str(run.run_id))
    grid.add_row("status", status)
    grid.add_row("started", _timestamp(run.started_at))
    grid.add_row("finished", _timestamp(run.finished_at))
    if run.metrics is not None:
        grid.add_row("golden set", run.metrics.golden_set_version)
        grid.add_row("prompt version", run.metrics.prompt_version)
        grid.add_row("model", run.metrics.model)
    if run.regression_vs is not None:
        grid.add_row("compared against", str(run.regression_vs))

    return Panel(
        grid,
        title="Banco Aurora — early-stage collections agent · eval run",
        border_style="cyan",
    )


def _compliance_banner(metrics: MetricSet) -> Panel:
    """The compliance-boundary result, rendered before anything reassuring."""
    if metrics.compliance_violations == 0:
        return Panel(
            Text("0 compliance violations", style="bold green"),
            title="compliance boundary",
            border_style="green",
        )

    body = Text()
    body.append(
        f"{metrics.compliance_violations} COMPLIANCE VIOLATION"
        f"{'S' if metrics.compliance_violations != 1 else ''}\n\n",
        style="bold white on red",
    )
    body.append(
        "The agent spoke phrases the golden set forbids: a discount, settlement or "
        "waiver it has no authority to grant, pressure or threat language, or "
        "something about the debt said to a party who has not been verified. Each "
        "one is a zero-tolerance failure in BLUEPRINT §5 and a direct FDCPA or "
        "UDAAP exposure.\n\n"
        "This run FAILS. It is not usable as a regression baseline, and no other "
        "metric on this page offsets it. The offending utterances are in the findings "
        "table below, marked COMPLIANCE VIOLATION.",
        style="bold red",
    )
    return Panel(body, title="ZERO-TOLERANCE FAILURE", border_style="bold red")


def _metrics_table(metrics: MetricSet, results: list[ThresholdResult]) -> Panel:
    """The metrics, each beside its pre-registered bar and its published comparator."""
    contained_low, contained_high = VOICE_CONTAINMENT_TUNED_RANGE
    link_rate_over_attempts = set_financial_link_rate_over_attempts()

    comparators = {
        "fully_automated_rate": (
            f"cold launch, low 30s — against {contained_low:.0%}–{contained_high:.0%} "
            f"for a tuned deployment (grade I, practitioner-reported; "
            f"'containment' is defined differently by everyone who reports it)"
        ),
        "promise_capture_rate": (
            f"SET Financial {SET_FINANCIAL_LIVE_TO_LINK_RATE:.1%} live-to-link "
            f"(grade V) — the same funnel is {link_rate_over_attempts:.1%} with all "
            f"12,800 attempts back underneath it, and both stop at a link sent"
        ),
        "commitment_entity_accuracy": (
            f"no published field-level comparator exists · "
            f"{metrics.commitment_slots_scored} field(s) scored"
        ),
        "terms_confirmation_rate": (
            "no published comparator exists — DECLARED in advance, and the golden "
            "set holds customers who cannot restate"
        ),
        "false_terms_confirmations": (
            "zero tolerance — the one way to raise the rate above by accepting a "
            "wrong amount or a wrong date as correct"
        ),
        "compliance_violations": "zero tolerance — not a rate, a gate",
        "cost_per_fully_automated_call_usd": (
            f"one self-service contact ${COST_PER_SELF_SERVICE_CONTACT_USD:.2f}, "
            f"one assisted contact ${COST_PER_ASSISTED_CONTACT_USD:.2f} (grade I) — "
            f"the bar is the cheaper one, not the person"
        ),
        "p95_turn_latency_ms": "voice budget p95 < 1,500 ms (BLUEPRINT §6)",
    }

    formatted = {
        "fully_automated_rate": f"{metrics.fully_automated_rate:.1%}",
        "promise_capture_rate": f"{metrics.promise_capture_rate:.1%}",
        "commitment_entity_accuracy": _optional(
            metrics.commitment_entity_accuracy, "{:.1%}"
        ),
        "terms_confirmation_rate": f"{metrics.terms_confirmation_rate:.1%}",
        "false_terms_confirmations": str(metrics.false_terms_confirmations),
        "compliance_violations": str(metrics.compliance_violations),
        "cost_per_fully_automated_call_usd": _optional(
            metrics.cost_per_fully_automated_call_usd, "${:,.4f}"
        ),
        "p95_turn_latency_ms": f"{metrics.p95_turn_latency_ms:,.0f} ms",
    }
    bars = {
        "fully_automated_rate": lambda v: f"≥ {v:.1%}",
        "promise_capture_rate": lambda v: f"≥ {v:.1%}",
        "commitment_entity_accuracy": lambda v: f"≥ {v:.1%}",
        "terms_confirmation_rate": lambda v: f"≥ {v:.1%}",
        "false_terms_confirmations": lambda v: f"= {v:.0f}",
        "compliance_violations": lambda v: f"= {v:.0f}",
        "cost_per_fully_automated_call_usd": lambda v: f"≤ ${v:,.2f}",
        "p95_turn_latency_ms": lambda v: f"≤ {v:,.0f} ms",
    }

    table = Table(expand=True, header_style="bold")
    table.add_column("metric", overflow="fold")
    table.add_column("this run", justify="right")
    table.add_column("pre-registered", justify="right")
    table.add_column("", justify="center", width=6)
    table.add_column("published comparator", overflow="fold")

    for result in results:
        name = result.threshold.metric
        if result.undefined:
            verdict = Text("n/a", style="yellow")
        elif result.passed:
            verdict = Text("PASS", style="green")
        else:
            verdict = Text("FAIL", style="bold red")
        table.add_row(
            name,
            Text(formatted[name], style="bold"),
            bars[name](result.threshold.value),
            verdict,
            comparators[name],
        )

    table.add_section()
    table.add_row(
        "p50_turn_latency_ms",
        f"{metrics.p50_turn_latency_ms:,.0f} ms",
        Text("reported", style="dim"),
        "",
        "voice budget p50 < 800 ms (BLUEPRINT §6)",
    )
    table.add_row(
        "scheduled accounts",
        str(metrics.scheduled_accounts),
        Text("denominator", style="dim"),
        "",
        "the denominator of both rates above — never 'live conversations'",
    )
    table.add_row(
        "reached",
        str(metrics.reached),
        Text("reporting only", style="dim"),
        "",
        f"about {OUTBOUND_CONNECTION_RATE:.0%} of outbound collections attempts reach "
        "a live person (grade V); no amount of dialogue quality fixes a number that "
        "does not answer",
    )

    return Panel(table, title="metrics", border_style="cyan")


def _terminal_state_table(metrics: MetricSet) -> Panel:
    """Where the calls actually ended, as a share of scheduled accounts."""
    table = Table(expand=True, header_style="bold")
    table.add_column("terminal state")
    table.add_column("calls", justify="right")
    table.add_column("of scheduled", justify="right")

    scheduled = metrics.scheduled_accounts or 1
    for state in TerminalState:
        count = metrics.terminal_state_counts.get(state, 0)
        style = "bold green" if state is TerminalState.COMPLETED_NO_CALLBACK else None
        table.add_row(
            Text(state.value, style=style),
            Text(str(count), style=style),
            Text(f"{count / scheduled:.1%}", style=style),
        )

    landed = sum(metrics.terminal_state_counts.values())
    unresolved = metrics.scheduled_accounts - landed
    if unresolved:
        table.add_section()
        table.add_row(
            Text("no terminal state (harness or agent error)", style="yellow"),
            Text(str(unresolved), style="yellow"),
            Text(f"{unresolved / scheduled:.1%}", style="yellow"),
        )

    return Panel(table, title="terminal states", border_style="cyan")


def _taxonomy_table(metrics: MetricSet) -> Panel:
    """The failure taxonomy, never collapsed to pass/fail."""
    table = Table(expand=True, header_style="bold")
    table.add_column("kind")
    table.add_column("findings", justify="right")
    table.add_column("definition", overflow="fold")

    for kind in FailureKind:
        table.add_row(
            kind.value, str(metrics.findings_by_kind.get(kind, 0)), _KIND_NOTE[kind]
        )

    note = Text(
        "Omission dominates in the speech-transcription literature. A scorecard "
        "that only says 'wrong' has nothing to say about the most common failure "
        "mode, which is why these three are counted separately and never averaged "
        "together — and why BLUEPRINT §6 asks for entity error rate on amounts and "
        "dates rather than an average word error rate. "
        "These are extraction failures — discrepancies between an expectation and "
        "a record. A compliance violation is a phrase the agent spoke, not a "
        "record value, and is counted only by the gate above.",
        style="dim italic",
    )
    return Panel(
        Group(table, Text(""), note), title="failure taxonomy", border_style="cyan"
    )


def _findings_table(run: EvalRun) -> Panel:
    """Individual discrepancies, compliance violations first."""
    findings = sorted(
        run.findings, key=lambda f: (not f.detail.startswith("COMPLIANCE"), f.case_id)
    )
    shown = findings[:MAX_FINDINGS_SHOWN]

    table = Table(expand=True, header_style="bold")
    table.add_column("case")
    table.add_column("field")
    table.add_column("kind")
    table.add_column("expected", overflow="fold")
    table.add_column("actual", overflow="fold")

    for finding in shown:
        violation = finding.detail.startswith("COMPLIANCE")
        style = "bold red" if violation else None
        table.add_row(
            Text(finding.case_id, style=style),
            Text(finding.field, style=style),
            Text(
                "COMPLIANCE VIOLATION" if violation else finding.kind.value, style=style
            ),
            Text(finding.expected or "—", style=style),
            Text(finding.actual or "—", style=style),
        )

    title = f"findings ({len(run.findings)})"
    if len(findings) > len(shown):
        title += f" — showing {len(shown)}; the rest are in eval_findings"
    return Panel(table, title=title, border_style="cyan")


def _regression_panel(run: EvalRun) -> Panel:
    """What moved adversely since the baseline run."""
    if run.regression_vs is None:
        return Panel(
            Text(
                "No baseline run to compare against — regression detection was "
                "skipped.",
                style="dim",
            ),
            title="regressions",
            border_style="dim",
        )
    if not run.regressions:
        return Panel(
            Text(
                f"No metric regressed against run {run.regression_vs}.", style="green"
            ),
            title="regressions",
            border_style="green",
        )

    body = Text()
    for statement in run.regressions:
        body.append(f"• {statement}\n", style="bold yellow")
    return Panel(body, title="regressions", border_style="yellow")


def _footnotes(metrics: MetricSet, results: list[ThresholdResult]) -> Panel:
    """The four sentences a reader has to have to read the table honestly."""
    failed = [r.threshold.metric for r in results if r.passed is False]
    undefined = [r.threshold.metric for r in results if r.undefined]
    body = Text()

    automated = metrics.terminal_state_counts.get(
        TerminalState.COMPLETED_NO_CALLBACK, 0
    )
    body.append("Denominator. ", style="bold")
    body.append(
        f"fully_automated_rate is {automated}/{metrics.scheduled_accounts} scheduled "
        f"accounts. It is not computed over the {metrics.reached} calls that were "
        "answered. Dividing by answered calls is the same trap as SET Financial's "
        "11.8% live-to-link rate, one layer up — a rate conditional on reaching a "
        f"live person — and it would delete the roughly "
        f"{1 - OUTBOUND_CONNECTION_RATE:.0%} of attempts that never reach one.\n\n"
    )

    body.append("Automation, capture, and money. ", style="bold")
    body.append(
        "fully_automated_rate and promise_capture_rate share a denominator and ask "
        "different questions: automation asks whether the call finished clean, "
        "capture asks whether a promise with both an amount and a date came out of "
        "it. Either can move while the other holds, which is why both are printed. "
        "Neither is money. Promise-to-pay is what vendors report and promise-to-pay "
        "is not cash received (BLUEPRINT §6); the north star is verified cash within "
        "30 days against a holdout, it is longitudinal, and it cannot be computed "
        "from a call transcript at all.\n\n"
    )

    body.append("Thresholds. ", style="bold")
    body.append(
        "Pre-registered before the first run and not edited to make a run pass. "
        "Where each bar came from is in the comparator column beside it and in full "
        "in the rationale on every Threshold in trail.evals.metrics: two are "
        "DECLARED with no comparator worth the name, two are zero-tolerance policy "
        "rather than measurement, and of the remaining four not one stands on "
        "evidence graded P — this domain has none to stand on (BLUEPRINT §4). "
    )
    body.append(
        f"{len(failed)} of {len(results)} missed: {', '.join(failed)}.\n"
        if failed
        else "Every defined bar was met.\n"
    )
    if undefined:
        body.append(
            f"{len(undefined)} undefined on this run and scored neither way: "
            f"{', '.join(undefined)}. An empty denominator is not a pass and not a "
            "fail — it reads 'undefined' in the value column and 'n/a' in the "
            "verdict column, and counting it either way would let a run that "
            "produced nothing print a green bar.\n"
        )
    body.append("\n")

    body.append("Latency. ", style="bold")
    body.append(
        "Measured client-side at the HTTP boundary under the harness's own "
        "concurrency, so it is an upper bound on the agent's turn time and is not "
        "directly comparable to an end-to-end voice budget on a single call.",
    )

    return Panel(body, title="how to read this", border_style="dim")


def _timestamp(value: datetime | None) -> str:
    return "—" if value is None else value.isoformat(timespec="seconds")
