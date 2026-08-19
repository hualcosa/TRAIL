"""The Banco Aurora early-stage collections client.

This is a first-class layer of the system, not a debug script. In the compose
topology the client is its own service boundary (INTERFACES §8), and it is
**the seam where telephony and audio attach later**: today it reads a line of
text from a keyboard and writes a line of text to a terminal; tomorrow the same
loop reads a transcript from an ASR stream and writes a sentence to a TTS
engine over a telephony transport. What does *not* change is everything below
the seam — the HTTP contract in INTERFACES §3, the state machine, the approved
Portuguese text, the traces, the record. Media and transport are a media and
transport problem, and keeping them outside this boundary is why the MVP can be
text-only without being a toy.

BLUEPRINT §8 makes that seam a design commitment rather than a convenience: the
architecture is cascaded (STT → LLM → TTS) precisely because a regulated
transaction needs a **text checkpoint before speech**, so a disclosure and an
amount can be verified before they are spoken. This module is where that
checkpoint is read out loud by a human today and handed to a synthesiser later.
A native speech-to-speech design has no such checkpoint, and the compliance gate
this whole repository is built around would have nowhere to stand.

Three commands:

``trail chat [--case CASE_ID]``
    One interactive call. You type as the customer, the agent answers, and the
    loop runs to a terminal state and prints the resulting
    :class:`~trail.models.CallRecord`. ``--case`` preloads a golden-set
    account so a demo has realistic identity and balance data. This is the
    live-demo path.

``trail eval [--compare-to RUN_ID]``
    Starts a golden-set run on the evals service, polls until it finishes, and
    renders the report — our numbers next to the published baselines they must
    be judged against, the failure taxonomy scored by kind, and any regressions
    against the comparison run.

``trail health``
    Whether the agent and evals services are up.

The client holds no collections logic and makes no judgment about the debt. It
renders what the agent said and what the record contains; it does not sort,
score or rank anything, because the queue this feeds is deliberately unordered
and every classification in this system is made by the collections specialist
who reviews the record (BLUEPRINT §7).

Errors are surfaced as a message and an actionable hint, never as a traceback:
a refused connection names the service, its URL, and how to start it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from trail.config import Settings, get_settings
from trail.evals.metrics import (
    OUTBOUND_CONNECTION_RATE,
    SET_FINANCIAL_FUNNEL,
    SET_FINANCIAL_LIVE_TO_LINK_RATE,
    VOICE_CONTAINMENT_TUNED_RANGE,
    ThresholdResult,
    check_thresholds,
    set_financial_link_rate_over_attempts,
)
from trail.models import (
    AccountProfile,
    CallRecord,
    EvalRun,
    EvalRunStatus,
    FailureKind,
    MetricSet,
    StartCallRequest,
    StartCallResponse,
    StartEvalRequest,
    StartEvalResponse,
    Step,
    SyntheticCase,
    TerminalState,
    TurnRequest,
    TurnResponse,
)

# --------------------------------------------------------------------------
# Published baselines (BLUEPRINT §4, §6)
#
# Every figure the report puts next to one of ours traces to a citable source,
# named in the table, and carries the evidence grade that source earned:
# **I** independent, **V** vendor-reported. Collections has nothing graded P —
# there is no peer-reviewed deployment to stand beside — and printing the grade
# in the cell is what stops the weakest number in the table from being read as
# though it were the strongest.
#
# The numbers themselves are imported from `trail.evals.metrics` rather than
# restated here. A comparator that exists in two files is a comparator that
# will eventually disagree with itself, and the version the CLI prints is the
# one a reader would then quote. Only the citation strings are local, because
# only they are about presentation.
# --------------------------------------------------------------------------

CONTAINMENT_SOURCE = (
    "Practitioner reporting, tuned deployments (grade I). 'Containment' is "
    "defined differently by almost everyone who reports it"
)
SET_FINANCIAL_SOURCE = (
    "SET Financial funnel, 4 weeks (grade V). Vendor-reported, and it stops at "
    "a payment link sent rather than at money received"
)
CONNECTION_SOURCE = (
    "Razorpay disclosed outbound benchmark (grade V). v0 is inbound, so this is "
    "the ceiling the next phase inherits, not a bar on this run"
)

# --------------------------------------------------------------------------
# Client configuration
#
# `agent_base_url` lives in the shared Settings (INTERFACES §7, "used by evals
# and the CLI"). There is no settings field for the evals service, so its URL
# is a flag defaulting to TRAIL_EVALS_BASE_URL and then to the compose
# hostname, matching how config.py defaults `agent_base_url` to http://agent:8000.
# --------------------------------------------------------------------------

DEFAULT_EVALS_BASE_URL = "http://evals:8001"

HEALTH_TIMEOUT_SECONDS = 5.0
# One turn is one extraction call to gpt-5.6-luna with reasoning off.
TURN_TIMEOUT_SECONDS = 180.0
# The evals service answers POST /runs immediately and runs in a background
# task, so this bounds a single request, never the run itself.
EVAL_TIMEOUT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 2.0

MAX_FINDINGS_SHOWN = 20

AGENT_START_HINT = (
    "Start it with `docker compose up -d agent`, or point the client somewhere "
    "else with --agent-url / TRAIL_AGENT_BASE_URL."
)
EVALS_START_HINT = (
    "Start it with `docker compose up -d evals`, or point the client somewhere "
    "else with --evals-url / TRAIL_EVALS_BASE_URL."
)

THEME = Theme(
    {
        "agent": "bold cyan",
        "customer": "bold white",
        "meta": "dim",
        "heading": "bold",
        "ok": "bold green",
        "warn": "bold yellow",
        "bad": "bold red",
        "state.completed_no_callback": "bold green",
        "state.completed_needs_callback": "bold yellow",
        "state.transferred_to_human": "bold magenta",
        "state.not_right_party": "bold red",
        "state.not_reached": "bold red",
    }
)

# What each terminal state means for the primary metric. Shown with the record
# because "completed" is the word this industry uses to flatter itself, and a
# viewer needs to see which bucket this call actually landed in.
TERMINAL_STATE_GLOSS: dict[TerminalState, str] = {
    TerminalState.COMPLETED_NO_CALLBACK: (
        "Ran to the end with no specialist callback and no transfer. This is the "
        "numerator of the primary metric. It is not a payment."
    ),
    TerminalState.COMPLETED_NEEDS_CALLBACK: (
        "Completed, but a specialist must call back. Not counted as fully "
        "automated: this call costs specialist time plus AI spend."
    ),
    TerminalState.TRANSFERRED_TO_HUMAN: (
        "Handed to a person mid-call. Not counted as fully automated, and the "
        "record does not say why - routing is uniform, classification is not the "
        "agent's job (BLUEPRINT §7)."
    ),
    TerminalState.NOT_RIGHT_PARTY: (
        "Identity was not confirmed, so nothing about the debt was disclosed and "
        "no message was left."
    ),
    TerminalState.NOT_REACHED: (
        "Nobody was reached. Stays in the denominator - no amount of dialogue "
        "quality fixes a number that does not answer."
    ),
}


class CliError(Exception):
    """An error the user can act on, rendered as a message plus a hint.

    Nothing raised as a :class:`CliError` ever reaches the user as a traceback.
    """

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class ServiceUnreachable(CliError):
    """The service did not answer at all: nothing listening, or the wrong URL.

    Distinct from a service that answered with an error status, which
    ``trail health`` reports as unhealthy rather than down.
    """


@dataclass(frozen=True)
class Service:
    """An HTTP service the client talks to, and how to start it if it is down."""

    name: str
    base_url: str
    start_hint: str


class ServiceClient:
    """A thin ``httpx.Client`` that raises :class:`CliError` instead of leaking transport errors.

    Every failure mode a user actually hits — the container is not up, the
    service is slow, the service answered 409 — becomes a sentence naming the
    service and its URL.
    """

    def __init__(self, service: Service, timeout: float) -> None:
        self._service = service
        self._timeout = timeout
        self._client = httpx.Client(base_url=service.base_url, timeout=timeout)

    def __enter__(self) -> ServiceClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._client.close()

    def request(self, method: str, path: str, *, json_body: Any = None) -> Any:
        """Send a request and return the decoded JSON body, or raise :class:`CliError`."""
        try:
            response = self._client.request(method, path, json=json_body)
        except httpx.ConnectError as exc:
            raise ServiceUnreachable(
                f"Cannot reach the {self._service.name} service at {self._service.base_url}.",
                self._service.start_hint,
            ) from exc
        except httpx.TimeoutException as exc:
            raise ServiceUnreachable(
                f"The {self._service.name} service at {self._service.base_url} did not "
                f"respond within {self._timeout:.0f}s.",
                f"Check `docker compose logs -f {self._service.name}`.",
            ) from exc
        except httpx.HTTPError as exc:
            raise ServiceUnreachable(
                f"Request to the {self._service.name} service at "
                f"{self._service.base_url} failed: {exc}",
                self._service.start_hint,
            ) from exc

        if response.is_success:
            return response.json()
        raise CliError(
            f"The {self._service.name} service answered {response.status_code} for "
            f"{method} {path}: {_error_detail(response)}",
            f"Check `docker compose logs -f {self._service.name}`.",
        )


def _error_detail(response: httpx.Response) -> str:
    """Pull the message out of FastAPI's ``{"detail": ...}`` body, whatever shape it is."""
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()[:500] or "<empty body>"
    if isinstance(payload, dict) and "detail" in payload:
        detail = payload["detail"]
        return detail if isinstance(detail, str) else json.dumps(detail)
    return json.dumps(payload)[:500]


