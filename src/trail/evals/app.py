"""The evals service — FastAPI on port 8001 (INTERFACES §4).

``POST /runs`` inserts a ``RUNNING`` row, schedules the run on a background
task, and returns ``202`` with the ``run_id`` immediately; a golden-set run is
minutes of model latency and holding an HTTP connection open for it would make
the harness's own timeouts part of the result. Poll ``GET /runs/{run_id}``.

The service drives the agent over HTTP and persists through :mod:`trail.db`.
It imports nothing from :mod:`trail.agent`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID

from fastapi import BackgroundTasks, FastAPI, HTTPException
from rich.console import Console

from trail.cases import GOLDEN_SET, GOLDEN_SET_VERSION
from trail.config import get_settings
from trail.db import (
    close_pool,
    get_eval_run,
    get_latest_eval_run,
    init_pool,
    insert_eval_run,
    update_eval_run,
)
from trail.evals.metrics import compute_metrics, detect_regression
from trail.evals.report import render_report
from trail.evals.runner import run_golden_set
from trail.models import (
    EvalRun,
    EvalRunStatus,
    MetricSet,
    StartEvalRequest,
    StartEvalResponse,
    SyntheticCase,
)
from trail.telemetry import configure_logging, setup_telemetry, span

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(instance: FastAPI) -> AsyncIterator[None]:
    """Open the connection pool for the process.

    Telemetry is set up at module scope, below, and not here: the FastAPI
    instrumentation works by patching ``build_middleware_stack``, which
    Starlette calls on its way into the lifespan scope.
    """
    await init_pool()
    try:
        yield
    finally:
        await close_pool()


app = FastAPI(
    title="Banco Aurora — eval harness",
    summary=(
        "Runs the golden set against the early-stage collections agent over the "
        "same HTTP interface the client uses, and scores it against pre-registered "
        "thresholds."
    ),
    lifespan=lifespan,
)

# Module scope, with the app — same contract as the agent service, and the two
# call it identically so the pair cannot drift into emitting different spans.
configure_logging()
setup_telemetry(get_settings().service_name, app)


def resolve_golden_set(version: str | None) -> tuple[str, Sequence[SyntheticCase]]:
    """Resolve a requested golden-set version to the cases it names.

    Only one golden set exists. An unknown version is rejected rather than
    silently served from the current one: a run stamped with a version it did not
    actually execute is worse than no run, because it makes two incomparable
    scorecards look comparable.
    """
    if version is None or version == GOLDEN_SET_VERSION:
        return GOLDEN_SET_VERSION, GOLDEN_SET
    raise ValueError(
        f"unknown golden set version {version!r}; this build carries "
        f"{GOLDEN_SET_VERSION!r}"
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe. The agent's harness uses the agent's equivalent."""
    return {"status": "ok"}


@app.post("/runs", status_code=202)
async def start_run(
    request: StartEvalRequest, background: BackgroundTasks
) -> StartEvalResponse:
    """Start a golden-set run and return immediately with its id."""
    try:
        version, cases = resolve_golden_set(request.golden_set_version)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Resolve the baseline BEFORE inserting this run, or `get_latest_eval_run`
    # returns the run we are about to start and every metric compares to itself.
    baseline = await _resolve_baseline(request.compare_to)

    run = EvalRun(started_at=datetime.now(timezone.utc))
    await insert_eval_run(run)
    background.add_task(_execute_run, run, version, cases, baseline)

    logger.info(
        "run %s scheduled: %d case(s) from golden set %s, baseline %s",
        run.run_id,
        len(cases),
        version,
        baseline.run_id if baseline else "none",
    )
    return StartEvalResponse(run_id=run.run_id)


@app.get("/runs/latest")
async def read_latest_run() -> EvalRun:
    """The most recent run by ``started_at``, whatever its status.

    Declared before ``/runs/{run_id}`` on purpose: FastAPI matches in
    declaration order and would otherwise try to parse ``latest`` as a UUID and
    answer 422 (INTERFACES §4).
    """
    run = await get_latest_eval_run()
    if run is None:
        raise HTTPException(status_code=404, detail="no eval run has been recorded")
    return run


@app.get("/runs/{run_id}")
async def read_run(run_id: UUID) -> EvalRun:
    """One run, with its findings."""
    run = await get_eval_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
    return run


async def _resolve_baseline(compare_to: UUID | None) -> EvalRun | None:
    """The run this one will be compared against, or ``None``.

    An explicit ``compare_to`` must exist, must carry metrics, and must have
    COMPLETED; asking to compare against a run that has none is a request the
    service cannot honour, and answering it with "no regressions" would be a
    lie. The status check is the same one the default path applies, and it is
    here because a caller who names a FAILED run by id gets a clean verdict
    against a run that violated a zero-tolerance assertion otherwise — which is
    exactly the laundering ``_execute_run`` marks those runs FAILED to prevent.

    With no explicit request, the baseline is the most recent run — used only if
    it completed and carries metrics. :mod:`trail.db` exposes the latest run of
    any status rather than the latest *completed* one, and reaching past that
    interface with ad-hoc SQL is not worth it: the conservative reading is
    better anyway. A failed or compliance-violating run suppresses regression
    detection for the next run instead of silently reaching back two runs, which
    would let a metric drift across a broken run without anyone being told.
    """
    if compare_to is not None:
        baseline = await get_eval_run(compare_to)
        if baseline is None:
            raise HTTPException(
                status_code=404, detail=f"unknown run {compare_to} to compare against"
            )
        if baseline.metrics is None:
            raise HTTPException(
                status_code=422,
                detail=f"run {compare_to} has no metrics to compare against",
            )
        if baseline.status is not EvalRunStatus.COMPLETED:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"run {compare_to} is {baseline.status.value} and cannot be a "
                    "regression baseline. A run that failed — including one failed "
                    "for a compliance violation — is not a system a later run may "
                    "be judged 'no regression' against (BLUEPRINT §5)."
                ),
            )
        return baseline

    latest = await get_latest_eval_run()
    if (
        latest is not None
        and latest.status is EvalRunStatus.COMPLETED
        and latest.metrics
    ):
        return latest
    return None


