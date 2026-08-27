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

from collections.abc import Iterator

import httpx
import pytest

from trail.config import get_settings

HEALTH_TIMEOUT_SECONDS = 3.0

TURN_TIMEOUT_SECONDS = 180.0
"""One turn is one or more real model calls, possibly with tool rounds."""


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
def live_agent(agent_base_url: str) -> str:
    reason = _health_reason("agent", agent_base_url)
    if reason:
        pytest.skip(reason)
    return agent_base_url


@pytest.fixture
def agent_client(live_agent: str) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=live_agent, timeout=TURN_TIMEOUT_SECONDS) as client:
        yield client


@pytest.fixture
def real_credentials(monkeypatch: pytest.MonkeyPatch) -> str:
    """Undo the unit tier's pinned key, so ``.env``'s real one is read.

    ``tests/conftest.py`` sets a deliberately invalid key on every test, which
    is what keeps the unit tier from spending money by accident. Almost every
    integration test is unaffected — they drive the agent over HTTP and never
    build a model in-process. The judge does, and it is the one thing in this
    repository that calls a model from the test process itself.

    Skips rather than fails when there is no real key: the same rule as a
    missing stack.
    """
    monkeypatch.delenv("TRAIL_LLM_API_KEY", raising=False)
    get_settings.cache_clear()
    key = get_settings().llm_api_key.get_secret_value()
    if not key or key.startswith("unit-tests"):
        pytest.skip("no real TRAIL_LLM_API_KEY in .env; the judge cannot run")
    return key