# --------------------------------------------------------------------------
# Configuration resolution
# --------------------------------------------------------------------------


def _settings() -> Settings:
    """Load the shared settings, turning a missing variable into a legible error."""
    try:
        return get_settings()
    except ValidationError as exc:
        raise CliError(
            "Configuration is incomplete: trail.config.Settings could not be built.",
            "The client needs no credentials of its own, but it shares trail.config with "
            "the services and TRAIL_LLM_API_KEY has no default. Export it, or skip "
            "config entirely by passing --agent-url.\n\n" + str(exc),
        ) from exc


def _agent_service(url: str | None) -> Service:
    return Service(
        "agent", (url or _settings().agent_base_url).rstrip("/"), AGENT_START_HINT
    )


def _evals_service(url: str | None) -> Service:
    resolved = url or os.environ.get("TRAIL_EVALS_BASE_URL") or DEFAULT_EVALS_BASE_URL
    return Service("evals", resolved.rstrip("/"), EVALS_START_HINT)


def _demo_profile() -> AccountProfile:
    """The built-in demo account, from the one place it is defined.

    The account itself moved to :func:`trail.cases.demo_profile` when the demo
    UI grew a ``GET /demo/cases`` endpoint that has to offer the same one. There
    is exactly one demo customer in this system and this is a call to it, not a
    second copy: a balance edited here and not there would produce a screen and
    a terminal that disagree about what Beatriz owes.

    Imported inside the function, matching :func:`_load_golden_case` — importing
    ``trail.cases`` pulls the whole golden set in, and ``trail health`` has
    no reason to pay for that.
    """
    from trail.cases import demo_profile

    return demo_profile()


