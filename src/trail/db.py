"""Postgres persistence: plain parameterised SQL over psycopg3.

No ORM and no migration tool, on purpose (BLUEPRINT §8). Five tables do not
need a mapper, and readable SQL is a feature in a repo people are going to read:
every statement below can be pasted into ``psql`` unchanged. Revisit past about
eight tables.

Conventions this module holds to:

* One process-wide :class:`~psycopg_pool.AsyncConnectionPool`, opened by
  :func:`init_pool` from the FastAPI lifespan startup hook and closed by
  :func:`close_pool` on shutdown.
* Reads open a cursor with ``row_factory=dict_row`` and hand the mapping
  straight to ``model_validate``; the column list in the ``SELECT`` is therefore
  the mapping to the model, and a column the model does not declare fails loudly
  on ``extra="forbid"`` instead of being silently dropped.
* ``jsonb`` columns are written with
  ``Jsonb(model.model_dump(mode="json"))`` — ``mode="json"`` is what renders
  enums as their string values, ``datetime`` as ISO-8601 and ``UUID`` as a
  string, which is what the schema's ``CHECK`` constraints and the eval report
  both expect. Reading back, psycopg returns parsed JSON and Pydantic validates
  it into the same model.
* Enum-valued columns are written as ``.value`` so what reaches the ``CHECK``
  constraint is unambiguously a plain string.

The trace tables carry no foreign key to ``call_records`` by design: turns and
LLM calls are written while the call is still running, and the record only lands
when it ends. A foreign key there would discard the traces of any call that
died mid-flight — exactly the calls worth inspecting.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from trail.config import get_settings
from trail.models import (
    CallRecord,
    EvalRun,
    Finding,
    LLMCallTrace,
    StrictModel,
    TurnTrace,
)

__all__ = [
    "close_pool",
    "get_call_record",
    "get_eval_run",
    "get_latest_eval_run",
    "get_pool",
    "init_pool",
    "insert_call_record",
    "insert_eval_run",
    "insert_llm_call_trace",
    "insert_turn_trace",
    "update_eval_run",
]

# Small on purpose: the agent is one uvicorn process serving a handful of
# concurrent calls, and every request holds a connection for microseconds
# between multi-second LLM calls.
_POOL_MIN_SIZE = 1
_POOL_MAX_SIZE = 10

# Compose starts postgres alongside the agent, so the first connection can lose
# the race. Wait rather than crash-loop, but bound the wait so a genuinely
# unreachable database fails the boot loudly instead of hanging forever.
_POOL_OPEN_TIMEOUT_SECONDS = 30.0

_pool: AsyncConnectionPool | None = None


# --------------------------------------------------------------------------
# Pool lifecycle
# --------------------------------------------------------------------------


async def init_pool(dsn: str | None = None) -> AsyncConnectionPool:
    """Open the process-wide connection pool and return it.

    Idempotent: a second call returns the pool already open and ignores ``dsn``.

    Args:
        dsn: connection string. Defaults to ``settings.database_url``.

    Raises:
        psycopg_pool.PoolTimeout: if the database is not reachable within
            ``_POOL_OPEN_TIMEOUT_SECONDS``.
    """
    global _pool
    if _pool is not None:
        return _pool

    pool = AsyncConnectionPool(
        conninfo=dsn or get_settings().database_url,
        min_size=_POOL_MIN_SIZE,
        max_size=_POOL_MAX_SIZE,
        open=False,
    )
    try:
        await pool.open(wait=True, timeout=_POOL_OPEN_TIMEOUT_SECONDS)
    except Exception:
        # Close the half-built pool so its background worker does not outlive
        # the failed startup and keep reconnecting to nothing.
        await pool.close()
        raise

    _pool = pool
    return pool


def get_pool() -> AsyncConnectionPool:
    """Return the pool opened by :func:`init_pool`.

    Raises:
        RuntimeError: if the pool has not been initialised.
    """
    if _pool is None:
        raise RuntimeError(
            "database pool is not initialised; call `await init_pool()` from the "
            "FastAPI lifespan startup hook before serving requests"
        )
    return _pool


async def close_pool() -> None:
    """Close the pool. Idempotent; safe on a process that never opened one."""
    global _pool
    if _pool is None:
        return
    await _pool.close()
    _pool = None


# --------------------------------------------------------------------------
# call_records
# --------------------------------------------------------------------------


async def insert_call_record(rec: CallRecord) -> None:
    """Persist one completed call.

    ``needs_specialist_review`` is written as the model carries it — pinned
    ``True`` by ``Literal[True]`` in Pydantic and by a ``CHECK`` in the schema,
    so neither this function nor a manual ``UPDATE`` can mark AI output final
    (BLUEPRINT §6).

    ``selected_path`` is written as ``.value`` like every other enum column, so
    what reaches ``call_records_selected_path_check`` is a plain string from the
    closed :class:`~trail.models.PaymentPath` set. A settlement or bespoke
    plan the agent had no authority to grant has no member to be written as.
    """
    async with get_pool().connection() as conn:
        await conn.execute(
            """
            INSERT INTO call_records (
                call_id, account_id, started_at, ended_at, terminal_state,
                commitments, disputes,
                selected_path, contact_channel_confirmed, consent_given,
                terms_confirmed,
                protocol_version, prompt_version, model,
                needs_specialist_review, reviewed_by, reviewed_at,
                total_input_tokens, total_output_tokens, cost_usd, wall_seconds
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s,
                %s, %s, %s,
                %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s
            )
            """,
            (
                rec.call_id,
                rec.account_id,
                rec.started_at,
                rec.ended_at,
                rec.terminal_state.value,
                _json_list(rec.commitments),
                _json_list(rec.disputes),
                rec.selected_path.value if rec.selected_path is not None else None,
                rec.contact_channel_confirmed,
                rec.consent_given,
                rec.terms_confirmed,
                rec.protocol_version,
                rec.prompt_version,
                rec.model,
                rec.needs_specialist_review,
                rec.reviewed_by,
                rec.reviewed_at,
                rec.total_input_tokens,
                rec.total_output_tokens,
                rec.cost_usd,
                rec.wall_seconds,
            ),
        )


async def get_call_record(call_id: UUID) -> CallRecord | None:
    """Read one completed call record, or ``None`` if there is no such row."""
    async with (
        get_pool().connection() as conn,
        conn.cursor(row_factory=dict_row) as cursor,
    ):
        await cursor.execute(
            """
            SELECT call_id, account_id, started_at, ended_at, terminal_state,
                   commitments, disputes,
                   selected_path, contact_channel_confirmed, consent_given,
                   terms_confirmed,
                   protocol_version, prompt_version, model,
                   needs_specialist_review, reviewed_by, reviewed_at,
                   total_input_tokens, total_output_tokens, cost_usd, wall_seconds
              FROM call_records
             WHERE call_id = %s
            """,
            (call_id,),
        )
        row = await cursor.fetchone()

    return CallRecord.model_validate(row) if row is not None else None


# --------------------------------------------------------------------------
# Traces — semantic records, distinct from OTel spans (BLUEPRINT §6)
# --------------------------------------------------------------------------


async def insert_turn_trace(t: TurnTrace) -> None:
    """Persist one conversational turn, mid-call."""
    async with get_pool().connection() as conn:
        await conn.execute(
            """
            INSERT INTO turn_traces (
                turn_id, call_id, step, agent_utterance, customer_utterance,
                extraction, latency_ms, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                t.turn_id,
                t.call_id,
                t.step.value,
                t.agent_utterance,
                t.customer_utterance,
                Jsonb(t.extraction.model_dump(mode="json")) if t.extraction else None,
                t.latency_ms,
                t.created_at,
            ),
        )


