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
-- What is expected to land here in a later round is the eval harness: run
-- records, findings, and pre-registered thresholds. Those are TRAIL's own data
-- with no upstream owner, which is the test for whether a table belongs in this
-- file at all.

-- Fail loudly rather than silently if the database is not what we think it is.
DO $$
BEGIN
    IF current_database() IS NULL THEN
        RAISE EXCEPTION 'no database';
    END IF;
END
$$;