def _load_golden_case(case_id: str) -> SyntheticCase:
    """Fetch one case from the golden set, or name the ones that exist.

    Imported here rather than at module scope so ``trail health`` does not pull
    the fixture in. No import guard: the golden set is a hard dependency of the
    same installed package, so a guard could only ever convert a typo into a
    plausible-looking runtime message and let it survive a smoke test — which is
    precisely what it did.

    The known ids are listed from ``GOLDEN_SET`` rather than written down here,
    so a case added to the fixture is offered by the CLI on the same commit.
    ``canonical_cooperative`` is the happy path and the one to reach for first;
    ``asks_for_discount`` is the case this system is expected to fail.
    """
    from trail.cases import GOLDEN_SET

    by_id = {case.case_id: case for case in GOLDEN_SET}
    if case_id not in by_id:
        raise CliError(
            f"No golden-set case with id {case_id!r}.",
            "Known cases: " + ", ".join(sorted(by_id)),
        )
    return by_id[case_id]


# --------------------------------------------------------------------------
# Shared rendering helpers
# --------------------------------------------------------------------------


def _tristate(value: bool | None) -> str:
    """Render a nullable boolean. ``None`` is 'not recorded', which is not 'no'.

    The distinction is what the specialist reads the record for: a
    ``terms_confirmed`` that is false because the customer restated the wrong
    amount, and one that is null because the restatement never happened at all,
    are different facts about the call and lead to different follow-ups
    (BLUEPRINT §6). Collapsing them into "no" would delete the second one.
    """
    if value is None:
        return "[meta]not recorded[/]"
    return "[ok]yes[/]" if value else "[warn]no[/]"


def _cell(value: str | None) -> str:
    """A table cell whose text came from data rather than from this module.

    Escaped, because Rich reads square brackets as markup and this system is
    full of strings that legitimately contain them: ``Finding.field`` is
    ``commitments[0].amount``, and ``source_utterance`` is whatever the customer
    said. Empty and ``None`` render as a dash, so a blank cell is visibly a
    blank cell.
    """
    return escape(value) if value else "[meta]-[/]"


def _pct(numerator: int, denominator: int) -> str:
    return "[meta]n/a[/]" if denominator == 0 else f"{numerator / denominator:.1%}"


def _duration(seconds: float) -> str:
    """Wall time as a human reads it. An early-stage collections call is short."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(round(seconds), 60)
    return f"{minutes}m {remainder:02d}s"


def _short(identifier: UUID) -> str:
    return str(identifier)[:8]


def _state_style(state: TerminalState) -> str:
    return f"state.{state.value}"


def _kv_grid(rows: Sequence[tuple[str, str]]) -> Table:
    """A borderless label/value grid, used for every header block."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="meta", justify="right", no_wrap=True)
    grid.add_column(overflow="fold")
    for label, value in rows:
        grid.add_row(label, value)
    return grid


def _render_error(console: Console, error: CliError) -> None:
    body = Text(error.message)
    if error.hint:
        body.append("\n\n")
        body.append(error.hint, style="dim")
    console.print(
        Panel(body, title="[bad]Error[/]", border_style="red", padding=(1, 2))
    )


# --------------------------------------------------------------------------
# trail chat
# --------------------------------------------------------------------------


def _render_call_header(
    console: Console, profile: AccountProfile, case: SyntheticCase | None
) -> None:
    """Show the operator the record the agent is about to speak from.

    The account fields are printed in a neutral machine form — an ISO date, a
    bare ``Decimal`` — and deliberately **not** re-rendered into the pt-BR
    wording the customer will hear. There is exactly one call site for the
    spoken formatters, ``trail.agent.machine.slots_for_call``, and the reason
    it is exactly one is that the agent and the compliance allowlist must build
    the identical string. A second, decorative rendering in the client would be
    a second definition of what R$ 847,32 looks like, and the first time the two
    disagreed the difference would show up as a mystery compliance violation
    rather than as a diff. The screen below shows the *inputs*; the panel after
    it shows what the agent actually said.
    """
    rows = [
        (
            "Customer",
            f"[heading]{escape(profile.full_name)}[/]  "
            f"[meta]({escape(profile.account_id)})[/]",
        ),
        ("Born", profile.date_of_birth.isoformat()),
        ("CPF", escape(profile.tax_id)),
        ("Phone", escape(profile.phone)),
        ("Product", profile.product.value),
        (
            "Past due",
            f"{profile.balance_brl} BRL - due {profile.due_date.isoformat()}, "
            f"{profile.days_past_due} day(s) ago",
        ),
    ]
    if case is not None:
        rows.append(
            ("Case", f"[heading]{escape(case.case_id)}[/] - {escape(case.description)}")
        )
    console.print(
        Panel(
            _kv_grid(rows),
            title="[agent]Banco Aurora - early-stage collections call[/]",
            title_align="left",
            border_style="cyan",
            padding=(1, 2),
        )
    )
    console.print(
        "[meta]Type the customer's reply and press Enter. Ctrl-C hangs up.[/]\n"
    )


def _render_agent_turn(
    console: Console, step: Step, utterance: str, elapsed_seconds: float | None
) -> None:
    subtitle = None if elapsed_seconds is None else f"[meta]{elapsed_seconds:.1f}s[/]"
    console.print(
        Panel(
            Text(utterance),
            title=f"[agent]Assistant[/] [meta]· {step.value}[/]",
            title_align="left",
            subtitle=subtitle,
            subtitle_align="right",
            border_style="cyan",
            padding=(1, 2),
        )
    )


