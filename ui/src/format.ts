/**
 * Display formatting and the Portuguese label vocabulary.
 *
 * One rule governs this file: **nothing here interprets a value the customer
 * said.** Amounts and dates that came out of an extraction are rendered exactly
 * as they arrived — "mil e duzentos" stays "mil e duzentos", "sexta-feira" stays
 * "sexta-feira" — because normalising a spoken amount is where an order-of-
 * magnitude error gets manufactured, and the backend deliberately refuses to do
 * it (see `PaymentCommitment` in `src/trail/models.py`). The formatters below
 * only touch values the *system of record* owns: a balance, a due date, an
 * account number, a token count, a cost.
 */

import type { PaymentPath, Product, Step, TerminalState } from "./types";

/** Machine-layer step ids stay in English — they are identifiers, not prose. */
export const STEP_IDS: Step[] = [
  "verify_right_party",
  "disclose_and_consent",
  "state_balance",
  "confirm_terms",
  "offer_payment_path",
  "capture_commitment",
  "confirm_contact",
  "post_outcome",
];

export const TERMINAL_LABELS: Record<TerminalState, string> = {
  completed_no_callback: "concluída sem retorno",
  completed_needs_callback: "concluída com retorno",
  transferred_to_human: "transferida para especialista",
  not_right_party: "pessoa errada",
  not_reached: "não atendida",
};

export const PATH_LABELS: Record<PaymentPath, string> = {
  pay_now: "pagar agora",
  payment_link: "link de pagamento",
  schedule: "agendar",
  instalments: "parcelar",
};

export const PRODUCT_LABELS: Record<Product, string> = {
  personal_loan: "empréstimo pessoal",
  credit_card: "cartão de crédito",
};

/** The six pipeline stages as the rail prints them, in execution order. */
export const STAGE_LABELS: Array<{ stage: string; label: string }> = [
  { stage: "extract", label: "extrair" },
  { stage: "judge", label: "julgar" },
  { stage: "advance", label: "avançar" },
  { stage: "screen", label: "gate" },
  { stage: "persist", label: "gravar" },
  { stage: "finalise", label: "encerrar" },
];

/**
 * Format a `Decimal`-as-string balance in Brazilian reais.
 *
 * The value arrives as a string on purpose and is parsed here only to reach
 * `Intl.NumberFormat`, at the very last step before pixels. It is never parsed
 * anywhere a number could be stored, compared or added: this figure is spoken
 * aloud to a customer, and a rounding artefact in a balance is the blueprint's
 * zero-tolerance failure.
 */
export function formatBRL(value: string): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return value; // unparseable: show what we were sent
  return n.toLocaleString("pt-BR", {
    style: "currency",
    currency: "BRL",
    minimumFractionDigits: 2,
  });
}

/** ISO date (YYYY-MM-DD) to DD/MM/YYYY, without going near a Date object. */
export function formatDate(iso: string): string {
  const parts = iso.slice(0, 10).split("-");
  if (parts.length !== 3) return iso;
  return `${parts[2]}/${parts[1]}/${parts[0]}`;
}

/**
 * Mask a CPF for display: `•••.417.529-••`.
 *
 * Every CPF in this repo is invented and passes its own check digits, so there
 * is nothing here to protect. It is masked anyway because this panel is a mock
 * of a screen a collections specialist reads all day, and a demo that renders a
 * full tax id teaches the wrong habit to whoever builds the real one.
 */
export function maskCpf(taxId: string): string {
  const digits = taxId.replace(/\D/g, "");
  if (digits.length !== 11) return taxId;
  return `•••.${digits.slice(3, 6)}.${digits.slice(6, 9)}-••`;
}

/** US dollars at four decimals — model spend lands in fractions of a cent. */
export function formatUsd(value: number): string {
  return `US$ ${value.toLocaleString("pt-BR", {
    minimumFractionDigits: 4,
    maximumFractionDigits: 4,
  })}`;
}

export function formatSeconds(seconds: number): string {
  return `${seconds.toLocaleString("pt-BR", {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  })} s`;
}

export function formatTokens(count: number): string {
  return `${count.toLocaleString("pt-BR")} tok`;
}

/** Short call reference for the header — the Brazilian "protocolo". */
export function protocolRef(callId: string): string {
  return callId.replace(/-/g, "").slice(0, 8).toUpperCase();
}
