"""Fixtures for the tier that needs the compose stack and a real API key.

Everything here skips rather than fails when the infrastructure is absent. An
integration test that goes red because Docker is not running teaches a reviewer
to ignore red, which is how a real failure gets ignored too — so the absence of
a stack produces a skip with an actionable reason, and only a stack that is
present and wrong produces a failure.

Run these with::

    make up && make test-integration

``make test-integration`` supplies the host-side addresses; the compose defaults
name other containers and do not resolve from a laptop.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from psycopg_pool import AsyncConnectionPool

from trail.config import get_settings
from trail.db import close_pool, get_pool, init_pool

HEALTH_TIMEOUT_SECONDS = 3.0

TURN_TIMEOUT_SECONDS = 180.0
"""One turn is one ``gpt-5.6-luna`` extraction with reasoning off."""

DEFAULT_EVAL_TIMEOUT_SECONDS = 900.0
"""How long a golden-set run may take before the test gives up.

Fifteen cases at up to eight model calls each, bounded to four concurrent
calls. Generous, and overridable through ``TRAIL_TEST_EVAL_TIMEOUT_SECONDS``,
because a slow run is a latency observation and not a test failure.
"""


def _health_reason(name: str, base_url: str) -> str | None:
    """Return why ``name`` cannot be tested, or ``None`` if it is up."""
    try:
        response = httpx.get(f"{base_url}/healthz", timeout=HEALTH_TIMEOUT_SECONDS)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return (
            f"{name} is not reachable at {base_url} ({type(exc).__name__}). "
            "Run `make up`, then `make test-integration`."
        )
    return None


@pytest.fixture(scope="session")
def agent_base_url() -> str:
    return get_settings().agent_base_url.rstrip("/")


@pytest.fixture(scope="session")
def evals_base_url() -> str:
    """The evals service has no ``Settings`` field, matching the CLI (INTERFACES §7)."""
    return os.environ.get("TRAIL_EVALS_BASE_URL", "http://localhost:8001").rstrip("/")


@pytest.fixture(scope="session")
def live_agent(agent_base_url: str) -> str:
    reason = _health_reason("agent", agent_base_url)
    if reason:
        pytest.skip(reason)
    return agent_base_url


@pytest.fixture(scope="session")
def live_evals(evals_base_url: str) -> str:
    reason = _health_reason("evals", evals_base_url)
    if reason:
        pytest.skip(reason)
    return evals_base_url


@pytest.fixture
def agent_client(live_agent: str) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=live_agent, timeout=TURN_TIMEOUT_SECONDS) as client:
        yield client


@pytest.fixture
def evals_client(live_evals: str) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=live_evals, timeout=30.0) as client:
        yield client


@pytest.fixture(scope="session")
def eval_poll_timeout() -> float:
    """Seconds to wait for a golden-set run. See :data:`DEFAULT_EVAL_TIMEOUT_SECONDS`."""
    return float(
        os.environ.get(
            "TRAIL_TEST_EVAL_TIMEOUT_SECONDS", str(DEFAULT_EVAL_TIMEOUT_SECONDS)
        )
    )


@pytest.fixture
async def db_pool() -> AsyncIterator[AsyncConnectionPool]:
    """The process-wide pool from :mod:`trail.db`, opened against the live database.

    Uses the package's own pool rather than a private connection, so a test that
    reads a record exercises the code path the agent uses to write one.
    """
    dsn = get_settings().database_url
    try:
        await init_pool(dsn)
    except Exception as exc:  # pragma: no cover - depends on the local stack
        await close_pool()
        pytest.skip(f"postgres is not reachable at {dsn} ({type(exc).__name__}).")
    try:
        yield get_pool()
    finally:
        await close_pool()
