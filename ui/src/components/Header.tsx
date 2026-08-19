/**
 * The masthead: who is calling whom, and the meta a reviewer would quote.
 *
 * `protocolo` is the call reference — the Brazilian customer-service reading of
 * the word — and not a protocol *version*. That is a deliberate reading of an
 * ambiguous spec line. The agent service exposes no version endpoint
 * (`/healthz` answers `{"status": "ok"}`) and `protocol_version` only arrives on
 * `CallRecord` once the call has ended, so a hardcoded "v1" in the header would
 * be a fact on a compliance screen that the screen cannot source. The short
 * call id is available the moment the call opens, is the thing a reviewer
 * actually needs to quote, and is true.
 *
 * `modelo` follows the same rule: it reads `—` until the finished record names
 * the model, then prints it verbatim.
 */

import { protocolRef } from "../format";

/**
 * Three states, not two. A page with no call open is not an *ended* call —
 * "encerrada" before anyone has clicked Atender is a status line reporting an
 * outcome that never happened, and the whole point of the dot is that it can be
 * trusted at a glance.
 */
type CallStatus = "idle" | "live" | "closed";

interface HeaderProps {
  callId: string | null;
  model: string | null;
  status: CallStatus;
}

const STATUS_LABELS: Record<CallStatus, string> = {
  idle: "sem chamada",
  live: "ligada",
  closed: "encerrada",
};

export function Header({ callId, model, status }: HeaderProps) {
  return (
    <header className="masthead">
      <h1 className="masthead__title">Banco Aurora · Cobrança 1–30 DPD</h1>
      <div className="masthead__meta">
        <span className="masthead__field">
          <span className="masthead__key">protocolo</span>
          <span className="mono">{callId ? protocolRef(callId) : "—"}</span>
        </span>
        <span className="masthead__field">
          <span className="masthead__key">modelo</span>
          <span className="mono">{model ?? "—"}</span>
        </span>
        <span className={`masthead__status is-${status}`} role="status">
          <span className="masthead__dot" aria-hidden="true" />
          {STATUS_LABELS[status]}
        </span>
      </div>
    </header>
  );
}
