-- =====================================================================
-- Banco Aurora — early-stage collections agent (1–30 days past due)
-- Postgres 16 schema. Applied once by the postgres image via
-- docker-entrypoint-initdb.d, and safe to re-run (CREATE ... IF NOT EXISTS).
--
-- There is no migration tool here, on purpose. Migrations are a production
-- concern: five tables and a local compose stack do not need Alembic, and
-- pretending otherwise adds a moving part the repo cannot exercise. The
-- deferral sits alongside audio, telephony, outbound consent management,
-- LLM-generated customers, auth and the incremental-recovery outcomes layer
-- in the list of things this build deliberately does not contain.
--
-- Conventions:
--   * Machine-generated identifiers are `uuid`; identifiers that come from
--     outside the system (account_id, case_id, reviewed_by) are `text`.
--   * All timestamps are `timestamptz`; the application writes UTC.
--   * Anything modelled as a Pydantic list or nested model is `jsonb`,
--     written with `model_dump(mode="json")` so enums land as their string
--     values and datetimes as ISO-8601.
--   * Enum-valued columns are `text` with a CHECK constraint rather than a
--     Postgres ENUM type: the values live in trail.models, and a CHECK is
--     readable in a repo people will actually read, without needing an
--     ALTER TYPE dance every time the taxonomy changes.
--   * Money has two types here, and they are not interchangeable.
--     `cost_usd` is `double precision`, matching the float in the models:
--     model spend in fractions of a cent, an analytics figure and not a
--     ledger. Any column holding money the bank and the customer transact in
--     — a balance, an instalment, a payment — is `numeric(12,2)`. In a
--     generic system that rule is a caveat about hypothetical real money; in
--     this one the money is real. `AccountProfile.balance_brl` is a `Decimal`
--     because it is rendered into approved text and spoken aloud, and a wrong
--     balance spoken aloud is a zero-tolerance failure (BLUEPRINT §5). A
--     balance that round-tripped through binary floating point is a wrong
--     balance waiting for the right cents.
--
--     No table below carries one yet, and that is a statement about what this
--     service owns rather than an oversight: the balance belongs to the core
--     banking system this repo mocks, `call_records` records what was said and
--     agreed rather than what is owed, and the 30-day outcomes layer that will
--     hold cash received is explicitly out of scope for this build. The rule
--     is written down here so that the first column to hold money is
--     `numeric(12,2)` and not `double precision`.
-- =====================================================================


