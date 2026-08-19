/**
 * The empty state: pick an account, then answer the call.
 *
 * The golden-set cases are offered by their own `description`, not by a label
 * written here, so a case added to the fixture shows up in this list on the
 * same commit. The default account is preselected and sits first — it is the
 * one `trail chat` uses, built by the same `demo_profile()`, so what a viewer
 * sees here is the same account they would get on the CLI.
 *
 * The paragraph about the typewriter is not filler. It is the only place the
 * demo can state, before anyone has seen a single utterance, that the reveal
 * is a visual effect and the approved text was already written. A reviewer who
 * learns that afterwards has already drawn the wrong conclusion.
 */

import { formatBRL } from "../format";
import type { DemoCase, DemoCases } from "../types";

interface CasePickerProps {
  cases: DemoCases | null;
  selected: string;
  starting: boolean;
  error: string | null;
  onSelect: (key: string) => void;
  onStart: () => void;
}

/** Stable option key: the default account has a null `case_id`. */
export function caseKey(item: DemoCase): string {
  return item.case_id ?? "__default__";
}

export function CasePicker({
  cases,
  selected,
  starting,
  error,
  onSelect,
  onStart,
}: CasePickerProps) {
  if (error) {
    return (
      <div className="empty">
        <p className="empty__lead">
          O serviço do agente não respondeu em <span className="mono">/api/demo/cases</span>.
          Suba o serviço e recarregue a página.
        </p>
        <p className="mono empty__detail">{error}</p>
      </div>
    );
  }

  if (!cases) {
    return (
      <div className="empty">
        <p className="mono empty__detail">carregando contas…</p>
      </div>
    );
  }

  const options: DemoCase[] = [cases.default, ...cases.cases];
  const current =
    options.find((item) => caseKey(item) === selected) ?? cases.default;

  return (
    <div className="empty">
      <p className="empty__lead">Nenhuma chamada aberta. Escolha uma conta e atenda.</p>

      <div className="empty__form">
        <div className="picker">
          <label className="picker__caption" htmlFor="case-select">
            Conta
          </label>
          <select
            id="case-select"
            className="picker__select"
            value={selected}
            onChange={(event) => onSelect(event.target.value)}
          >
            {options.map((item) => (
              <option key={caseKey(item)} value={caseKey(item)}>
                {item.label}
              </option>
            ))}
          </select>
        </div>

        <dl className="picker__preview">
          <div>
            <dt>titular</dt>
            <dd className="mono">{current.profile.full_name}</dd>
          </div>
          <div>
            <dt>saldo</dt>
            <dd className="mono">{formatBRL(current.profile.balance_brl)}</dd>
          </div>
          <div>
            <dt>atraso</dt>
            <dd className="mono">{current.profile.days_past_due} dias</dd>
          </div>
          <div>
            <dt>caso</dt>
            <dd className="mono">{current.case_id ?? "demonstração"}</dd>
          </div>
        </dl>

        <button
          type="button"
          className="btn btn--primary btn--wide"
          onClick={onStart}
          disabled={starting}
        >
          {starting ? "Atendendo…" : "Atender chamada"}
        </button>
      </div>

      <p className="empty__thesis">
        O modelo nunca escreve o que o cliente ouve. O texto falado é aprovado
        previamente e sai literal do protocolo; a digitação na tela é apenas um
        efeito visual. O que transmite ao vivo é a esteira do turno — extração,
        julgamento, avanço, gate de conformidade, gravação — com a latência, os
        tokens e o custo de cada etapa.
      </p>
    </div>
  );
}