def _ask_customer(console: Console) -> str:
    """Read one non-empty customer utterance. An empty turn is not a turn."""
    while True:
        reply = Prompt.ask("[customer]Customer[/]", console=console).strip()
        if reply:
            console.print()
            return reply
        console.print("[meta]Say something, or press Ctrl-C to hang up.[/]")


def _render_commitments(console: Console, record: CallRecord) -> None:
    if not record.commitments:
        console.print("[meta]No promise to pay recorded.[/]\n")
        return
    table = Table(
        title="Promises to pay",
        title_justify="left",
        header_style="heading",
        expand=True,
    )
    table.add_column("Amount", justify="right")
    table.add_column("Date")
    table.add_column("Method")
    table.add_column("Heard as", overflow="fold", ratio=2)
    for commitment in record.commitments:
        table.add_row(
            _cell(commitment.amount),
            _cell(commitment.date),
            _cell(commitment.method.value if commitment.method else None),
            f"[meta]{escape(commitment.source_utterance)}[/]",
        )
    console.print(table)
    console.print(
        "[meta]Amount and date are stored exactly as the customer said them. "
        "'mil e duzentos' is not turned into 1200 and 'sexta-feira' is not "
        "resolved into a date - resolution is inference, and inference is where "
        "the wrong figure gets manufactured.[/]\n"
    )


def _render_disputes(console: Console, record: CallRecord) -> None:
    if not record.disputes:
        console.print("[meta]Nothing disputed.[/]\n")
        return
    table = Table(
        title="Disputes", title_justify="left", header_style="heading", expand=True
    )
    table.add_column("Subject")
    table.add_column("Detail")
    table.add_column("Heard as", overflow="fold", ratio=2)
    for dispute in record.disputes:
        table.add_row(
            _cell(dispute.subject),
            _cell(dispute.detail),
            f"[meta]{escape(dispute.source_utterance)}[/]",
        )
    console.print(table)
    console.print(
        "[meta]Recorded verbatim and never assessed. 'Já paguei', 'esse valor está "
        "errado' and 'eu nunca peguei esse empréstimo' are three different facts "
        "for the specialist, and this agent is not permitted to decide which is "
        "true or which matters more.[/]\n"
    )


def _render_record(console: Console, record: CallRecord) -> None:
    """Render a finished call record, terminal state first and largest."""
    style = _state_style(record.terminal_state)
    label = record.terminal_state.value.replace("_", " ").upper()

    banner = Text(justify="center")
    banner.append(label + "\n", style=style)
    banner.append(TERMINAL_STATE_GLOSS[record.terminal_state], style="dim")
    console.print(
        Panel(
            banner,
            title="[heading]Call record[/]",
            title_align="left",
            subtitle=f"[meta]call {record.call_id}[/]",
            subtitle_align="right",
            border_style=style,
            padding=(1, 2),
        )
    )
    console.print()

    outcome = Table(
        title="Outcome", title_justify="left", header_style="heading", box=None
    )
    outcome.add_column("Field", style="meta", justify="right")
    outcome.add_column("Value")
    outcome.add_row("account", escape(record.account_id))
    outcome.add_row("consent given", _tristate(record.consent_given))
    outcome.add_row("terms confirmed", _tristate(record.terms_confirmed))
    outcome.add_row(
        "payment path",
        record.selected_path.value if record.selected_path else "[meta]none chosen[/]",
    )
    outcome.add_row(
        "contact channel confirmed", _tristate(record.contact_channel_confirmed)
    )
    outcome.add_row(
        "needs specialist review", _tristate(record.needs_specialist_review)
    )
    outcome.add_row(
        "reviewed by",
        escape(record.reviewed_by) if record.reviewed_by else "[meta]nobody yet[/]",
    )

    provenance = Table(
        title="Provenance and cost",
        title_justify="left",
        header_style="heading",
        box=None,
    )
    provenance.add_column("Field", style="meta", justify="right")
    provenance.add_column("Value")
    provenance.add_row("protocol", escape(record.protocol_version))
    provenance.add_row("prompt", escape(record.prompt_version))
    provenance.add_row("model", escape(record.model))
    provenance.add_row("wall time", _duration(record.wall_seconds))
    provenance.add_row(
        "tokens",
        f"{record.total_input_tokens:,} in / {record.total_output_tokens:,} out",
    )
    provenance.add_row("cost", f"${record.cost_usd:.4f}")

    columns = Table.grid(padding=(0, 6))
    columns.add_column()
    columns.add_column()
    columns.add_row(outcome, provenance)
    console.print(columns)
    console.print()

    _render_commitments(console, record)
    _render_disputes(console, record)

    console.print(
        "[meta]Routed to the specialist queue with every other record, in call-start "
        "order. There is no priority, no score and no propensity on this record, and "
        "there is not going to be one. Nothing here is final until a specialist "
        "reviews it.[/]"
    )


