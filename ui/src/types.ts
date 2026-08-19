/**
 * TypeScript mirrors of the agent service's wire contract.
 *
 * These are hand-maintained copies of the Pydantic models in
 * `src/trail/models.py` plus the two demo-only shapes the UI needs
 * (`/demo/cases` and the SSE frame vocabulary of
 * `POST /calls/{call_id}/turns/stream`). They are deliberately structural and
 * lossy in exactly one direction: every field the UI reads is typed, and
 * nothing here validates. A mismatch surfaces as a blank cell rather than a
 * thrown parse error, because a demo that refuses to render because the backend
 * grew a field is worse than one that renders the fields it understands.
 *
 * Money is a caveat worth repeating from the Python side. `balance_brl` arrives
 * as a JSON string, not a number, because it is a `Decimal` the customer will
 * hear spoken aloud and binary floating point has no business near it. It is
 * formatted for display and never arithmetic. `cost_usd` is a real `number`:
 * model spend in fractions of a cent, an analytics figure and not a ledger.
 */

export type Step =
  | "verify_right_party"
  | "disclose_and_consent"
  | "state_balance"
  | "confirm_terms"
  | "offer_payment_path"
  | "capture_commitment"
  | "confirm_contact"
  | "post_outcome";

export type TerminalState =
  | "completed_no_callback"
  | "completed_needs_callback"
  | "transferred_to_human"
  | "not_right_party"
  | "not_reached";

export type PaymentPath = "pay_now" | "payment_link" | "schedule" | "instalments";

export type Product = "personal_loan" | "credit_card";

export interface PaymentCommitment {
  amount: string | null;
  date: string | null;
  method: PaymentPath | null;
  /** Verbatim customer words this commitment came from. Never reformatted. */
  source_utterance: string;
}

export interface Dispute {
  subject: string;
  detail: string | null;
  source_utterance: string;
}

export interface AccountProfile {
  account_id: string;
  full_name: string;
  /** CPF, 11 digits, no punctuation. Synthetic. Masked before display. */
  tax_id: string;
  date_of_birth: string;
  phone: string;
  product: Product;
  /** JSON string, not a number — see the module docstring. */
  balance_brl: string;
  due_date: string;
  days_past_due: number;
}

export interface CallRecord {
  call_id: string;
  account_id: string;
  started_at: string;
  ended_at: string;
  terminal_state: TerminalState;

  commitments: PaymentCommitment[];
  disputes: Dispute[];
  selected_path: PaymentPath | null;
  contact_channel_confirmed: boolean | null;
  consent_given: boolean | null;
  terms_confirmed: boolean | null;

  protocol_version: string;
  prompt_version: string;
  model: string;

  needs_specialist_review: true;
  reviewed_by: string | null;
  reviewed_at: string | null;

  total_input_tokens: number;
  total_output_tokens: number;
  cost_usd: number;
  wall_seconds: number;
}

/**
 * `trace_id` and `trace_url` are observability metadata and are null whenever
 * tracing is disabled, which is the default offline. Treat a null as "no trace
 * to link", never as an error.
 */
export interface StartCallResponse {
  call_id: string;
  step: Step;
  agent_utterance: string;
  finished: boolean;
  terminal_state: TerminalState | null;
  trace_id?: string | null;
  trace_url?: string | null;
}

export interface TurnResponse {
  call_id: string;
  step: Step;
  agent_utterance: string;
  finished: boolean;
  terminal_state: TerminalState | null;
  /** Populated exactly when `finished` is true. */
  record: CallRecord | null;
  trace_id?: string | null;
  trace_url?: string | null;
}

// ---------------------------------------------------------------------------
// GET /demo/cases
// ---------------------------------------------------------------------------

export interface DemoCase {
  /** Null on the default account, which belongs to no golden-set case. */
  case_id: string | null;
  label: string;
  profile: AccountProfile;
}

export interface DemoCases {
  default: DemoCase;
  cases: DemoCase[];
}

// ---------------------------------------------------------------------------
// SSE frames — POST /calls/{call_id}/turns/stream
// ---------------------------------------------------------------------------

/**
 * The six pipeline stages, in the order the backend runs them.
 *
 * This is the whole reason the stream exists. The LLM never writes a word the
 * customer hears, so there is no token stream to show; what streams is the work
 * the turn actually did. `finalise` is emitted only on the turn that ends the
 * call, and `judge` is emitted with status "skip" on every step other than
 * confirm_terms.
 */
export type StageName =
  | "extract"
  | "judge"
  | "advance"
  | "screen"
  | "persist"
  | "finalise";

export type StageStatus = "start" | "done" | "skip";

export interface ExtractDetail {
  step: Step;
  understood: boolean;
  needs_human: boolean;
  unresolved: boolean;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
}

export interface JudgeDetail {
  terms_restated_correctly: boolean;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
}

export interface AdvanceDetail {
  from_step: Step;
  to_step: Step;
  finished: boolean;
  terminal_state: TerminalState | null;
}

export interface ComplianceViolation {
  check: string;
  rule: string;
  detail: string;
  evidence: string;
}

export interface ScreenDetail {
  passed: boolean;
  forced_transfer: boolean;
  violations: ComplianceViolation[];
}

export interface FinaliseDetail {
  terminal_state: TerminalState;
}

export type StageDetail =
  | ExtractDetail
  | JudgeDetail
  | AdvanceDetail
  | ScreenDetail
  | FinaliseDetail
  | Record<string, never>;

export interface StageEvent {
  stage: StageName;
  status: StageStatus;
  ms: number | null;
  detail: StageDetail | null;
}

export interface TraceEvent {
  trace_id: string | null;
  trace_url: string | null;
}

export interface ErrorEvent {
  status: number;
  detail: string;
}

/**
 * One decoded SSE frame. `trace` always arrives last, even when the turn
 * failed; `error` replaces `turn` rather than accompanying it.
 */
export type TurnStreamEvent =
  | { event: "stage"; data: StageEvent }
  | { event: "turn"; data: TurnResponse }
  | { event: "trace"; data: TraceEvent }
  | { event: "error"; data: ErrorEvent };
