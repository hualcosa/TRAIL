/**
 * The UI's own state shapes, and the one piece of real logic in the frontend:
 * how the ficha fills itself in while a call is still running.
 *
 * Worth reading, because it is the only place this client derives anything.
 *
 * `TurnResponse` carries no extraction — it is the agent's next approved
 * utterance, the step, and the record once the call is over. So during a live
 * call there is nothing on the turn payload that says whether identity was
 * confirmed or consent was given. The only truthful in-flight signal is the
 * pipeline itself: `advance` reports which step the machine moved *from* and
 * *to*, and a machine that left `verify_right_party` is a machine whose identity
 * compare passed. `judge` reports the terms verdict directly.
 *
 * That derivation is inference about the *state machine*, never about the
 * customer, and it is thrown away the moment the authoritative answer exists:
 * when the call finishes, `applyRecord` overwrites every capture row from
 * `CallRecord`. The alternative — leaving the ficha blank until the call ends —
 * would hide the thing the demo is for, and the alternative to *that* — asking
 * the model what it thinks was captured — is the classifier this whole system
 * refuses to build.
 */

import type {
  AdvanceDetail,
  CallRecord,
  ComplianceViolation,
  Dispute,
  ExtractDetail,
  JudgeDetail,
  PaymentCommitment,
  StageEvent,
  StageName,
  Step,
  TerminalState,
} from "./types";

/** Tri-state as the ficha prints it. `—` is "not asked", not "no". */
export type Tri = "sim" | "não" | "—";

export interface CaptureRows {
  identidade: Tri;
  consentimento: Tri;
  termos: Tri;
  caminho: Tri;
  canal: Tri;
}

export const EMPTY_CAPTURE: CaptureRows = {
  identidade: "—",
  consentimento: "—",
  termos: "—",
  caminho: "—",
  canal: "—",
};

export interface FichaState {
  capture: CaptureRows;
  commitments: PaymentCommitment[];
  disputes: Dispute[];
  record: CallRecord | null;
}

export const EMPTY_FICHA: FichaState = {
  capture: EMPTY_CAPTURE,
  commitments: [],
  disputes: [],
  record: null,
};

/**
 * What one completed turn cost, printed in the turn footer under the reply.
 *
 * Every field is nullable and the footer prints only the ones that are present.
 * The opening utterance is why: the agent speaks first, no extraction ran, and
 * there is nothing to measure — but there *is* a trace. Filling those fields
 * with zeros would put "0 ms · 0 tok · US$ 0,0000" on screen, which is not an
 * absent measurement, it is a claimed one, and it is wrong.
 */
export interface TurnMetrics {
  ms: number | null;
  tokens: number | null;
  costUsd: number | null;
  traceUrl: string | null;
}

export type Entry =
  | {
      kind: "agent";
      id: string;
      text: string;
      step: Step;
      /** True when the compliance gate forced this turn into a transfer. */
      transferred: boolean;
      /**
       * What the gate caught, verbatim from the `screen` frame. Rendered under
       * the stamp because the audience for this demo is the person who has to
       * sign off on the gate, and "it transferred" without the rule it fired on
       * is not a reviewable statement.
       */
      violations: ComplianceViolation[];
      metrics: TurnMetrics | null;
      /** False on replay/rehydrate; the reveal only animates on arrival. */
      fresh: boolean;
    }
  | { kind: "customer"; id: string; text: string }
  | {
      kind: "error";
      id: string;
      status: number;
      detail: string;
      traceUrl: string | null;
    };

/** Live state of the six-cell rail for the turn currently in flight. */
export interface RailCell {
  status: "pending" | "running" | "done" | "skipped";
  ms: number | null;
}

export type RailState = Record<StageName, RailCell>;

export const EMPTY_RAIL: RailState = {
  extract: { status: "pending", ms: null },
  judge: { status: "pending", ms: null },
  advance: { status: "pending", ms: null },
  screen: { status: "pending", ms: null },
  persist: { status: "pending", ms: null },
  finalise: { status: "pending", ms: null },
};

export function applyStageToRail(rail: RailState, event: StageEvent): RailState {
  const next: RailCell =
    event.status === "start"
      ? { status: "running", ms: null }
      : event.status === "skip"
        ? { status: "skipped", ms: event.ms }
        : { status: "done", ms: event.ms };
  return { ...rail, [event.stage]: next };
}

// ---------------------------------------------------------------------------
// Ficha derivation
// ---------------------------------------------------------------------------

