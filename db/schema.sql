-- TRAIL — database bootstrap.
--
-- Applied once by the Postgres image from `docker-entrypoint-initdb.d`, on an
-- empty volume only. There is no migration tool by design: the schema is small,
-- the stack is disposable, and `make clean` is the migration.
--
-- The table list is deliberately empty, and that is the interesting part.
--
-- Conversation state — threads, checkpoints, cross-thread memory — is owned by
-- LangGraph's Postgres checkpointer and store, which create and migrate their
-- own tables from `setup()` at startup (see `trail.runtime.checkpointers`).
-- Declaring them here would mean maintaining a second, hand-written copy of a
-- schema the library already versions, and being wrong about it on the first
-- upgrade.
--
-- Per-call tokens, cost and latency live in Langfuse, arriving as typed
-- generations off the OTel span the trace middleware stamps. A local table
-- duplicating them would be a second source of truth for the same numbers, and
-- the one this repository can least afford to have disagree with itself.
--
-- What does live here is the eval harness. Run records and findings are
-- TRAIL's own data with no upstream owner, which is the test for whether a
-- table belongs in this file at all.
--
-- Every statement below is idempotent, because this file is applied twice: by
-- the Postgres image on an empty volume, and by `trail.evals.store` on the
-- first run against a volume that predates the harness. One definition, two
-- callers — the alternative is a second copy of the DDL in Python, which is
-- the duplication the paragraphs above refuse for LangGraph's tables.

-- Fail loudly rather than silently if the database is not what we think it is.
DO $$
BEGIN
    IF current_database() IS NULL THEN
        RAISE EXCEPTION 'no database';
    END IF;
END
$$;


-- ---------------------------------------------------------------------------
-- The eval harness
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS eval_runs (
    id                 BIGSERIAL PRIMARY KEY,
    started_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at        TIMESTAMPTZ,

    -- Pins the measurement. A baseline scored against a different set of cases
    -- is not a baseline, and the harness refuses that comparison rather than
    -- printing a delta between two different experiments.
    golden_set_version TEXT        NOT NULL,

    -- What produced the numbers. Without these a run is a score with no
    -- subject: the same golden set over two models is two findings, not one
    -- trend, and `guardrails` is the dial most likely to explain a jump.
    agent              TEXT        NOT NULL,
    model              TEXT        NOT NULL,
    guardrails         TEXT        NOT NULL,

    -- Empty means the agent graded itself. Recorded rather than inferred,
    -- because a later reader comparing two runs needs to know which of them
    -- had an independent grader.
    judge_model        TEXT        NOT NULL DEFAULT '',

    -- FAILED is the zero-tolerance verdict: a fabricated fact, or a gate that
    -- refused a legitimate question. The baseline lookup filters on it, which
    -- is the boundary rule made mechanical — a failed run cannot become the
    -- thing a later run is judged "no regression" against.
    status             TEXT        NOT NULL
                       CHECK (status IN ('COMPLETED', 'FAILED')),

    -- The whole scorecard, each metric with its value, denominator and the bar
    -- it was measured against. JSONB and not a column per metric: the metric
    -- list belongs to the example's golden set, so a new example must not need
    -- a migration to be measurable.
    metrics            JSONB       NOT NULL DEFAULT '{}'::jsonb,

    baseline_id        BIGINT      REFERENCES eval_runs (id) ON DELETE SET NULL
);

-- The baseline lookup, which is the only query with a hot path: most recent
-- COMPLETED run of this golden set.
CREATE INDEX IF NOT EXISTS eval_runs_baseline_idx
    ON eval_runs (golden_set_version, status, started_at DESC);

-- Findings are normalised out of the run rather than nested in its JSON so the
-- failure taxonomy is queryable: `select kind, count(*) ... group by kind` is
-- the question this table exists to answer, and it is not a question you ask
-- of a JSON blob.
CREATE TABLE IF NOT EXISTS eval_findings (
    id         BIGSERIAL PRIMARY KEY,
    run_id     BIGINT NOT NULL REFERENCES eval_runs (id) ON DELETE CASCADE,
    case_id    TEXT   NOT NULL,
    turn       INT    NOT NULL DEFAULT 0,

    -- OMISSION | FABRICATION | WRONG_PATH | ERROR. Unconstrained on purpose:
    -- the taxonomy is the harness's vocabulary and a new kind should not need
    -- a schema change to be recorded, only to be reported.
    kind       TEXT   NOT NULL,

    -- `check_name`, not `check`: CHECK is a reserved word, and a column that
    -- needs quoting in every query is a column that will eventually be typed
    -- without them.
    check_name TEXT   NOT NULL,

    -- 'check' or 'judge'. A substring test and a model's opinion are not the
    -- same evidence, and a reader must be able to weigh them differently
    -- without going back to read the case.
    source     TEXT   NOT NULL DEFAULT 'check',

    expected   TEXT   NOT NULL DEFAULT '',
    actual     TEXT   NOT NULL DEFAULT '',
    detail     TEXT   NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS eval_findings_run_idx  ON eval_findings (run_id);
CREATE INDEX IF NOT EXISTS eval_findings_kind_idx ON eval_findings (kind);
