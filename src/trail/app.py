"""The conversation service: three endpoints over one agent.

``POST /threads`` opens a thread and returns its greeting.
``POST /threads/{id}/turns/stream`` runs one turn and streams its frames.
``GET  /healthz`` says the process is up.

The streaming endpoint is the only interesting one, and what makes it work is
what it does *not* do: it holds no per-turn state, owns no vocabulary, and
renders whatever :func:`~trail.runtime.turns.run_turn` yields. Adding a stage,
a guardrail or a whole new example agent changes nothing in this file.

Two lifetimes are managed here and getting either wrong is subtle:

* **Persistence** is opened in the lifespan and held for the process.
  ``from_conn_string`` is an async context manager; entering it per request
  closes the pool when the request ends, which presents as an exhausted-pool
  error under load and as nothing at all in a smoke test.
* **Telemetry** is set up at *module scope*, below the ``app`` definition, not
  in the lifespan. ``FastAPIInstrumentor`` patches ``build_middleware_stack``,
  which Starlette calls on its way *into* the lifespan scope — a call from
  inside is already too late and yields no HTTP server spans at all.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from trail import costs
from trail.config import Settings, get_settings
from trail.runtime.agent import build_agent
from trail.runtime.checkpointers import open_persistence
from trail.runtime.events import SSE_HEADERS, error_json, sse
from trail.runtime.registry import load_spec
from trail.runtime.turns import ERROR, TURN, run_turn
from trail.telemetry import configure_logging, setup_telemetry

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Wire models
# --------------------------------------------------------------------------


class StartThreadResponse(BaseModel):
    """A new thread, and the opening line the client should show."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    agent: str
    greeting: str
    #: Echoed so a client can render the dial without a second request, and so
    #: a screenshot of a demo carries the setting that produced it.
    guardrails: str


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1)


# --------------------------------------------------------------------------
# Lifespan
# --------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Resolve the example, open persistence, and compile the agent once.

    Compiling once is deliberate. ``create_agent`` builds a graph, and building
    one per request would pay that cost on every turn to produce an identical
    object — while also making a thread's continuity depend on a graph that no
    longer exists by the time the next turn arrives.
    """
    settings = get_settings()
    spec = load_spec(settings.agent)
    prices = costs.load_prices(settings.model_prices)

    async with open_persistence(settings.checkpointer, settings.database_url) as store:
        app.state.settings = settings
        app.state.spec = spec
        app.state.prices = prices
        app.state.persistence = store
        app.state.agent = build_agent(spec, settings, persistence=store, prices=prices)
        logger.info(
            "agent ready: name=%s model=%s guardrails=%s checkpointer=%s durable=%s",
            spec.name,
            settings.model,
            settings.guardrails,
            store.kind,
            store.durable,
        )
        yield


app = FastAPI(
    title="TRAIL — traced agent runtime",
    version="1.0.0",
    summary="An LLM+tools agent with switchable guardrails and an observable pipeline.",
    description=(
        "One turn is a sequence of frames: a stage per guardrail, model call "
        "and tool call, each with its own latency, then the answer and a trace "
        "link. A guardrail that is switched off still reports itself, marked "
        "skipped, because an absence that renders as nothing is "
        "indistinguishable from a success."
    ),
    lifespan=lifespan,
)

# Module scope, with the app, and both halves matter — see this module's
# docstring. Get it wrong and the service silently emits no HTTP server spans,
# which makes a cross-service trace unreadable in exactly the service the
# observability argument is about.
configure_logging()
setup_telemetry(get_settings().service_name, app)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _settings(request: Request) -> Settings:
    return request.app.state.settings


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@app.post(
    "/threads",
    response_model=StartThreadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def start_thread(request: Request) -> StartThreadResponse:
    """Open a thread.

    No model call happens here. The greeting is the example's own string, and
    spending a turn's tokens to produce a hello would put a cost on the
    scoreboard that bought nothing.
    """
    settings = _settings(request)
    spec = request.app.state.spec
    return StartThreadResponse(
        thread_id=str(uuid4()),
        agent=spec.name,
        greeting=spec.greeting,
        guardrails=settings.guardrails,
    )


@app.post("/threads/{thread_id}/turns/stream", response_class=StreamingResponse)
async def stream_turn(thread_id: str, body: TurnRequest, request: Request):
    """Run one turn, streaming its frames as Server-Sent Events.

    This always answers 200, even when the turn fails. By the time anything can
    go wrong the response body has begun, and a body that has begun cannot
    become a 500 — so failures arrive as an ``error`` frame carrying the status
    the buffered endpoint would have returned.
    """
    settings = _settings(request)
    agent = request.app.state.agent

    async def frames() -> AsyncIterator[str]:
        async for name, payload in run_turn(
            agent, thread_id=thread_id, message=body.message, settings=settings
        ):
            yield sse(name, error_json(payload) if name == ERROR else payload)

    return StreamingResponse(
        frames(), media_type="text/event-stream", headers=SSE_HEADERS
    )


@app.post("/threads/{thread_id}/turns")
async def submit_turn(thread_id: str, body: TurnRequest, request: Request) -> dict:
    """Run one turn and return only the answer.

    Drains the same generator the streaming endpoint renders. One
    implementation behind two endpoints is what stops the streamed answer and
    the buffered answer from being able to disagree.
    """
    settings = _settings(request)
    agent = request.app.state.agent
    answer: dict | None = None

    async for name, payload in run_turn(
        agent, thread_id=thread_id, message=body.message, settings=settings
    ):
        if name == TURN:
            answer = payload
        elif name == ERROR:
            # Re-raised rather than rendered: this endpoint's body has not
            # begun, so it can still answer with a real status code.
            raise (
                payload
                if isinstance(payload, HTTPException)
                else HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY, detail=str(payload)
                )
            )

    if answer is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="the turn produced no answer",
        )
    return answer


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
