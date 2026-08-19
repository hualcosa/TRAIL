/**
 * A ficha da chamada — the record filling itself in.
 *
 * Drawn as the tear-off stub of a boleto: canary carbon ground, no elevation,
 * no card, and leader dots running from each label to its value the way a
 * printed form leads your eye across a line you are meant to fill.
 *
 * Two decisions carry the product's argument.
 *
 * **Tri-state values are words, never icons and never pills.** `—` means the
 * question was not put, which is a genuinely different fact from `não`, and a
 * coloured pill would smuggle in a judgement a collections record is not
 * allowed to make — a green *sim* reads as good news about a person. Only `não`
 * takes colour, because a refusal is what a reviewer scans for.
 *
 * **Every promise carries the customer's own words underneath it.** The
 * `source_utterance` is printed verbatim, in italic serif, against a carbon
 * rule: not summarised, not reformatted, not trimmed. That indent is the thesis
 * made visible — a reviewer sees the claim and its evidence on the same line of
 * sight, and the amount above it stays exactly as spoken because the backend
 * refuses to normalise it.
 */

import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  PATH_LABELS,
  PRODUCT_LABELS,
  TERMINAL_LABELS,
  formatBRL,
  formatDate,
  formatSeconds,
  formatTokens,
  formatUsd,
  maskCpf,
} from "../format";
import type { FichaState, Tri } from "../state";
import type { AccountProfile } from "../types";

interface FichaProps {
  profile: AccountProfile | null;
  ficha: FichaState;
}

function Section({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <section className="ficha__section">
      <h3 className="ficha__heading">{title}</h3>
      {children}
    </section>
  );
}

function Row({
  label,
  value,
  tone,
  flash,
}: {
  label: string;
  value: string;
  tone?: "muted" | "negative";
  flash?: boolean;
}) {
  return (
    <div className={`ficha__row ${flash ? "is-fresh" : ""}`}>
      <span className="ficha__label">{label}</span>
      <span className="ficha__leader" aria-hidden="true" />
      <span className={`ficha__value mono ${tone ? `is-${tone}` : ""}`}>
        {value}
      </span>
    </div>
  );
}

const CAPTURE_ROWS: Array<[keyof FichaState["capture"], string]> = [
  ["identidade", "identidade"],
  ["consentimento", "consentimento"],
  ["termos", "termos"],
  ["caminho", "caminho"],
  ["canal", "canal"],
];

function toneFor(value: Tri): "muted" | "negative" | undefined {
  if (value === "—") return "muted";
  if (value === "não") return "negative";
  return undefined;
}

/**
 * Track which values changed since the last render so they can flash once.
 *
 * A background flash rather than a movement: the ficha is a document being
 * filled in, and paper does not slide. Comparing in an effect rather than
 * during render keeps this a side effect, and the flash is a pure CSS animation
 * that `prefers-reduced-motion` leaves as a plain colour change.
 */
