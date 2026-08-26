/**
 * The transcript column.
 *
 * `role="log"` with `aria-live="polite"` so a screen reader announces each new
 * utterance as it lands without interrupting whatever is being read. The
 * typewriter reveal is kept out of that announcement by `Utterance` itself —
 * the animated span is `aria-hidden` and a complete copy sits beside it — so
 * the live region fires once per turn rather than once per character.
 *
 * Autoscroll follows the tail only when the reader is already at the tail. A
 * transcript that yanks itself down while somebody is reading an earlier turn
 * is the reason reviewers stop scrolling back, and scrolling back is the whole
 * point of a demo about auditability.
 */

import { useEffect, useRef } from "react";
import { Message } from "./Message";
import type { Entry } from "../state";

const TAIL_TOLERANCE_PX = 80;

export function Transcript({ entries }: { entries: Entry[] }) {
  const scroller = useRef<HTMLDivElement | null>(null);
  const wasAtTail = useRef(true);

  useEffect(() => {
    const node = scroller.current;
    if (!node) return;
    if (wasAtTail.current) node.scrollTop = node.scrollHeight;
  }, [entries]);

  return (
    <div
      className="transcript"
      ref={scroller}
      onScroll={(event) => {
        const node = event.currentTarget;
        const distance =
          node.scrollHeight - node.scrollTop - node.clientHeight;
        wasAtTail.current = distance <= TAIL_TOLERANCE_PX;
      }}
    >
      <div
        className="transcript__flow"
        role="log"
        aria-live="polite"
        aria-relevant="additions"
        aria-label="Transcrição da conversa"
      >
        {entries.map((entry) => (
          <Message key={entry.id} entry={entry} />
        ))}
      </div>
    </div>
  );
}