async def insert_llm_call_trace(t: LLMCallTrace) -> None:
    """Persist one model API call, successful or failed.

    Failed calls are recorded too, with the error payload in ``response_json``:
    a model call that cost tokens and produced nothing is exactly what the
    economics and reliability posts need to see.
    """
    async with get_pool().connection() as conn:
        await conn.execute(
            """
            INSERT INTO llm_call_traces (
                trace_id, call_id, step, prompt_version, model,
                request_json, response_json,
                input_tokens, output_tokens,
                cache_read_input_tokens, cache_creation_input_tokens,
                cost_usd, latency_ms, created_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s, %s
            )
            """,
            (
                t.trace_id,
                t.call_id,
                t.step.value,
                t.prompt_version,
                t.model,
                Jsonb(t.request_json),
                Jsonb(t.response_json),
                t.input_tokens,
                t.output_tokens,
                t.cache_read_input_tokens,
                t.cache_creation_input_tokens,
                t.cost_usd,
                t.latency_ms,
                t.created_at,
            ),
        )


# --------------------------------------------------------------------------
# Eval runs and findings
# --------------------------------------------------------------------------


async def insert_eval_run(run: EvalRun) -> None:
    """Insert the run row.

    ``run.findings`` is ignored: the row is written before the run starts, so
    there are none yet. :func:`update_eval_run` writes them at the end.
    """
    async with get_pool().connection() as conn:
        await conn.execute(
            """
            INSERT INTO eval_runs (
                run_id, started_at, finished_at, status,
                metrics, regression_vs, regressions
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run.run_id,
                run.started_at,
                run.finished_at,
                run.status.value,
                Jsonb(run.metrics.model_dump(mode="json")) if run.metrics else None,
                run.regression_vs,
                Jsonb(run.regressions),
            ),
        )


async def update_eval_run(run: EvalRun) -> None:
    """Write the finished run: status, metrics, regressions, and findings.

    ``started_at`` is deliberately not updated. It was set when the row was
    inserted, it is what ``GET /runs/latest`` orders by, and rewriting it from a
    reconstructed model would silently reorder the run history.

    The findings are replaced wholesale rather than appended, so re-running the
    scorer over the same run leaves one set of findings and not two. The delete
    and the re-insert share the connection's transaction — ``pool.connection()``
    opens one and commits it on clean exit — so a failure mid-way rolls back to
    the previous findings instead of leaving none.

    Raises:
        LookupError: if no such run exists. A silent no-op here would lose a
            completed run's metrics and leave the API reporting it as still
            running.
    """
    async with get_pool().connection() as conn:
        cursor = await conn.execute(
            """
            UPDATE eval_runs
               SET finished_at   = %s,
                   status        = %s,
                   metrics       = %s,
                   regression_vs = %s,
                   regressions   = %s
             WHERE run_id = %s
            """,
            (
                run.finished_at,
                run.status.value,
                Jsonb(run.metrics.model_dump(mode="json")) if run.metrics else None,
                run.regression_vs,
                Jsonb(run.regressions),
                run.run_id,
            ),
        )
        if cursor.rowcount == 0:
            raise LookupError(f"eval run {run.run_id} does not exist")

        await conn.execute(
            "DELETE FROM eval_findings WHERE run_id = %s",
            (run.run_id,),
        )
        if run.findings:
            async with conn.cursor() as findings_cursor:
                await findings_cursor.executemany(
                    """
                    INSERT INTO eval_findings (
                        run_id, case_id, field, kind, expected, actual, detail
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            run.run_id,
                            f.case_id,
                            f.field,
                            f.kind.value,
                            f.expected,
                            f.actual,
                            f.detail,
                        )
                        for f in run.findings
                    ],
                )


