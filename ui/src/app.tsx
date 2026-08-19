/**
 * The demo shell: one call at a time, from account picker to closed record.
 *
 * All call state lives here and is passed down; there is no store and no
 * context, because there is exactly one call and its whole lifetime fits on one
 * screen. The only genuinely intricate part is `runTurn`, which consumes the
 * SSE stream and is documented where it sits.
 *
 * Note what this component never does: it does not decide anything about the
 * customer. It renders the steps the state machine took, the verdicts the gate
 * returned and the words the customer said. Every judgement on this screen was
 * made by the backend and is reproduced, not re-derived.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, fetchDemoCases, markUnreachable, startCall, streamTurn } from "./api";
import { CasePicker, caseKey } from "./components/CasePicker";
import { Composer } from "./components/Composer";
import { Ficha, fichaSummary } from "./components/Ficha";
import { Header } from "./components/Header";
import { StageRail } from "./components/StageRail";
import { Transcript } from "./components/Transcript";
import { useNarrowViewport } from "./hooks";
import {
  EMPTY_FICHA,
  EMPTY_RAIL,
  accumulateStage,
  applyRecord,
  applyStageToFicha,
  applyStageToRail,
  applyUnreachedRecord,
  terminalFromStage,
  type Entry,
  type FichaState,
  type RailState,
  type TurnAccumulator,
} from "./state";
import type {
  AccountProfile,
  DemoCases,
  ErrorEvent,
  Step,
  TerminalState,
  TurnResponse,
} from "./types";

let sequence = 0;
/** Entry ids only need to be unique and stable for React's reconciler. */
function nextId(prefix: string): string {
  sequence += 1;
  return `${prefix}-${sequence}`;
}

