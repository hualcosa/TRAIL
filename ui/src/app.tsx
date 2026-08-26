/**
 * The whole application: one conversation, its history, and the pipeline
 * behind every answer.
 *
 * All state lives here and is passed down. There is no store and no context,
 * because there are eleven values and one place that changes them, and a store
 * would be indirection bought with nothing.
 *
 * The turn loop is the only intricate part, and its shape is forced. A turn
 * arrives as a stream of frames, so the accumulator has to live in local
 * variables rather than in state: the loop spans many renders, and a closed-over
 * `useState` value would be stale by the second frame.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApiError,
  deleteThread,
  fetchThread,
  fetchThreads,
  startThread,
  streamTurn,
} from "./api";
import { useNarrowViewport, usePersisted } from "./hooks";
import {
  EMPTY_ACCUMULATOR,
  accumulateStage,
  metricsFrom,
  nextId,
  type Entry,
  type TurnAccumulator,
} from "./state";
import type { StartThreadResponse, ThreadSummary, TurnEvent } from "./types";

import { Composer } from "./components/Composer";
import { Header, NEXT_THEME } from "./components/Header";
import { Sidebar } from "./components/Sidebar";
import { Transcript } from "./components/Transcript";

export function App() {
  const [thread, setThread] = useState<StartThreadResponse | null>(null);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [durable, setDurable] = useState(true);
  const [busy, setBusy] = useState(false);
  const [fatal, setFatal] = useState<string | null>(null);

  const narrow = useNarrowViewport();
  const [theme, setTheme] = usePersisted("trail.theme", "");
  // Two pieces of state for one panel, and the split is the fix for a real
  // failure. The persisted preference governs the *wide* layout, where the
  // sidebar shares the width. Below the breakpoint it overlays the
  // conversation, so it starts closed every time and is never remembered — a
  // phone that restored "open" would greet its reader with a list covering the
  // thing they came to read.
  const [sidebarPref, setSidebarPref] = usePersisted("trail.sidebar", "1");
  const [overlayOpen, setOverlayOpen] = useState(false);
  const sidebarOpen = narrow ? overlayOpen : sidebarPref === "1";

  const toggleSidebar = useCallback(() => {
    if (narrow) setOverlayOpen((open) => !open);
    else setSidebarPref(sidebarPref === "1" ? "0" : "1");
  }, [narrow, sidebarPref, setSidebarPref]);

  // The <html> attribute is what `light-dark()` reads. Removed rather than set
  // to a sentinel for "system", so the CSS falls back to the media query.
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "light" || theme === "dark") root.dataset.theme = theme;
    else delete root.dataset.theme;
  }, [theme]);

  const refreshThreads = useCallback(async () => {
    try {
      const list = await fetchThreads();
      setThreads(list.threads);
      setDurable(list.durable);
    } catch {
      // A failed list is not a failed conversation. The sidebar stays as it
      // was rather than emptying, which would read as "your history is gone".
    }
  }, []);

  const openThread = useCallback(async () => {
    try {
      const opened = await startThread();
      setThread(opened);
      setEntries(
        opened.greeting
          ? [
              {
                kind: "agent",
                id: nextId(),
                text: opened.greeting,
                rail: [],
                violations: [],
                blocked: false,
                // No metrics: the greeting is the example's own string and no
                // model ran. Zeroes here would be a measurement of nothing.
                metrics: {
                  ms: null,
                  tokensIn: null,
                  tokensOut: null,
                  costUsd: null,
                  traceUrl: null,
                },
                fresh: false,
              },
            ]
          : [],
      );
      setFatal(null);
      void refreshThreads();
    } catch (error) {
      setFatal(
        error instanceof ApiError
          ? `${error.status} · ${error.message}`
          : "não consegui falar com o agente. a stack está de pé? `make up`",
      );
    }
  }, [refreshThreads]);

  // Open one on first load, and only once: StrictMode double-invokes effects in
  // development, and without the guard every reload creates two threads.
  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    void openThread();
  }, [openThread]);

  const resume = useCallback(
    async (threadId: string) => {
      try {
        const past = await fetchThread(threadId);
        setThread((current) =>
          current ? { ...current, thread_id: threadId } : current,
        );
        setEntries(
          past.messages.map((message) =>
            message.role === "user"
              ? { kind: "user", id: nextId(), text: message.text }
              : {
                  kind: "agent",
                  id: nextId(),
                  text: message.text,
                  // A reopened conversation has no rail. The frames were live
                  // measurements and are not stored — showing an empty rail is
                  // honest; reconstructing one would be invention.
                  rail: [],
                  violations: [],
                  blocked: false,
                  metrics: {
                    ms: null,
                    tokensIn: null,
                    tokensOut: null,
                    costUsd: null,
                    traceUrl: null,
                  },
                  fresh: false,
                },
          ),
        );
        // Picking a conversation on a phone means you want to read it, not
        // keep looking at the list that is covering it.
        setOverlayOpen(false);
      } catch {
        setFatal("não consegui abrir essa conversa.");
      }
    },
    [],
  );

  const forgetThread = useCallback(
    async (threadId: string) => {
      setThreads((current) => current.filter((t) => t.thread_id !== threadId));
      try {
        await deleteThread(threadId);
      } finally {
        void refreshThreads();
      }
    },
    [refreshThreads],
  );

  const send = useCallback(
    async (message: string) => {
      if (!thread) return;
      const threadId = thread.thread_id;
      setBusy(true);
      setEntries((prev) => [...prev, { kind: "user", id: nextId(), text: message }]);

      // Local, not state: this loop spans many renders and a closed-over state
      // value would be stale from the second frame onward.
      let acc: TurnAccumulator = EMPTY_ACCUMULATOR;
      let turn: TurnEvent | null = null;
      let traceUrl: string | null = null;
      let failure: { status: number; detail: string } | null = null;

      try {
        for await (const frame of streamTurn(threadId, message)) {
          if (frame.event === "stage") {
            acc = accumulateStage(acc, frame.data);
            // Re-rendered per frame so the rail fills in as the turn runs.
            // That live fill is the demo; batching it to the end would make a
            // stream indistinguishable from a slow request.
            setEntries((prev) => withLiveRail(prev, acc));
          } else if (frame.event === "turn") {
            turn = frame.data;
          } else if (frame.event === "error") {
            failure = frame.data;
          } else {
            traceUrl = frame.data.trace_url;
          }
        }
      } catch (error) {
        failure =
          error instanceof ApiError
            ? { status: error.status, detail: error.message }
            : { status: 0, detail: "conexão interrompida" };
      }

      setEntries((prev) => {
        const withoutLive = prev.filter((entry) => entry.id !== LIVE_ID);
        if (failure) {
          return [
            ...withoutLive,
            {
              kind: "error",
              id: nextId(),
              status: failure.status,
              detail: failure.detail,
              traceUrl,
            },
          ];
        }
        return [
          ...withoutLive,
          {
            kind: "agent",
            id: nextId(),
            text: turn?.text ?? "",
            rail: acc.rail,
            violations: acc.violations,
            blocked: acc.blocked,
            metrics: metricsFrom(acc, turn, traceUrl),
            fresh: true,
          },
        ];
      });
      setBusy(false);
      void refreshThreads();
    },
    [thread, refreshThreads],
  );

  const showSidebar = !narrow || sidebarOpen;

  return (
    <div className={`shell${showSidebar ? " shell--with-sidebar" : ""}`}>
      {showSidebar ? (
        <div className="shell__sidebar" id="sidebar">
          <Sidebar
            threads={threads}
            durable={durable}
            activeId={thread?.thread_id ?? null}
            onSelect={(id) => void resume(id)}
            onNew={() => {
              setOverlayOpen(false);
              void openThread();
            }}
            onDelete={(id) => void forgetThread(id)}
          />
        </div>
      ) : null}

      <div className="shell__main">
        <Header
          agent={thread?.agent ?? null}
          guardrails={thread?.guardrails ?? null}
          threadId={thread?.thread_id ?? null}
          theme={theme}
          onTheme={() => setTheme(NEXT_THEME[theme] ?? "")}
          onToggleSidebar={toggleSidebar}
          showSidebarToggle={narrow}
          sidebarOpen={sidebarOpen}
        />

        {fatal ? (
          <p className="fatal" role="alert">
            {fatal}
          </p>
        ) : null}

        <Transcript entries={entries} />
        <Composer busy={busy} onSend={(message) => void send(message)} />
      </div>
    </div>
  );
}

/** The id of the in-flight entry, replaced by the real one when the turn lands. */
const LIVE_ID = "__live__";

/**
 * Show the rail of a turn that is still running.
 *
 * A placeholder entry rather than a separate component, so the rail appears
 * exactly where the answer will — the wait is visibly the pipeline working in
 * the answer's own place, rather than a spinner somewhere else.
 */
function withLiveRail(entries: Entry[], acc: TurnAccumulator): Entry[] {
  const live: Entry = {
    kind: "agent",
    id: LIVE_ID,
    text: "",
    rail: acc.rail,
    violations: [],
    blocked: false,
    metrics: {
      ms: null,
      tokensIn: null,
      tokensOut: null,
      costUsd: null,
      traceUrl: null,
    },
    fresh: false,
  };
  const withoutLive = entries.filter((entry) => entry.id !== LIVE_ID);
  return [...withoutLive, live];
}
