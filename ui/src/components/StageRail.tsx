/**
 * The stage rail — the second device, and the honest half of the animation.
 *
 * Six cells, one per pipeline stage, each dim until its `stage` SSE frame lands
 * and then filled with the latency the backend measured. Unlike the typewriter,
 * nothing here is decorative: every filled cell is a frame that arrived and
 * every number under it came off the wire.
 *
 * A skipped stage renders **struck through rather than hidden**. `judge` only
 * runs on `confirm_terms`, so on five steps out of six it is absent — and an
 * absent cell would let that absence read as a pass. Which path the turn took
 * is the information the rail exists to carry, so the skip has to be visible as
 * a skip.
 */

import { STAGE_LABELS } from "../format";
import type { RailState } from "../state";
import type { StageName } from "../types";

const MARKS: Record<string, string> = {
  pending: "▫",
  running: "▪",
  done: "▪",
  skipped: "▫",
};

export function StageRail({ rail }: { rail: RailState }) {
  return (
    <div className="rail" aria-label="Etapas do turno em andamento">
      {STAGE_LABELS.map(({ stage, label }) => {
        const cell = rail[stage as StageName];
        return (
          <div key={stage} className={`rail__cell rail__cell--${cell.status}`}>
            <span className="rail__label">
              <span className="rail__mark" aria-hidden="true">
                {MARKS[cell.status]}
              </span>
              <span className="rail__name">{label}</span>
            </span>
            <span className="rail__ms">
              {cell.status === "skipped"
                ? "pulado"
                : cell.ms !== null
                  ? `${cell.ms} ms`
                  : " "}
            </span>
          </div>
        );
      })}
    </div>
  );
}