export function App() {
  const [cases, setCases] = useState<DemoCases | null>(null);
  const [casesError, setCasesError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string>("__default__");
  const [starting, setStarting] = useState(false);

  const [callId, setCallId] = useState<string | null>(null);
  const [profile, setProfile] = useState<AccountProfile | null>(null);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [ficha, setFicha] = useState<FichaState>(EMPTY_FICHA);
  const [step, setStep] = useState<Step | null>(null);
  const [terminal, setTerminal] = useState<TerminalState | null>(null);
  const [rail, setRail] = useState<RailState | null>(null);
  const [busy, setBusy] = useState(false);

  const narrow = useNarrowViewport();
  const [fichaOpen, setFichaOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchDemoCases()
      .then((payload) => {
        if (cancelled) return;
        setCases(payload);
        setSelected(caseKey(payload.default));
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setCasesError(error instanceof Error ? error.message : String(error));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const selectedProfile = useMemo<AccountProfile | null>(() => {
    if (!cases) return null;
    const all = [cases.default, ...cases.cases];
    return (all.find((item) => caseKey(item) === selected) ?? cases.default)
      .profile;
  }, [cases, selected]);

  const model = ficha.record?.model ?? null;

  const reset = useCallback(() => {
    setCallId(null);
    setProfile(null);
    setEntries([]);
    setFicha(EMPTY_FICHA);
    setStep(null);
    setTerminal(null);
    setRail(null);
    setBusy(false);
  }, []);

  const onStart = useCallback(async () => {
    if (!cases || !selectedProfile) return;
    const chosen =
      [cases.default, ...cases.cases].find(
        (item) => caseKey(item) === selected,
      ) ?? cases.default;
    setStarting(true);
    try {
      const started = await startCall(chosen.profile, chosen.case_id);
      setCallId(started.call_id);
      setProfile(chosen.profile);
      setStep(started.step);
      setTerminal(started.terminal_state);
      setEntries([
        {
          kind: "agent",
          id: nextId("agent"),
          text: started.agent_utterance,
          step: started.step,
          transferred: false,
          violations: [],
          // The opening turn has no pipeline behind it — the agent speaks
          // first, nothing was extracted — so it carries a trace link and no
          // latency figures. Printing zeros there would claim a measurement
          // that was never taken.
          metrics: started.trace_url
            ? { ms: null, tokens: null, costUsd: null, traceUrl: started.trace_url }
            : null,
          fresh: true,
        },
      ]);
    } catch (error: unknown) {
      const detail =
        error instanceof ApiError
          ? `${error.status} · ${error.message}`
          : String(error);
      setCasesError(detail);
    } finally {
      setStarting(false);
    }
  }, [cases, selected, selectedProfile]);

  /**
   * Run one customer turn and play the pipeline as it arrives.
   *
   * The customer's line is appended before the request goes out, because the
   * customer said it — it is not contingent on the turn succeeding, and a
   * transcript that drops it on a 502 loses the evidence for the retry.
   *
   * Everything the stream reports is accumulated in local variables rather than
   * state: the loop can run for seconds across many frames, and reading a piece
   * of React state inside it would read the value captured when the callback
   * was created. State is written on every frame (so the rail and the ficha
   * move live) and read exactly never.
   *
   * The stream always ends with a `trace` frame, including on failure, so the
   * trace link is attached after the loop rather than at the point the `turn`
   * or `error` frame lands.
   */
  const runTurn = useCallback(
    async (utterance: string) => {
      if (!callId || busy) return;
      setEntries((prev) => [
        ...prev,
        { kind: "customer", id: nextId("customer"), text: utterance },
      ]);
      setBusy(true);
      setRail(EMPTY_RAIL);

      let acc: TurnAccumulator = {
        tokens: 0,
        costUsd: 0,
        transferred: false,
        violations: [],
        startedAt: performance.now(),
      };
      let response: TurnResponse | null = null;
      let failure: ErrorEvent | null = null;
      let traceUrl: string | null = null;
      let stageTerminal: TerminalState | null = null;

      try {
        for await (const frame of streamTurn(callId, utterance)) {
          if (frame.event === "stage") {
            const event = frame.data;
            acc = accumulateStage(acc, event);
            stageTerminal = terminalFromStage(event) ?? stageTerminal;
            setRail((prev) => applyStageToRail(prev ?? EMPTY_RAIL, event));
            setFicha((prev) => applyStageToFicha(prev, event));
          } else if (frame.event === "turn") {
            response = frame.data;
          } else if (frame.event === "error") {
            failure = frame.data;
          } else {
            traceUrl = frame.data.trace_url;
          }
        }
      } catch (error: unknown) {
        // The stream never opened, or it broke mid-flight. Either way the call
        // stays open on the server and the same turn can be resubmitted.
        failure =
          error instanceof ApiError
            ? { status: error.status, detail: error.message }
            : { status: 0, detail: String(error) };
      }

      const ms = Math.round(performance.now() - acc.startedAt);

      if (response) {
        const turn = response;
        setStep(turn.step);
        setTerminal(turn.terminal_state);
        setEntries((prev) => [
          ...prev,
          {
            kind: "agent",
            id: nextId("agent"),
            text: turn.agent_utterance,
            step: turn.step,
            transferred: acc.transferred,
            violations: acc.violations,
            metrics: {
              ms,
              tokens: acc.tokens,
              costUsd: acc.costUsd,
              traceUrl: turn.trace_url ?? traceUrl,
            },
            fresh: true,
          },
        ]);
        if (turn.record) setFicha(applyRecord(turn.record));
      } else {
        const problem = failure ?? {
          status: 0,
          detail: "O stream terminou sem resposta do turno.",
        };
        setEntries((prev) => [
          ...prev,
          {
            kind: "error",
            id: nextId("error"),
            status: problem.status,
            detail: problem.detail,
            traceUrl,
          },
        ]);
        // A failed turn leaves the call open on purpose — the state machine did
        // not advance, so the same utterance can be sent again. The one
        // exception is a terminal state the stage frames already reported
        // before the failure, which is the machine having finished.
        if (stageTerminal) setTerminal(stageTerminal);
      }

      setRail(null);
      setBusy(false);
    },
    [busy, callId],
  );

  const onUnreachable = useCallback(async () => {
    if (!callId || busy) return;
    setBusy(true);
    try {
      const record = await markUnreachable(callId, "sem atendimento");
      setTerminal(record.terminal_state);
      setFicha(applyUnreachedRecord(record));
    } catch (error: unknown) {
      const problem =
        error instanceof ApiError
          ? { status: error.status, detail: error.message }
          : { status: 0, detail: String(error) };
      setEntries((prev) => [
        ...prev,
        {
          kind: "error",
          id: nextId("error"),
          status: problem.status,
          detail: problem.detail,
          traceUrl: null,
        },
      ]);
    } finally {
      setBusy(false);
    }
  }, [busy, callId]);

  const fichaPanel = (
    <Ficha profile={profile ?? selectedProfile} ficha={ficha} />
  );

  return (
    <div className="shell">
      <Header
        callId={callId}
        model={model}
        status={callId === null ? "idle" : terminal ? "closed" : "live"}
      />

      <main className="layout">
        <section className="call" aria-label="Chamada">
          {callId ? (
            <>
              <p className="call__step">
                <span className="eyebrow">passo</span>
                <span aria-hidden="true" className="call__caret">
                  ▸
                </span>
                <span className="mono">{step ?? "—"}</span>
              </p>
              <Transcript entries={entries} />
              {rail ? <StageRail rail={rail} /> : null}
              {narrow ? (
                <div className="ficha-strip">
                  <button
                    type="button"
                    className="ficha-strip__toggle"
                    aria-expanded={fichaOpen}
                    aria-controls="ficha-panel"
                    onClick={() => setFichaOpen((open) => !open)}
                  >
                    <span className="eyebrow">Ficha da chamada</span>
                    <span className="mono">{fichaSummary(ficha)}</span>
                    <span aria-hidden="true">{fichaOpen ? "▲" : "▼"}</span>
                  </button>
                  {fichaOpen ? (
                    <div id="ficha-panel" className="ficha ficha--inline">
                      {fichaPanel}
                    </div>
                  ) : null}
                </div>
              ) : null}
              <Composer
                busy={busy}
                finished={terminal}
                onSend={runTurn}
                onUnreachable={onUnreachable}
                onRestart={reset}
              />
            </>
          ) : (
            <CasePicker
              cases={cases}
              selected={selected}
              starting={starting}
              error={casesError}
              onSelect={setSelected}
              onStart={onStart}
            />
          )}
        </section>

        {!narrow ? (
          <aside className="ficha" aria-label="Ficha da chamada">
            <h2 className="ficha__title">Ficha da chamada</h2>
            {fichaPanel}
          </aside>
        ) : null}
      </main>
    </div>
  );
}