def cmd_chat(args: argparse.Namespace, console: Console) -> int:
    """Run one interactive call to a terminal state and print the record."""
    case = _load_golden_case(args.case) if args.case else None
    profile = case.profile if case else _demo_profile()

    # `docker compose run` prints its container-startup table before we get the
    # terminal. Wipe it so the call opens on a clean screen — scrollback keeps
    # it for anyone debugging. No-op when stdout is not a terminal.
    if console.is_terminal:
        console.clear()

    _render_call_header(console, profile, case)

    service = _agent_service(args.agent_url)
    with ServiceClient(service, TURN_TIMEOUT_SECONDS) as client:
        body = StartCallRequest(profile=profile, case_id=args.case).model_dump(
            mode="json"
        )
        # v0 is inbound (BLUEPRINT §3): the customer was notified and tapped to
        # call, so the call is answered rather than placed. The distinction is
        # not cosmetic — it is why there is no consent-to-call management in
        # this repository at all.
        with console.status("[meta]answering the call...[/]", spinner="dots"):
            started = StartCallResponse.model_validate(
                client.request("POST", "/calls", json_body=body)
            )
        _render_agent_turn(console, started.step, started.agent_utterance, None)

        call_id = started.call_id
        record: CallRecord | None = None
        while True:
            try:
                utterance = _ask_customer(console)
            except (EOFError, KeyboardInterrupt):
                console.print(
                    "\n[warn]Hung up mid-call.[/] [meta]The record only lands at a "
                    "terminal state, so nothing was written for "
                    f"call {call_id}.[/]"
                )
                return 130

            turn_body = TurnRequest(
                call_id=call_id, customer_utterance=utterance
            ).model_dump(mode="json")
            began = time.monotonic()
            with console.status("[meta]the assistant is working...[/]", spinner="dots"):
                turn = TurnResponse.model_validate(
                    client.request(
                        "POST", f"/calls/{call_id}/turns", json_body=turn_body
                    )
                )
            _render_agent_turn(
                console, turn.step, turn.agent_utterance, time.monotonic() - began
            )
            if turn.finished:
                record = turn.record
                break

    if record is None:
        raise CliError(
            f"The agent ended call {call_id} without returning a record.",
            "INTERFACES §3 requires `record` to be populated whenever `finished` is true.",
        )

    console.print()
    _render_record(console, record)
    return 0


# --------------------------------------------------------------------------
# trail eval
# --------------------------------------------------------------------------


