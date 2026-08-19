"""The eval service against the running stack: one real golden-set run.

The harness drives the agent over HTTP from its own container, so this is the
only test that exercises the service boundary the repository exists to make
visible — evals talking to agent, agent talking to Postgres and the model provider, and
two OTel service names in one trace.

The expensive test asserts the honest-denominator property on live data:
``fully_automated_rate`` must equal the count of clean completions over
*fifteen scheduled accounts*, whatever the agent managed on the day.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from trail.cases import GOLDEN_SET, GOLDEN_SET_VERSION
from trail.models import (
    EvalRun,
    EvalRunStatus,
    StartEvalRequest,
    StartEvalResponse,
    TerminalState,
)

pytestmark = pytest.mark.integration

POLL_INTERVAL_SECONDS = 5.0


def test_the_evals_service_reports_itself_healthy(evals_client: httpx.Client) -> None:
    response = evals_client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_an_unknown_golden_set_version_is_refused_rather_than_substituted(
    evals_client: httpx.Client,
) -> None:
    """A run stamped with a version it did not execute is worse than no run.

    It makes two incomparable scorecards look comparable, which is the failure
    the version stamp exists to prevent.
    """
    response = evals_client.post(
        "/runs",
        json=StartEvalRequest(golden_set_version="golden_v0").model_dump(mode="json"),
    )

    assert response.status_code == 422
    assert GOLDEN_SET_VERSION in response.json()["detail"]


def test_reading_a_run_that_was_never_started_is_a_404(
    evals_client: httpx.Client,
) -> None:
    response = evals_client.get(f"/runs/{uuid4()}")

    assert response.status_code == 404


def test_the_latest_run_endpoint_is_not_parsed_as_a_run_id(
    evals_client: httpx.Client,
) -> None:
    """Route ordering: ``/runs/latest`` is declared before ``/runs/{run_id}``.

    Declared the other way round, FastAPI tries to parse "latest" as a UUID and
    answers 422 (INTERFACES §4).
    """
    response = evals_client.get("/runs/latest")

    assert response.status_code in {200, 404}


def test_a_golden_set_run_completes_and_returns_a_metric_set(
    evals_client: httpx.Client, eval_poll_timeout: float
) -> None:
    """The whole harness, against the live agent. This one costs real tokens.

    ``POST /runs`` returns 202 immediately and the run proceeds on a background
    task, so this polls. What it asserts afterwards is the arithmetic, not the
    score: the denominator is the fifteen scheduled accounts of ``golden_v1``,
    and the primary metric is the count of clean completions over that number
    and nothing else.

    ``compliance_violations`` is asserted at zero because it is a gate rather
    than a metric. If the agent improvised a sentence outside the approved
    collections script, this test is
    supposed to fail (BLUEPRINT §5) — and the service marks such a run FAILED
    so it can never become the baseline a later run is judged against.
    """
    started = StartEvalResponse.model_validate(
        _post(evals_client, "/runs", StartEvalRequest(), expected=202)
    )

    run = _poll(evals_client, started.run_id, eval_poll_timeout)

    assert run.metrics is not None, (
        f"run finished as {run.status.value} with no metrics"
    )
    metrics = run.metrics

    assert run.status is EvalRunStatus.COMPLETED
    assert metrics.golden_set_version == GOLDEN_SET_VERSION
    assert metrics.scheduled_accounts == len(GOLDEN_SET)
    assert metrics.reached <= metrics.scheduled_accounts

    automated = metrics.terminal_state_counts.get(
        TerminalState.COMPLETED_NO_CALLBACK, 0
    )
    assert metrics.fully_automated_rate == pytest.approx(
        automated / metrics.scheduled_accounts
    )
    assert metrics.compliance_violations == 0
    assert metrics.prompt_version and metrics.model


def test_the_latest_run_is_the_one_that_just_finished(
    evals_client: httpx.Client,
) -> None:
    """Ordered by ``started_at``, whatever its status.

    Depends on some run having happened; skips rather than fails on a database
    that has never seen one.
    """
    response = evals_client.get("/runs/latest")
    if response.status_code == 404:
        pytest.skip("no eval run has been recorded in this database yet")

    latest = EvalRun.model_validate(response.json())
    by_id = EvalRun.model_validate(evals_client.get(f"/runs/{latest.run_id}").json())

    assert by_id.run_id == latest.run_id
    assert by_id.findings == latest.findings


def _post(
    client: httpx.Client, path: str, body: StartEvalRequest, *, expected: int
) -> Any:
    response = client.post(path, json=body.model_dump(mode="json"))
    assert response.status_code == expected, response.text
    return response.json()


def _poll(client: httpx.Client, run_id: UUID, timeout: float) -> EvalRun:
    """Wait for a run to leave RUNNING, or fail with how long it waited."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/runs/{run_id}")
        assert response.status_code == 200, response.text
        run = EvalRun.model_validate(response.json())
        if run.status is not EvalRunStatus.RUNNING:
            return run
        time.sleep(POLL_INTERVAL_SECONDS)

    pytest.fail(
        f"run {run_id} was still RUNNING after {timeout:.0f}s; raise "
        "TRAIL_TEST_EVAL_TIMEOUT_SECONDS or check `docker compose logs -f evals`"
    )
