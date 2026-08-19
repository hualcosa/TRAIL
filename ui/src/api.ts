/**
 * HTTP surface of the agent service, plus a hand-written SSE reader.
 *
 * Every path here is relative. The browser talks to the same origin it was
 * served from and something in front of it — Vite's dev proxy, or nginx in
 * compose — forwards `/api/*` to the agent. That is why this file contains no
 * base URL, no credentials and no CORS handling: there is no cross-origin
 * request to configure. The one absolute URL in the whole UI is `trace_url`,
 * which the backend builds because only the backend knows the browser-reachable
 * address of Jaeger.
 */

import type {
  DemoCases,
  StartCallResponse,
  TurnStreamEvent,
  AccountProfile,
  CallRecord,
} from "./types";

const API = "/api";

/**
 * Raised when the service answers with a non-2xx status.
 *
 * Carries the status and the server's own `detail` string, because the UI is
 * required to say what failed rather than that something failed, and FastAPI's
 * detail is the only sentence that knows which of extraction, compliance or
 * persistence broke.
 */
export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
  }
}

async function readError(response: Response): Promise<ApiError> {
  // FastAPI answers `{"detail": "..."}` on HTTPException and a list of dicts on
  // a 422. Neither is guaranteed — an nginx 502 is HTML — so every access here
  // is defensive and falls back to the status line.
  let detail = `${response.status} ${response.statusText}`;
  try {
    const body: unknown = await response.json();
    if (body && typeof body === "object" && "detail" in body) {
      const raw = (body as { detail: unknown }).detail;
      detail = typeof raw === "string" ? raw : JSON.stringify(raw);
    }
  } catch {
    // Body was not JSON. The status line stands.
  }
  return new ApiError(response.status, detail);
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw await readError(response);
  return (await response.json()) as T;
}

export async function fetchDemoCases(): Promise<DemoCases> {
  const response = await fetch(`${API}/demo/cases`);
  if (!response.ok) throw await readError(response);
  return (await response.json()) as DemoCases;
}

export function startCall(
  profile: AccountProfile,
  caseId: string | null,
): Promise<StartCallResponse> {
  return postJson<StartCallResponse>("/calls", {
    profile,
    case_id: caseId,
  });
}

export function markUnreachable(
  callId: string,
  reason: string,
): Promise<CallRecord> {
  return postJson<CallRecord>(`/calls/${callId}/unreachable`, {
    call_id: callId,
    reason,
  });
}

// ---------------------------------------------------------------------------
// SSE
// ---------------------------------------------------------------------------

/**
 * Split the leading complete frame off an SSE buffer.
 *
 * Returns `[frame, rest]` or null when no terminator is present yet. SSE frames
 * end at a blank line, and the spec permits either LF or CRLF line endings, so
 * both terminators are searched and the earlier one wins. Getting this wrong in
 * the CRLF direction is invisible against a Python backend that emits LF and
 * then fails against a proxy that rewrites line endings.
 */
function takeFrame(buffer: string): [string, string] | null {
  const lf = buffer.indexOf("\n\n");
  const crlf = buffer.indexOf("\r\n\r\n");
  if (lf === -1 && crlf === -1) return null;
  if (crlf !== -1 && (lf === -1 || crlf < lf)) {
    return [buffer.slice(0, crlf), buffer.slice(crlf + 4)];
  }
  return [buffer.slice(0, lf), buffer.slice(lf + 2)];
}

/** Parse one frame's lines into its event name and its concatenated data. */
function parseFrame(frame: string): { event: string; data: string } | null {
  let event = "message";
  const data: string[] = [];
  for (const rawLine of frame.split("\n")) {
    const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
    if (line === "" || line.startsWith(":")) continue; // blank or comment
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1); // spec: one optional space
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }
  if (data.length === 0) return null;
  return { event, data: data.join("\n") };
}

/**
 * Read an SSE response body and yield decoded frames as they land.
 *
 * Two bugs live here and both are silent until they are not:
 *
 * (a) **Frames split mid-chunk.** A `ReadableStream` chunk boundary has nothing
 *     to do with a frame boundary — one read can deliver half of `data: {"st`
 *     and the next the rest. The buffer therefore persists across reads and is
 *     only ever consumed a complete frame at a time. Parsing per chunk works
 *     perfectly on localhost, where frames usually arrive whole, and shatters
 *     the first time a real network splits a 400-byte `turn` payload.
 *
 * (b) **Multi-byte characters split mid-chunk.** `TextDecoder` is created once
 *     and called with `{stream: true}` so a UTF-8 sequence straddling a chunk
 *     boundary is held back rather than emitted as U+FFFD. Every agent
 *     utterance in this product is Portuguese, so "confirmação" carries a
 *     two-byte ç that will eventually land on a boundary.
 */
export async function* readSse(
  response: Response,
): AsyncGenerator<{ event: string; data: string }> {
  const body = response.body;
  if (!body) {
    throw new ApiError(
      response.status,
      "A resposta do serviço veio sem corpo. O stream não pôde ser lido.",
    );
  }
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      for (;;) {
        const split = takeFrame(buffer);
        if (!split) break;
        buffer = split[1];
        const parsed = parseFrame(split[0]);
        if (parsed) yield parsed;
      }
    }
    // Flush the decoder, then honour a trailing frame that the server closed
    // without its blank line. Servers should not do that; some proxies do.
    buffer += decoder.decode();
    const tail = parseFrame(buffer);
    if (tail) yield tail;
  } finally {
    reader.releaseLock();
  }
}

/**
 * Submit one customer turn and yield the pipeline as it happens.
 *
 * The response is a stream rather than a payload, so `fetch` resolving only
 * means the headers arrived — the turn itself is still running. Anything that
 * fails before the stream opens (the service down, the proxy unreachable) comes
 * back as a thrown `ApiError`; anything that fails inside the turn arrives as an
 * `error` frame and is followed by a `trace` frame, so a failed turn is still
 * traceable in Jaeger. Callers must handle both.
 */
export async function* streamTurn(
  callId: string,
  customerUtterance: string,
): AsyncGenerator<TurnStreamEvent> {
  const response = await fetch(`${API}/calls/${callId}/turns/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({
      call_id: callId,
      customer_utterance: customerUtterance,
    }),
  });
  if (!response.ok) throw await readError(response);

  for await (const frame of readSse(response)) {
    let data: unknown;
    try {
      data = JSON.parse(frame.data);
    } catch {
      // A frame we cannot parse is dropped rather than fatal: the stream still
      // has a `trace` frame to deliver and the call is still open.
      continue;
    }
    switch (frame.event) {
      case "stage":
      case "turn":
      case "trace":
      case "error":
        yield { event: frame.event, data } as TurnStreamEvent;
        break;
      default:
        break; // unknown event name — forward compatibility, ignore it
    }
  }
}