def _compare(metrics: MetricSet, baseline: EvalRun | None) -> list[str]:
    """Regression statements against ``baseline``, or why there are none.

    Postgres outlives the code. A baseline row written by a build carrying a
    different golden set is not a comparable system — the cases are different,
    so every rate is a different measurement — and comparing anyway would report
    "no regressions" about an arithmetic coincidence. ``resolve_golden_set``
    guards the *current* run's version; this guards the baseline's, which is the
    half that can arrive from six months ago.

    A different prompt version or model is a real comparison and is kept, but it
    is a comparison between two systems rather than two runs of one, so it is
    said out loud rather than left for a reader to notice.
    """
    if baseline is None or baseline.metrics is None:
        return []

    before = baseline.metrics
    if before.golden_set_version != metrics.golden_set_version:
        statement = (
            f"regression detection skipped: baseline run {baseline.run_id} was "
            f"scored against golden set {before.golden_set_version!r} and this run "
            f"against {metrics.golden_set_version!r}. Different cases are a "
            "different measurement, not a movement."
        )
        logger.warning("%s", statement)
        return [statement]

    if (before.prompt_version, before.model) != (metrics.prompt_version, metrics.model):
        logger.warning(
            "comparing across systems: baseline run %s ran prompt %s on %s, this run "
            "ran prompt %s on %s. The comparison is valid; the two are not the same "
            "system",
            baseline.run_id,
            before.prompt_version,
            before.model,
            metrics.prompt_version,
            metrics.model,
        )

    return detect_regression(metrics, before)


async def _execute_run(
    run: EvalRun,
    golden_set_version: str,
    cases: Sequence[SyntheticCase],
    baseline: EvalRun | None,
) -> None:
    """Drive the golden set, score it, persist it, and print the scorecard.

    Never raises: a failure here happens after the ``202`` has been sent, so the
    only way to report it is the run row itself. Anything that goes wrong lands
    as ``status=FAILED`` with no metrics, which the report renders as an
    infrastructure failure rather than a quality result.
    """
    settings = get_settings()
    try:
        with span("eval.run", run_id=run.run_id):
            outcomes = await run_golden_set(cases, run_id=run.run_id)
            metrics, findings = compute_metrics(
                run_id=run.run_id,
                golden_set_version=golden_set_version,
                outcomes=outcomes,
                fallback_prompt_version=settings.prompt_version,
                fallback_model=settings.model,
            )

        regressions = _compare(metrics, baseline)

        # A compliance violation fails the run outright (BLUEPRINT §5). The
        # metrics are still persisted — the evidence matters — but the run is
        # marked FAILED so it cannot become the baseline that a later run is
        # judged "no regression" against.
        status = (
            EvalRunStatus.FAILED
            if metrics.compliance_violations
            else EvalRunStatus.COMPLETED
        )
        finished = run.model_copy(
            update={
                "finished_at": datetime.now(timezone.utc),
                "status": status,
                "metrics": metrics,
                "findings": findings,
                "regression_vs": baseline.run_id if baseline is not None else None,
                "regressions": regressions,
            }
        )
        await update_eval_run(finished)
        logger.info(
            "run %s %s: %.1f%% fully automated of %d scheduled, %d violation(s)",
            finished.run_id,
            status.value,
            metrics.fully_automated_rate * 100,
            metrics.scheduled_accounts,
            metrics.compliance_violations,
        )
        _print_report(finished)

    except Exception:
        logger.exception("run %s failed", run.run_id)
        failed = run.model_copy(
            update={
                "finished_at": datetime.now(timezone.utc),
                "status": EvalRunStatus.FAILED,
            }
        )
        try:
            await update_eval_run(failed)
        except Exception:
            logger.exception(
                "run %s could not be marked FAILED; it will stay RUNNING in the "
                "database and GET /runs/latest will show it as such",
                run.run_id,
            )


def _print_report(run: EvalRun) -> None:
    """Render the scorecard to the container log, guarded.

    Deliberately outside the run's own error handling and unable to fail it: the
    run is already persisted by the time this is called, and a formatting bug
    must not roll a completed run back to FAILED and discard its metrics.
    Printing is a convenience; the record is the result.
    """
    try:
        render_report(run, Console())
    except Exception:
        logger.exception(
            "run %s was scored and persisted, but its report could not be rendered; "
            "read it with GET /runs/%s",
            run.run_id,
            run.run_id,
        )
