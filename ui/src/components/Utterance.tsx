/**
 * One block in the transcript, drawn as an NCR form field rather than a chat
 * bubble.
 *
 * The caption is notched into the top rule the way a legend sits in a fieldset,
 * and the agent and the customer are told apart by rule *style*, not by which
 * side of the screen they sit on: the agent's box is solid, because those words
 * were pre-printed on the form before the call began, and the customer's box is
 * dashed, because those words were filled in by hand. That distinction is the
 * product's thesis rendered as a border-style.
 */

import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useReducedMotion } from "../hooks";
import { formatTokens, formatUsd } from "../format";
import type { Entry, TurnMetrics } from "../state";

const CHAR_MS = 14;

/**
 * The typewriter reveal.
 *
 * **This is cosmetic and nothing else.** There is no token stream behind it.
 * The LLM never writes a word the customer hears — agent utterances are
 * verbatim compliance-approved protocol text, retrieved whole and already
 * complete in `text` before the first character is painted. The effect exists
 * to give the eye somewhere to rest while the reader takes in the stamp; it is
 * not evidence of generation and the empty state says so in prose.
 *
 * Accessibility: the animated span is `aria-hidden` and a complete copy of the
 * text is rendered for screen readers. Without that split, the transcript's
 * live region would re-announce the utterance on every one of its ~200
 * mutations.
 */
function ApprovedText({ text, animate }: { text: string; animate: boolean }) {
  const [shown, setShown] = useState(() => (animate ? 0 : text.length));
  const timer = useRef<number | null>(null);

  useEffect(() => {
    if (!animate) {
      setShown(text.length);
      return;
    }
    setShown(0);
    timer.current = window.setInterval(() => {
      setShown((n) => {
        if (n >= text.length) {
          if (timer.current !== null) window.clearInterval(timer.current);
          timer.current = null;
          return n;
        }
        return n + 1;
      });
    }, CHAR_MS);
    return () => {
      if (timer.current !== null) window.clearInterval(timer.current);
      timer.current = null;
    };
  }, [text, animate]);

  const complete = () => setShown(text.length);

  return (
    <p
      className="utterance__speech"
      // Clicking finishes the reveal early. It is not a control — the full text
      // is already in the accessibility tree — so it takes no tab stop and no
      // role, and keyboard users have nothing to skip.
      onClick={complete}
    >
      <span aria-hidden="true">{text.slice(0, shown)}</span>
      <span className="sr-only">{text}</span>
    </p>
  );
}

/**
 * O carimbo — the signature element.
 *
 * Reads `TEXTO APROVADO · §{step}` in approval green, or `TRANSFERIDO` in
 * carimbo red when the compliance gate forced the turn into a transfer. It
 * lands like a stamp: scale 1.35 down to 1 with a small overshoot, plus a
 * one-frame ink spread from the `::after` overlay in the stylesheet. Under
 * `prefers-reduced-motion` the animation is dropped entirely in CSS and the
 * stamp is simply there.
 */
function Carimbo({ step, transferred }: { step: string; transferred: boolean }) {
  return (
    <span
      className={`carimbo ${transferred ? "carimbo--transfer" : "carimbo--ok"}`}
    >
      {transferred ? (
        "Transferido"
      ) : (
        <>
          Texto aprovado ·{" "}
          {/* The step id keeps its own case. It is an identifier the backend
              uses verbatim, and upper-casing it into VERIFY_RIGHT_PARTY makes
              it read as a constant this UI invented rather than the key a
              reviewer can search the traces for. */}
          <span className="carimbo__step">§{step}</span>
        </>
      )}
    </span>
  );
}

/**
 * The one-line turn footer the stage rail collapses into.
 *
 * Built from the parts that exist rather than from a fixed template, so a turn
 * with no measurement behind it (the opening utterance) prints its trace link
 * alone instead of three zeros.
 */
function TurnFooter({ metrics }: { metrics: TurnMetrics | null }) {
  if (!metrics) return null;

  const parts: ReactNode[] = [];
  if (metrics.ms !== null) parts.push(<span key="ms">{metrics.ms} ms</span>);
  if (metrics.tokens !== null) {
    parts.push(<span key="tok">{formatTokens(metrics.tokens)}</span>);
  }
  if (metrics.costUsd !== null) {
    parts.push(<span key="usd">{formatUsd(metrics.costUsd)}</span>);
  }
  if (metrics.traceUrl) {
    parts.push(
      <a
        key="trace"
        className="turn-footer__trace"
        href={metrics.traceUrl}
        target="_blank"
        rel="noopener"
      >
        ver trace ↗
      </a>,
    );
  }
  if (parts.length === 0) return null;

  return (
    <p className="turn-footer mono">
      {parts.map((part, index) => (
        <span key={index}>
          {index > 0 ? <span aria-hidden="true"> · </span> : null}
          {part}
        </span>
      ))}
    </p>
  );
}

export function Utterance({ entry }: { entry: Entry }) {
  const reduced = useReducedMotion();

  if (entry.kind === "customer") {
    return (
      <article className="utterance utterance--customer">
        <span className="utterance__caption">Cliente</span>
        <p className="utterance__speech">{entry.text}</p>
      </article>
    );
  }

  if (entry.kind === "error") {
    return (
      <article className="utterance utterance--error" role="alert">
        <span className="utterance__caption">Falha no turno</span>
        <p className="utterance__failure">{failureGuidance(entry.status)}</p>
        <p className="mono utterance__failure-detail">
          {entry.status} · {entry.detail}
        </p>
        {entry.traceUrl ? (
          <p className="turn-footer mono">
            <a
              className="turn-footer__trace"
              href={entry.traceUrl}
              target="_blank"
              rel="noopener"
            >
              ver trace ↗
            </a>
          </p>
        ) : null}
      </article>
    );
  }

  return (
    <article className="utterance utterance--agent">
      <span className="utterance__caption">Agente</span>
      <ApprovedText text={entry.text} animate={entry.fresh && !reduced} />
      <div className="utterance__seal">
        <Carimbo step={entry.step} transferred={entry.transferred} />
      </div>
      {entry.violations.length > 0 ? (
        <ul className="violations">
          {entry.violations.map((violation, index) => (
            <li className="violations__item mono" key={index}>
              <span className="violations__check">{violation.check}</span>
              <span className="violations__rule">{violation.rule}</span>
              <span className="violations__detail">{violation.detail}</span>
              <span className="violations__evidence">
                “{violation.evidence}”
              </span>
            </li>
          ))}
        </ul>
      ) : null}
      <TurnFooter metrics={entry.metrics} />
    </article>
  );
}

/**
 * What the reviewer should do about this failure.
 *
 * Errors state what broke and the next action. They do not apologise and they
 * are never vague: "algo deu errado" tells a compliance reviewer watching a
 * demo nothing, and the server's own `detail` is printed underneath unedited so
 * the specific cause survives.
 */
function failureGuidance(status: number): string {
  if (status === 502) {
    return "A extração falhou no upstream. Reenvie o mesmo turno.";
  }
  if (status === 409) {
    return "A chamada já está encerrada. Abra uma nova chamada para continuar.";
  }
  if (status === 404) {
    return "A chamada não existe mais no serviço. Abra uma nova chamada.";
  }
  if (status === 422 || status === 400) {
    return "O serviço recusou o corpo do turno. Verifique o texto e reenvie.";
  }
  return "O turno não foi concluído. Reenvie o mesmo turno.";
}