def _poll_run(client: ServiceClient, run_id: UUID, console: Console) -> EvalRun:
    """Poll ``GET /runs/{run_id}`` until the run leaves ``RUNNING``."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task(f"running the golden set (run {_short(run_id)})", total=None)
        while True:
            run = EvalRun.model_validate(client.request("GET", f"/runs/{run_id}"))
            if run.status != EvalRunStatus.RUNNING:
                return run
            time.sleep(POLL_INTERVAL_SECONDS)


def _containment_comparison(rate: float, floor: float) -> tuple[str, str]:
    """Where our fully-automated rate sits against the two published anchors.

    There are two, not one: the pre-registered ``floor`` is the cold-launch end
    of the practitioner range, and the tuned band sits well above it. A rate
    between them has cleared the bar this system was allowed to declare for
    itself and is still short of what a tuned deployment reports, and that is a
    real and reportable position rather than a rounding of "below the band".
    """
    low, high = VOICE_CONTAINMENT_TUNED_RANGE
    if rate > high:
        return "ok", f"above the {high:.1%} top of the tuned containment band"
    if rate >= low:
        return "ok", f"inside the tuned containment band of {low:.1%} to {high:.1%}"
    if rate >= floor:
        return (
            "warn",
            f"above the pre-registered cold-launch floor of {floor:.1%}, below the "
            f"tuned band of {low:.1%} to {high:.1%}",
        )
    return "bad", f"below the pre-registered cold-launch floor of {floor:.1%}"


def _render_primary_metric(console: Console, metrics: MetricSet) -> None:
    results = {r.threshold.metric: r for r in check_thresholds(metrics)}
    automated = metrics.terminal_state_counts.get(
        TerminalState.COMPLETED_NO_CALLBACK, 0
    )
    floor = results["fully_automated_rate"].threshold.value
    style, verdict = _containment_comparison(metrics.fully_automated_rate, floor)

    body = Text(justify="center")
    body.append(f"{metrics.fully_automated_rate:.1%}\n", style=style)
    body.append(
        f"{automated} of {metrics.scheduled_accounts} scheduled accounts ran to the "
        "end with no specialist callback and no transfer\n",
        style="dim",
    )
    body.append(verdict + "\n\n", style=style)
    body.append(
        "The denominator is scheduled accounts, not live conversations. Eight "
        "pass/fail thresholds are pre-registered in trail.evals.metrics.THRESHOLDS "
        "and are scored below; a threshold moved after seeing the number is not a "
        "threshold, it is a description. Note what this rate is not: a finished call "
        "is not a captured promise, and a captured promise is not money.",
        style="dim",
    )
    console.print(
        Panel(
            body,
            title="[heading]Fully-automated completion rate[/]",
            title_align="left",
            subtitle="[meta]primary metric[/]",
            subtitle_align="right",
            border_style=style,
            padding=(1, 2),
        )
    )
    console.print()


def _render_baselines(console: Console, metrics: MetricSet) -> None:
    scheduled = metrics.scheduled_accounts
    low, high = VOICE_CONTAINMENT_TUNED_RANGE
    attempts, live, links, _transfers = SET_FINANCIAL_FUNNEL

    table = Table(
        title="Against the published baselines",
        title_justify="left",
        header_style="heading",
        expand=True,
    )
    table.add_column("Metric")
    table.add_column("This run", justify="right", no_wrap=True)
    table.add_column("Published", justify="right", no_wrap=True)
    table.add_column("Source", overflow="fold", ratio=2)

    table.add_row(
        "Fully-automated completion rate",
        f"{metrics.fully_automated_rate:.1%}",
        f"{low:.1%} - {high:.1%}",
        CONTAINMENT_SOURCE,
    )
    table.add_row(
        "Promise capture rate",
        f"{metrics.promise_capture_rate:.1%}",
        f"{SET_FINANCIAL_LIVE_TO_LINK_RATE:.1%}",
        SET_FINANCIAL_SOURCE,
    )
    table.add_row(
        "[meta]The same funnel over every attempt[/]",
        "[meta]-[/]",
        f"[meta]{set_financial_link_rate_over_attempts():.1%}[/]",
        f"[meta]{links:,} links over {attempts:,} attempts, rather than over the "
        f"{live:,} that reached a live person. Both numbers are true; only one of "
        "them answers the question a lender with a portfolio is asking, which is "
        "why this harness divides by scheduled accounts.[/]",
    )
    table.add_row(
        "Reached",
        _pct(metrics.reached, scheduled),
        f"{OUTBOUND_CONNECTION_RATE:.1%}",
        CONNECTION_SOURCE,
    )
    console.print(table)
    console.print()


def _optional(value: float | None, template: str) -> str:
    """Render a metric that has no value on this run as "undefined".

    A rate with an empty denominator is not a perfect score, and a cost with no
    automated calls to divide by is not free. Printing either as a number is how
    a run that produced nothing comes to look like a run that went well.
    """
    return "[meta]undefined[/]" if value is None else template.format(value)


def _verdict(result: ThresholdResult) -> str:
    """PASS, FAIL, or n/a — never a PASS on a metric that has no value."""
    if result.undefined:
        return "[meta]n/a[/]"
    return "[ok]PASS[/]" if result.passed else "[bad]FAIL[/]"


def _render_secondary_metrics(console: Console, metrics: MetricSet) -> None:
    """Every metric beside its pre-registered bar and the verdict against it.

    The bars are declared in ``trail.evals.metrics.THRESHOLDS``, before any run,
    and every one is shown whether it passed or not — showing only the failures
    makes a run look better the more bars you delete. A bar whose metric is
    undefined on this run reads ``n/a`` and is scored neither way.
    """
    results = {r.threshold.metric: r for r in check_thresholds(metrics)}
    violations = metrics.compliance_violations

    table = Table(
        title="Secondary metrics",
        title_justify="left",
        header_style="heading",
        expand=True,
    )
    table.add_column("Metric")
    table.add_column("Value", justify="right", no_wrap=True)
    table.add_column("Bar", justify="right", no_wrap=True)
    table.add_column("", justify="center", no_wrap=True)
    table.add_column("Note", overflow="fold", ratio=2)

    table.add_row(
        "Promise capture rate",
        f"{metrics.promise_capture_rate:.1%}",
        "≥ 11.8%",
        _verdict(results["promise_capture_rate"]),
        "Calls that produced a promise carrying both an amount and a date, over the "
        "same denominator as the primary metric. A different question, not a "
        "restatement of it - and still not money received.",
    )
    table.add_row(
        "Commitment entity accuracy",
        _optional(metrics.commitment_entity_accuracy, "{:.1%}"),
        "≥ 95.0%",
        _verdict(results["commitment_entity_accuracy"]),
        f"Exact match on amount, date and method, over "
        f"{metrics.commitment_slots_scored} scored field(s). Entity error rate on "
        "amounts and dates, never averaged into a word error rate (BLUEPRINT §6).",
    )
    table.add_row(
        "Terms confirmation rate",
        f"{metrics.terms_confirmation_rate:.1%}",
        "≥ 70.0%",
        _verdict(results["terms_confirmation_rate"]),
        "Customers who restated both the amount and the due date correctly.",
    )
    table.add_row(
        "False terms confirmations",
        f"[{'ok' if metrics.false_terms_confirmations == 0 else 'bad'}]"
        f"{metrics.false_terms_confirmations}[/]",
        "= 0",
        _verdict(results["false_terms_confirmations"]),
        "Zero tolerance: a case that pinned the restatement false and was recorded "
        "true. The one way to raise the rate above by accepting a wrong amount.",
    )
    table.add_row(
        "Compliance violations",
        f"[{'ok' if violations == 0 else 'bad'}]{violations}[/]",
        "= 0",
        _verdict(results["compliance_violations"]),
        "Zero tolerance: an unauthorised discount, settlement or waiver, pressure or "
        "threat language, or the debt disclosed to an unverified party "
        "(BLUEPRINT §5).",
    )
    table.add_row(
        "Cost per fully-automated call",
        _optional(metrics.cost_per_fully_automated_call_usd, "${:.4f}"),
        "≤ $1.84",
        _verdict(results["cost_per_fully_automated_call_usd"]),
        "Total spend across all attempted calls over the count of fully automated "
        "completions. Anchored on one self-service contact, not on one specialist.",
    )
    table.add_row(
        "Turn latency p50 / p95",
        f"{metrics.p50_turn_latency_ms:,.0f} / {metrics.p95_turn_latency_ms:,.0f} ms",
        "p95 ≤ 1,500 ms",
        _verdict(results["p95_turn_latency_ms"]),
        "Text-only, and pre-registered as an expected miss. The voice latency budget "
        "in BLUEPRINT §6 applies once audio attaches at the client seam.",
    )
    table.add_row(
        "Reached",
        f"{metrics.reached} of {metrics.scheduled_accounts}",
        "[meta]reporting[/]",
        "",
        "Reporting only. Never a denominator.",
    )
    console.print(table)
    console.print()


def _render_terminal_states(console: Console, metrics: MetricSet) -> None:
    table = Table(
        title="Terminal states",
        title_justify="left",
        header_style="heading",
        expand=True,
    )
    table.add_column("State")
    table.add_column("Calls", justify="right", no_wrap=True)
    table.add_column("Share of scheduled", justify="right", no_wrap=True)
    for state in TerminalState:
        count = metrics.terminal_state_counts.get(state, 0)
        table.add_row(
            f"[{_state_style(state)}]{state.value}[/]",
            str(count),
            _pct(count, metrics.scheduled_accounts),
        )
    console.print(table)
    console.print()


def _render_failure_taxonomy(console: Console, metrics: MetricSet) -> None:
    table = Table(
        title="Failure taxonomy",
        title_justify="left",
        header_style="heading",
        expand=True,
    )
    table.add_column("Kind")
    table.add_column("Findings", justify="right", no_wrap=True)
    table.add_column("Meaning", overflow="fold", ratio=2)
    meanings = {
        FailureKind.OMISSION: "In the utterance, absent from the record. Dominates in "
        "the speech-transcription literature.",
        FailureKind.FABRICATION: "In the record, absent from the utterance.",
        FailureKind.WRONG_VALUE: "In both, and different. An amount off by an order of "
        "magnitude lands here.",
    }
    for kind in FailureKind:
        table.add_row(
            kind.value,
            str(metrics.findings_by_kind.get(kind, 0)),
            meanings[kind],
        )
    console.print(table)
    console.print("[meta]Scored separately, never collapsed to pass/fail.[/]\n")


def _render_regressions(console: Console, run: EvalRun) -> None:
    if run.regression_vs is None:
        console.print(
            "[meta]No comparison run: this is the first completed run, or none was "
            "requested.[/]\n"
        )
        return
    if not run.regressions:
        console.print(
            f"[ok]No regressions[/] [meta]against run {run.regression_vs}.[/]\n"
        )
        return
    body = Text("\n".join(f"- {statement}" for statement in run.regressions))
    console.print(
        Panel(
            body,
            title=f"[bad]{len(run.regressions)} regression(s)[/]",
            title_align="left",
            subtitle=f"[meta]vs run {run.regression_vs}[/]",
            subtitle_align="right",
            border_style="red",
            padding=(1, 2),
        )
    )
    console.print()


def _render_findings(console: Console, run: EvalRun) -> None:
    if not run.findings:
        console.print("[ok]No findings.[/]\n")
        return
    table = Table(
        title=f"Findings ({len(run.findings)})",
        title_justify="left",
        header_style="heading",
        expand=True,
    )
    table.add_column("Case", no_wrap=True)
    table.add_column("Field", overflow="fold")
    table.add_column("Kind", no_wrap=True)
    table.add_column("Expected", overflow="fold")
    table.add_column("Actual", overflow="fold")
    table.add_column("Detail", overflow="fold", ratio=2)
    for finding in run.findings[:MAX_FINDINGS_SHOWN]:
        table.add_row(
            _cell(finding.case_id),
            _cell(finding.field),
            finding.kind.value,
            _cell(finding.expected),
            _cell(finding.actual),
            _cell(finding.detail),
        )
    console.print(table)
    remaining = len(run.findings) - MAX_FINDINGS_SHOWN
    if remaining > 0:
        console.print(
            f"[meta]... and {remaining} more. The full list is on "
            f"GET /runs/{run.run_id}.[/]"
        )
    console.print()


def _render_eval_run(console: Console, run: EvalRun) -> None:
    duration = (
        _duration((run.finished_at - run.started_at).total_seconds())
        if run.finished_at
        else "[meta]unfinished[/]"
    )
    metrics = run.metrics
    rows = [
        ("Run", f"[heading]{run.run_id}[/]"),
        ("Status", run.status.value),
        ("Started", run.started_at.strftime("%Y-%m-%d %H:%M:%S %Z")),
        ("Duration", duration),
    ]
    if metrics is not None:
        rows += [
            ("Golden set", escape(metrics.golden_set_version)),
            ("Prompt", escape(metrics.prompt_version)),
            ("Model", escape(metrics.model)),
        ]
    console.print(
        Panel(
            _kv_grid(rows),
            title="[heading]Golden-set eval[/]",
            title_align="left",
            border_style="cyan",
            padding=(1, 2),
        )
    )
    console.print()

    if run.status == EvalRunStatus.FAILED or metrics is None:
        console.print(
            Panel(
                Text(
                    "The run did not produce metrics. Check `docker compose logs -f evals`."
                ),
                title="[bad]Run failed[/]",
                title_align="left",
                border_style="red",
                padding=(1, 2),
            )
        )
        _render_findings(console, run)
        return

    _render_primary_metric(console, metrics)
    _render_baselines(console, metrics)
    _render_secondary_metrics(console, metrics)
    _render_terminal_states(console, metrics)
    _render_failure_taxonomy(console, metrics)
    _render_regressions(console, run)
    _render_findings(console, run)


def cmd_eval(args: argparse.Namespace, console: Console) -> int:
    """Start a golden-set run, wait for it, and render the report.

    Exits non-zero when the run failed, regressed against its comparison run,
    recorded a compliance violation, or missed a pre-registered threshold — the
    four outcomes that should stop a pipeline. A bar that exists and is never
    enforced is a bar that will be missed quietly.
    """
    service = _evals_service(args.evals_url)
    with ServiceClient(service, EVAL_TIMEOUT_SECONDS) as client:
        body = StartEvalRequest(compare_to=args.compare_to).model_dump(mode="json")
        started = StartEvalResponse.model_validate(
            client.request("POST", "/runs", json_body=body)
        )
        console.print(f"[meta]Started eval run[/] [heading]{started.run_id}[/]\n")
        try:
            run = _poll_run(client, started.run_id, console)
        except KeyboardInterrupt:
            console.print(
                f"\n[warn]Stopped waiting.[/] [meta]Run {started.run_id} continues on the "
                f"evals service; read it at {service.base_url}/runs/{started.run_id}.[/]"
            )
            return 130

    _render_eval_run(console, run)

    if run.status != EvalRunStatus.COMPLETED or run.metrics is None:
        return 1
    if run.regressions:
        return 1
    if run.metrics.compliance_violations > 0:
        return 1
    if any(result.passed is False for result in check_thresholds(run.metrics)):
        return 1
    return 0


# --------------------------------------------------------------------------
# trail health
# --------------------------------------------------------------------------


def cmd_health(args: argparse.Namespace, console: Console) -> int:
    """Check both services' ``/healthz`` and print a status table."""
    services = [_agent_service(args.agent_url), _evals_service(args.evals_url)]

    table = Table(
        title="Services", title_justify="left", header_style="heading", expand=True
    )
    table.add_column("Service", no_wrap=True)
    table.add_column("URL", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Detail", overflow="fold", ratio=2)

    hints: list[str] = []
    for service in services:
        with ServiceClient(service, HEALTH_TIMEOUT_SECONDS) as client:
            began = time.monotonic()
            try:
                payload = client.request("GET", "/healthz")
            except ServiceUnreachable as exc:
                hints.append(f"{service.name}: {service.start_hint}")
                table.add_row(
                    service.name, service.base_url, "[bad]down[/]", escape(exc.message)
                )
                continue
            except CliError as exc:
                # It answered, just not with a 2xx: reachable but broken.
                hints.append(f"{service.name}: {exc.hint}")
                table.add_row(
                    service.name,
                    service.base_url,
                    "[warn]unhealthy[/]",
                    escape(exc.message),
                )
                continue
            elapsed_ms = (time.monotonic() - began) * 1000

        status = payload.get("status") if isinstance(payload, dict) else None
        if status == "ok":
            table.add_row(
                service.name,
                service.base_url,
                "[ok]up[/]",
                f"[meta]{elapsed_ms:.0f} ms[/]",
            )
        else:
            hints.append(
                f"{service.name}: check `docker compose logs -f {service.name}`."
            )
            table.add_row(
                service.name,
                service.base_url,
                "[warn]unhealthy[/]",
                escape(f"/healthz answered {json.dumps(payload)}"),
            )

    console.print(table)
    if hints:
        console.print()
        for hint in hints:
            console.print(f"[meta]{hint}[/]")
        return 1
    return 0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

_EPILOG = """\
environment:
  TRAIL_AGENT_BASE_URL   agent service base URL  (default http://agent:8000)
  TRAIL_EVALS_BASE_URL   evals service base URL  (default http://evals:8001)

running against a local compose stack:
  TRAIL_AGENT_BASE_URL=http://localhost:8000 \\
  TRAIL_EVALS_BASE_URL=http://localhost:8001 trail health

exit codes:
  0  success
  1  a service is down, or the eval run failed, regressed, missed a
     pre-registered threshold, or violated a compliance assertion
  130  interrupted
"""


def _add_agent_url(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--agent-url",
        metavar="URL",
        default=None,
        help="Agent service base URL. Defaults to TRAIL_AGENT_BASE_URL.",
    )


def _add_evals_url(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--evals-url",
        metavar="URL",
        default=None,
        help="Evals service base URL. Defaults to TRAIL_EVALS_BASE_URL.",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the ``trail`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="trail",
        description=(
            "Client for the Banco Aurora early-stage collections agent. "
            "Text today, telephony and audio later - the HTTP contract underneath "
            "does not change."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subcommands = parser.add_subparsers(
        dest="command", required=True, metavar="COMMAND"
    )

    chat = subcommands.add_parser(
        "chat",
        help="Hold one call interactively, typing as the customer.",
        description=(
            "Answer one call and speak for the customer yourself. Runs to a terminal "
            "state and prints the resulting call record. Type in Portuguese - the "
            "agent's approved text is pt-BR and so is the extraction prompt."
        ),
    )
    chat.add_argument(
        "--case",
        metavar="CASE_ID",
        default=None,
        help=(
            "Preload a golden-set account instead of the built-in demo account, "
            "e.g. canonical_cooperative. An unknown id lists the ones that exist."
        ),
    )
    _add_agent_url(chat)
    chat.set_defaults(handler=cmd_chat)

    evaluate = subcommands.add_parser(
        "eval",
        help="Run the golden set and render the report.",
        description=(
            "Start a golden-set run on the evals service, wait for it, and render the "
            "metrics next to the published baselines, each with its evidence grade."
        ),
    )
    evaluate.add_argument(
        "--compare-to",
        metavar="RUN_ID",
        type=UUID,
        default=None,
        help="Detect regressions against this run. Defaults to the latest completed run.",
    )
    _add_evals_url(evaluate)
    evaluate.set_defaults(handler=cmd_eval)

    health = subcommands.add_parser(
        "health",
        help="Check that the agent and evals services are up.",
        description="Probe /healthz on both services and print a status table.",
    )
    _add_agent_url(health)
    _add_evals_url(health)
    health.set_defaults(handler=cmd_health)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code; never raises to the shell."""
    args = build_parser().parse_args(argv)
    # highlight=False: every colour in this client is chosen, not inferred.
    # Rich's automatic highlighter would tint numbers and paths inside customer
    # values and error hints, which reads as meaning that is not there.
    console = Console(theme=THEME, highlight=False)
    try:
        return int(args.handler(args, console))
    except CliError as exc:
        _render_error(console, exc)
        return 1
    except KeyboardInterrupt:
        console.print("\n[meta]Interrupted.[/]")
        return 130


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