async def get_eval_run(run_id: UUID) -> EvalRun | None:
    """Read one run, reassembled from ``eval_runs`` plus its findings."""
    async with (
        get_pool().connection() as conn,
        conn.cursor(row_factory=dict_row) as cursor,
    ):
        await cursor.execute(
            """
            SELECT run_id, started_at, finished_at, status,
                   metrics, regression_vs, regressions
              FROM eval_runs
             WHERE run_id = %s
            """,
            (run_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        findings = await _findings_for(conn, run_id)

    return EvalRun.model_validate({**row, "findings": findings})


async def get_latest_eval_run() -> EvalRun | None:
    """Read the most recent run by ``started_at``, whatever its status."""
    async with (
        get_pool().connection() as conn,
        conn.cursor(row_factory=dict_row) as cursor,
    ):
        await cursor.execute(
            """
            SELECT run_id, started_at, finished_at, status,
                   metrics, regression_vs, regressions
              FROM eval_runs
             ORDER BY started_at DESC
             LIMIT 1
            """
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        findings = await _findings_for(conn, row["run_id"])

    return EvalRun.model_validate({**row, "findings": findings})


async def _findings_for(conn: AsyncConnection[Any], run_id: UUID) -> list[Finding]:
    """Read a run's findings in insertion order.

    ``eval_findings.id`` is a surrogate with no counterpart on
    :class:`~trail.models.Finding` — the same case can legitimately produce
    several identical-looking findings — so it orders the read and is never
    round-tripped into the model.
    """
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            """
            SELECT case_id, field, kind, expected, actual, detail
              FROM eval_findings
             WHERE run_id = %s
             ORDER BY id
            """,
            (run_id,),
        )
        return [Finding.model_validate(row) for row in await cursor.fetchall()]


def _json_list(models: Sequence[StrictModel]) -> Jsonb:
    """Serialise a list of models for a ``jsonb`` column."""
    return Jsonb([m.model_dump(mode="json") for m in models])
