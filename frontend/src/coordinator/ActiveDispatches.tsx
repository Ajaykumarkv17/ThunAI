/**
 * ActiveDispatches.tsx — coordinator's live view of dispatched assignments.
 *
 * Shows every responder assignment and its current lifecycle stage, so a
 * coordinator can watch a dispatch they approved progress to completion. Read-
 * only: the coordinator observes, the responder acts.
 */

import { useActiveDispatches, type Dispatch } from './useActiveDispatches';

/** Human label + progress rank for each assignment state. */
const STATE_META: Record<string, { label: string; step: number }> = {
  AWAITING_ACK: { label: 'Awaiting responder', step: 0 },
  ACCEPTED: { label: 'Accepted', step: 1 },
  EN_ROUTE: { label: 'On the way', step: 2 },
  ON_SCENE: { label: 'On scene', step: 3 },
  VERIFICATION: { label: 'Completed', step: 4 },
  DECLINED: { label: 'Declined — reassigning', step: -1 },
};

const TOTAL_STEPS = 4;

function stateClass(state: string): string {
  if (state === 'VERIFICATION') return 'dispatch-pill dispatch-pill--done';
  if (state === 'DECLINED') return 'dispatch-pill dispatch-pill--declined';
  if (state === 'AWAITING_ACK') return 'dispatch-pill dispatch-pill--waiting';
  return 'dispatch-pill dispatch-pill--active';
}

function DispatchRow({ d }: { d: Dispatch }) {
  const meta = STATE_META[d.state] ?? { label: d.state, step: 0 };
  const pct = meta.step < 0 ? 0 : Math.round((meta.step / TOTAL_STEPS) * 100);
  return (
    <li className="dispatch-row">
      <div className="dispatch-row__head">
        <span className="dispatch-row__title">
          Rescue <span className="dispatch-row__ref">#{d.requestId}</span>
        </span>
        <span className={stateClass(d.state)}>{meta.label}</span>
      </div>
      <p className="dispatch-row__meta">
        {d.locationReference} · {d.occupantCount} occupant(s) · {d.equipmentRequirement}
      </p>
      <div
        className="dispatch-row__progress"
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`Dispatch progress: ${meta.label}`}
      >
        <span className="dispatch-row__bar" style={{ width: `${pct}%` }} />
      </div>
    </li>
  );
}

export function ActiveDispatches() {
  const { dispatches, loading } = useActiveDispatches();

  if (loading && dispatches.length === 0) return null;
  if (dispatches.length === 0) return null;

  return (
    <section className="active-dispatches" aria-labelledby="dispatches-heading">
      <h2 id="dispatches-heading" className="active-dispatches__title">
        Active dispatches
      </h2>
      <ol className="active-dispatches__list">
        {dispatches.map((d) => (
          <DispatchRow key={d.assignmentId} d={d} />
        ))}
      </ol>
    </section>
  );
}