-- ---------------------------------------------------------------------
-- call_records — the mocked system of record, one row per completed call.
--
-- Mirrors trail.models.CallRecord. Note what is NOT here: there is no
-- priority, urgency, severity, risk_score, triage, hardship, vulnerability,
-- propensity, segment or score column, and there must never be one. Sorting
-- the specialist queue by how likely a customer looks to pay is an inferred,
-- customer-specific classification made by a language model from one phone
-- transcript, deciding who gets human attention and in what order — the
-- disparate treatment is the FDCPA/UDAAP exposure, and the customers it would
-- sort to the bottom are the ones FCA Consumer Duty and CONC exist to protect.
-- Every record goes to the same specialist queue. Order the queue by
-- started_at.
--
-- `reviewed_by` / `reviewed_at` start NULL so nothing finalises itself, and
-- `needs_specialist_review` is pinned true by a CHECK constraint so no future
-- code path — or manual UPDATE, which never passes through the Pydantic model
-- that pins it a second time — can mark AI output as final.
--
-- `selected_path` gets a CHECK of its own for the same reason PaymentPath is a
-- closed enum in the models: a settlement, waiver or bespoke plan the agent
-- had no authority to grant has no representation here, so it cannot be
-- written down even by hand.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS call_records (
    call_id                     uuid         PRIMARY KEY,
    account_id                  text         NOT NULL,
    started_at                  timestamptz  NOT NULL,
    ended_at                    timestamptz  NOT NULL,
    terminal_state              text         NOT NULL,

    commitments                 jsonb        NOT NULL DEFAULT '[]'::jsonb,
    disputes                    jsonb        NOT NULL DEFAULT '[]'::jsonb,
    selected_path               text,
    contact_channel_confirmed   boolean,
    consent_given               boolean,
    terms_confirmed             boolean,

    protocol_version            text         NOT NULL,
    prompt_version              text         NOT NULL,
    model                       text         NOT NULL,

    needs_specialist_review     boolean      NOT NULL DEFAULT true,
    reviewed_by                 text,
    reviewed_at                 timestamptz,

    total_input_tokens          integer      NOT NULL DEFAULT 0,
    total_output_tokens         integer      NOT NULL DEFAULT 0,
    cost_usd                    double precision NOT NULL DEFAULT 0,
    wall_seconds                double precision NOT NULL DEFAULT 0,

    CONSTRAINT call_records_terminal_state_check CHECK (terminal_state IN (
        'completed_no_callback',
        'completed_needs_callback',
        'transferred_to_human',
        'not_right_party',
        'not_reached'
    )),
    CONSTRAINT call_records_selected_path_check CHECK (selected_path IS NULL OR selected_path IN (
        'pay_now',
        'payment_link',
        'schedule',
        'instalments'
    )),
    CONSTRAINT call_records_specialist_review_check CHECK (needs_specialist_review),
    CONSTRAINT call_records_review_pair_check CHECK (
        (reviewed_by IS NULL) = (reviewed_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS call_records_account_id_idx
    ON call_records (account_id);
CREATE INDEX IF NOT EXISTS call_records_started_at_idx
    ON call_records (started_at DESC);
CREATE INDEX IF NOT EXISTS call_records_terminal_state_idx
    ON call_records (terminal_state);

-- The specialist queue: unreviewed records, oldest first. Ordered by
-- started_at and by nothing else — no risk-based filtering, no "work the big
-- balances first". See the note above.
CREATE INDEX IF NOT EXISTS call_records_specialist_queue_idx
    ON call_records (started_at)
    WHERE reviewed_at IS NULL;


-- ---------------------------------------------------------------------
-- turn_traces — one row per conversational turn.
--
-- Mirrors trail.models.TurnTrace. Deliberately no foreign key to
-- call_records: turns are written as the call runs, and the call record only
-- lands when the call ends. A FK here would make every trace write depend on
-- an outcome that does not exist yet, and would silently discard the traces
-- of any call that crashes mid-flight — exactly the calls worth inspecting.
--
-- `customer_utterance` is '' on the opening turn, where the agent speaks first
-- and `extraction` is NULL.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS turn_traces (
    turn_id             uuid         PRIMARY KEY,
    call_id             uuid         NOT NULL,
    step                text         NOT NULL,
    agent_utterance     text         NOT NULL,
    customer_utterance  text         NOT NULL DEFAULT '',
    extraction          jsonb,
    latency_ms          integer      NOT NULL DEFAULT 0,
    created_at          timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT turn_traces_step_check CHECK (step IN (
        'verify_right_party',
        'disclose_and_consent',
        'state_balance',
        'confirm_terms',
        'offer_payment_path',
        'capture_commitment',
        'confirm_contact',
        'post_outcome'
    ))
);

CREATE INDEX IF NOT EXISTS turn_traces_call_id_created_at_idx
    ON turn_traces (call_id, created_at);
CREATE INDEX IF NOT EXISTS turn_traces_created_at_idx
    ON turn_traces (created_at DESC);


-- ---------------------------------------------------------------------
-- llm_call_traces — one row per model API call, successful or not.
--
-- Mirrors trail.models.LLMCallTrace. prompt_version and model are stamped on
-- the row rather than joined from config so a trace stays interpretable after
-- either changes — which matters more here than it looks, because a managed
-- model can reach end of life on a published date (BLUEPRINT §8) and the run
-- it produced still has to be readable afterwards. No foreign key, for the
-- same reason as turn_traces.
--
-- The three token columns are disjoint and the prompt is their sum:
-- `input_tokens` is the uncached remainder, `cache_read_input_tokens` is what
-- was served from cache at ~0.1x, and `cache_creation_input_tokens` is what
-- was written into it at 1.25x. Storing all three is what lets the economics
-- post recompute spend from the traces without trusting cost_usd.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS llm_call_traces (
    trace_id                    uuid         PRIMARY KEY,
    call_id                     uuid         NOT NULL,
    step                        text         NOT NULL,
    prompt_version              text         NOT NULL,
    model                       text         NOT NULL,
    request_json                jsonb        NOT NULL,
    response_json               jsonb        NOT NULL,
    input_tokens                integer      NOT NULL DEFAULT 0,
    output_tokens               integer      NOT NULL DEFAULT 0,
    cache_read_input_tokens     integer      NOT NULL DEFAULT 0,
    cache_creation_input_tokens integer      NOT NULL DEFAULT 0,
    cost_usd                    double precision NOT NULL DEFAULT 0,
    latency_ms                  integer      NOT NULL DEFAULT 0,
    created_at                  timestamptz  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS llm_call_traces_call_id_created_at_idx
    ON llm_call_traces (call_id, created_at);
CREATE INDEX IF NOT EXISTS llm_call_traces_created_at_idx
    ON llm_call_traces (created_at DESC);
CREATE INDEX IF NOT EXISTS llm_call_traces_model_prompt_version_idx
    ON llm_call_traces (model, prompt_version);


-- ---------------------------------------------------------------------
-- eval_runs — one row per golden-set run.
--
-- Mirrors trail.models.EvalRun, minus `findings`, which is normalised into
-- eval_findings so the failure taxonomy can be counted in SQL. Reassemble the
-- model by reading this row and its findings.
--
-- `metrics` holds a whole serialised MetricSet: it is a report, written once
-- and read whole, and splitting a whole MetricSet into one column per metric would
-- mean a migration every time one is added. This port added one —
-- `promise_capture_rate` — and touched nothing in this file, which is the
-- argument.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS eval_runs (
    run_id          uuid         PRIMARY KEY,
    started_at      timestamptz  NOT NULL,
    finished_at     timestamptz,
    status          text         NOT NULL,
    metrics         jsonb,
    regression_vs   uuid,
    regressions     jsonb        NOT NULL DEFAULT '[]'::jsonb,

    CONSTRAINT eval_runs_status_check CHECK (status IN (
        'running',
        'completed',
        'failed'
    )),
    CONSTRAINT eval_runs_finished_check CHECK (
        (status = 'running') = (finished_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS eval_runs_started_at_idx
    ON eval_runs (started_at DESC);
CREATE INDEX IF NOT EXISTS eval_runs_status_started_at_idx
    ON eval_runs (status, started_at DESC);


-- ---------------------------------------------------------------------
-- eval_findings — one row per scored discrepancy.
--
-- Mirrors trail.models.Finding, plus a surrogate `id` (the model has no
-- identifier of its own — the same case can legitimately produce several
-- identical-looking findings) and a `run_id` foreign key. The FK is safe
-- here, unlike on the trace tables: the eval run row is inserted before the
-- run starts, so the parent always exists.
--
-- `kind` is never collapsed to pass/fail. Omission dominates in the
-- literature, and a scorecard that only says "wrong" has nothing to say
-- about it (BLUEPRINT §6) — an amount that was never captured is not a
-- smaller error than an amount captured wrongly.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS eval_findings (
    id          bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id      uuid         NOT NULL REFERENCES eval_runs (run_id) ON DELETE CASCADE,
    case_id     text         NOT NULL,
    field       text         NOT NULL,
    kind        text         NOT NULL,
    expected    text,
    actual      text,
    detail      text         NOT NULL DEFAULT '',
    created_at  timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT eval_findings_kind_check CHECK (kind IN (
        'omission',
        'fabrication',
        'wrong_value'
    ))
);

CREATE INDEX IF NOT EXISTS eval_findings_run_id_idx
    ON eval_findings (run_id);
CREATE INDEX IF NOT EXISTS eval_findings_run_id_kind_idx
    ON eval_findings (run_id, kind);
CREATE INDEX IF NOT EXISTS eval_findings_case_id_idx
    ON eval_findings (case_id);