/**
 * Which capture row each step answers, once the machine has moved past it.
 *
 * Leaving a step is the evidence. `capture_commitment`, `state_balance` and
 * `post_outcome` answer no row: they are things the agent says, not things the
 * customer confirms, and inventing a row for them would be the ficha claiming
 * a fact nobody stated.
 */
const STEP_TO_ROW: Partial<Record<Step, keyof CaptureRows>> = {
  verify_right_party: "identidade",
  disclose_and_consent: "consentimento",
  confirm_terms: "termos",
  offer_payment_path: "caminho",
  confirm_contact: "canal",
};

export function applyStageToFicha(
  ficha: FichaState,
  event: StageEvent,
): FichaState {
  if (event.status !== "done" || event.detail === null) return ficha;

  if (event.stage === "advance") {
    const detail = event.detail as AdvanceDetail;
    const row = STEP_TO_ROW[detail.from_step];
    if (!row) return ficha;

    // A call that ended as `not_right_party` did not fail to ask about
    // identity — it asked and got the wrong person. That is a `não`, and it is
    // the one terminal state that contradicts its own step rather than simply
    // stopping short of it.
    if (detail.terminal_state === "not_right_party") {
      return { ...ficha, capture: { ...ficha.capture, identidade: "não" } };
    }
    // Any other terminal state stops the machine where it stands. The row for
    // the step it stopped on stays `—`: the question was put and the answer
    // never resolved, which is neither a yes nor a no.
    if (detail.finished) return ficha;
    if (detail.to_step === detail.from_step) return ficha;

    return { ...ficha, capture: { ...ficha.capture, [row]: "sim" } };
  }

  if (event.stage === "judge") {
    const detail = event.detail as JudgeDetail;
    return {
      ...ficha,
      capture: {
        ...ficha.capture,
        termos: detail.terms_restated_correctly ? "sim" : "não",
      },
    };
  }

  return ficha;
}

function tri(value: boolean | null | undefined): Tri {
  if (value === null || value === undefined) return "—";
  return value ? "sim" : "não";
}

/**
 * Replace every derived value with the finished record's own.
 *
 * Called once, when `finished` turns true. Nothing derived survives this: the
 * record is what the specialist reviews and what the eval harness scores, so
 * the screen must agree with it exactly, including where it disagrees with
 * what the stage frames implied.
 */
export function applyRecord(record: CallRecord): FichaState {
  return {
    capture: {
      identidade: record.terminal_state === "not_right_party" ? "não" : "sim",
      consentimento: tri(record.consent_given),
      termos: tri(record.terms_confirmed),
      caminho: tri(record.selected_path !== null),
      canal: tri(record.contact_channel_confirmed),
    },
    commitments: record.commitments,
    disputes: record.disputes,
    record,
  };
}

/**
 * A record that never reached anyone answers no capture question at all.
 *
 * `POST /calls/{id}/unreachable` produces a real record with a real terminal
 * state, and `applyRecord` would read its empty booleans as five confident
 * `não`s. A call nobody answered did not refuse consent.
 */
export function applyUnreachedRecord(record: CallRecord): FichaState {
  return { ...EMPTY_FICHA, record };
}

export interface TurnAccumulator {
  tokens: number;
  costUsd: number;
  transferred: boolean;
  violations: ComplianceViolation[];
  startedAt: number;
}

export function accumulateStage(
  acc: TurnAccumulator,
  event: StageEvent,
): TurnAccumulator {
  if (event.status !== "done" || event.detail === null) return acc;
  if (event.stage === "extract") {
    const d = event.detail as ExtractDetail;
    return {
      ...acc,
      tokens: acc.tokens + d.input_tokens + d.output_tokens,
      costUsd: acc.costUsd + d.cost_usd,
    };
  }
  if (event.stage === "judge") {
    const d = event.detail as JudgeDetail;
    return {
      ...acc,
      tokens: acc.tokens + d.input_tokens + d.output_tokens,
      costUsd: acc.costUsd + d.cost_usd,
    };
  }
  if (event.stage === "screen") {
    const d = event.detail as { passed: boolean; forced_transfer: boolean; violations: ComplianceViolation[] };
    return { ...acc, transferred: d.forced_transfer, violations: d.violations };
  }
  return acc;
}

export function terminalFromStage(event: StageEvent): TerminalState | null {
  if (event.stage !== "advance" || event.status !== "done" || !event.detail) {
    return null;
  }
  return (event.detail as AdvanceDetail).terminal_state;
}
