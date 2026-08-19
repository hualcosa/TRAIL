/**
 * The composer — where the demo driver speaks as the customer.
 *
 * Enter sends and Shift+Enter opens a line, which is the convention every
 * messaging surface has trained into people's hands. While a turn is in flight
 * the field and both buttons are disabled and the send button *says* so rather
 * than only looking so: a greyed control with an unchanged label is
 * indistinguishable from a click that did not register, and this demo is
 * watched by people who will click twice.
 *
 * Once the call finishes the whole composer is replaced by its terminal state
 * and a way to start over. Leaving a disabled field on screen would invite one
 * more attempt at a call that has already been recorded and closed.
 */

import { useEffect, useRef, useState } from "react";
import { TERMINAL_LABELS } from "../format";
import type { TerminalState } from "../types";

interface ComposerProps {
  busy: boolean;
  finished: TerminalState | null;
  onSend: (utterance: string) => void;
  onUnreachable: () => void;
  onRestart: () => void;
}

export function Composer({
  busy,
  finished,
  onSend,
  onUnreachable,
  onRestart,
}: ComposerProps) {
  const [text, setText] = useState("");
  const field = useRef<HTMLTextAreaElement | null>(null);

  // Return focus to the field the moment the turn lands, so a demo driver can
  // type the next customer line without reaching for the mouse.
  useEffect(() => {
    if (!busy && !finished) field.current?.focus();
  }, [busy, finished]);

  if (finished) {
    return (
      <div className="composer composer--closed">
        <p className="composer__closed-label">
          <span className="eyebrow">Chamada encerrada</span>
          <span className="mono">{TERMINAL_LABELS[finished]}</span>
        </p>
        <button type="button" className="btn btn--primary" onClick={onRestart}>
          Nova chamada
        </button>
      </div>
    );
  }

  const submit = () => {
    const value = text.trim();
    if (!value || busy) return;
    setText("");
    onSend(value);
  };

  return (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <div className="composer__field">
        <label className="composer__caption" htmlFor="composer-input">
          Fala do cliente
        </label>
        <textarea
          id="composer-input"
          ref={field}
          className="composer__input"
          rows={2}
          value={text}
          disabled={busy}
          placeholder="Fale como o cliente…"
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              submit();
            }
          }}
        />
      </div>
      <div className="composer__actions">
        <button
          type="submit"
          className="btn btn--primary"
          disabled={busy || text.trim() === ""}
        >
          {busy ? "Processando…" : "Enviar"}
        </button>
        <button
          type="button"
          className="btn btn--stamp"
          disabled={busy}
          onClick={onUnreachable}
        >
          Encerrar como não atendida
        </button>
      </div>
    </form>
  );
}