function useFreshKeys(values: Record<string, string>): Set<string> {
  const previous = useRef<Record<string, string> | null>(null);
  const [fresh, setFresh] = useState<Set<string>>(new Set());

  useEffect(() => {
    const before = previous.current;
    previous.current = values;
    if (before === null) return; // first paint is not an arrival
    const changed = new Set<string>();
    for (const [key, value] of Object.entries(values)) {
      if (before[key] !== value) changed.add(key);
    }
    if (changed.size === 0) return;
    setFresh(changed);
    const timer = window.setTimeout(() => setFresh(new Set()), 900);
    return () => window.clearTimeout(timer);
    // The dependency is the serialised snapshot: `values` is rebuilt every
    // render, so depending on the object itself would flash every field on
    // every keystroke elsewhere in the app.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(values)]);

  return fresh;
}

export function Ficha({ profile, ficha }: FichaProps) {
  const { capture, commitments, disputes, record } = ficha;
  const fresh = useFreshKeys(capture as unknown as Record<string, string>);

  return (
    <div className="ficha__body">
      <Section title="Conta">
        {profile ? (
          <>
            <Row label="titular" value={profile.full_name} />
            <Row label="conta" value={profile.account_id} />
            <Row label="cpf" value={maskCpf(profile.tax_id)} />
            <Row label="produto" value={PRODUCT_LABELS[profile.product]} />
            <Row label="saldo" value={formatBRL(profile.balance_brl)} />
            <Row label="vencimento" value={formatDate(profile.due_date)} />
            <Row label="atraso" value={`${profile.days_past_due} dias`} />
            <Row label="telefone" value={profile.phone} />
          </>
        ) : (
          <p className="ficha__empty">Nenhuma conta selecionada.</p>
        )}
      </Section>

      <Section title="Captura">
        {CAPTURE_ROWS.map(([key, label]) => (
          <Row
            key={key}
            label={label}
            value={capture[key]}
            tone={toneFor(capture[key])}
            flash={fresh.has(key)}
          />
        ))}
      </Section>

      <Section title="Promessas">
        {commitments.length === 0 ? (
          <p className="ficha__empty">(nenhuma ainda)</p>
        ) : (
          commitments.map((commitment, index) => (
            <div className="ficha__entry" key={index}>
              <div className="ficha__row">
                <span className="ficha__label">valor · data</span>
                <span className="ficha__leader" aria-hidden="true" />
                <span className="ficha__value mono">
                  {commitment.amount ?? "—"}
                  <span aria-hidden="true"> · </span>
                  {commitment.date ?? "—"}
                </span>
              </div>
              {commitment.method ? (
                <Row label="forma" value={PATH_LABELS[commitment.method]} />
              ) : null}
              <blockquote className="ficha__evidence">
                {commitment.source_utterance}
              </blockquote>
            </div>
          ))
        )}
      </Section>

      <Section title="Disputas">
        {disputes.length === 0 ? (
          <p className="ficha__empty">(nenhuma)</p>
        ) : (
          disputes.map((dispute, index) => (
            <div className="ficha__entry" key={index}>
              <div className="ficha__row">
                <span className="ficha__label">assunto</span>
                <span className="ficha__leader" aria-hidden="true" />
                <span className="ficha__value mono is-negative">
                  {dispute.subject}
                </span>
              </div>
              {dispute.detail ? (
                <Row label="detalhe" value={dispute.detail} />
              ) : null}
              <blockquote className="ficha__evidence">
                {dispute.source_utterance}
              </blockquote>
            </div>
          ))
        )}
      </Section>

      {record ? (
        <Section title="Fechamento">
          <div className="ficha__terminal">
            <span
              className={`carimbo carimbo--flat ${
                record.terminal_state === "completed_no_callback"
                  ? "carimbo--ok"
                  : "carimbo--transfer"
              }`}
            >
              {TERMINAL_LABELS[record.terminal_state]}
            </span>
          </div>
          {record.selected_path ? (
            <Row label="caminho escolhido" value={PATH_LABELS[record.selected_path]} />
          ) : null}
          <Row label="custo total" value={formatUsd(record.cost_usd)} />
          <Row label="duração" value={formatSeconds(record.wall_seconds)} />
          <Row
            label="tokens"
            value={formatTokens(
              record.total_input_tokens + record.total_output_tokens,
            )}
          />
          <Row label="revisão" value="pendente · especialista" />
          <p className="ficha__note">
            Todo registro vai para a mesma fila de especialistas, na mesma ordem,
            sem priorização.
          </p>
        </Section>
      ) : null}
    </div>
  );
}

/** One-line summary for the collapsed mobile strip. */
export function fichaSummary(ficha: FichaState): string {
  const { capture, commitments, record } = ficha;
  if (record) return TERMINAL_LABELS[record.terminal_state];
  const answered = Object.values(capture).filter((v) => v !== "—").length;
  return `${answered}/5 capturado · ${commitments.length} promessa${
    commitments.length === 1 ? "" : "s"
  }`;
}
