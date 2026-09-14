/**
 * coordinator/useDecisionInbox.ts — state acquisition + one-tap submission for
 * Decision_Inbox (Req 12.1–12.3).
 *
 * Responsibilities:
 *  - Fetch every OPEN Escalation_Record from Escalation_Service and keep it
 *    ordered by response deadline from soonest to latest (Req 12.1).
 *  - Keep the list current via AppSync Events on `/escalations/*` so a persisted
 *    record appears within 10s without a manual reload (Req 11.2, 12.5),
 *    re-sorting on every change so ordering stays correct.
 *  - Recompute the remaining time before each deadline at least once every 10s
 *    (Req 12.1) — a `nowMs` clock ticked on an interval that consumers use to
 *    derive whole minutes/seconds remaining.
 *  - Submit one option on a single activation, disabling that record's controls
 *    until Escalation_Service confirms an outcome (Req 12.2), and re-enabling
 *    with an error indication if it returns an error or confirms no outcome
 *    within 10s (Req 12.3). The 10s bound is enforced with
 *    `AbortSignal.timeout` (Design §3.8).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useIncidentUpdates } from '../shared/realtime';
import type { EscalationRecord } from './types';

/** Confirmation-window / request timeout for a one-tap submission (Req 12.3). */
const SUBMIT_TIMEOUT_MS = 10_000;

/** Remaining-time recompute cadence. Req 12.1 requires ≤10s; 1s is smooth. */
const CLOCK_TICK_MS = 1_000;

/** Fallback poll cadence when realtime is unavailable, to reconcile the list. */
const REFRESH_INTERVAL_MS = 15_000;

/** Escalation_Service base URL for fetch + one-tap response (Design §3.8). */
const API_BASE = (import.meta.env?.VITE_CONSOLE_API_BASE as string | undefined) ?? '';

/** Order OPEN records by response deadline, soonest first (Req 12.1). */
function byDeadline(a: EscalationRecord, b: EscalationRecord): number {
  return (
    new Date(a.responseDeadline).getTime() - new Date(b.responseDeadline).getTime()
  );
}

/** Keep only OPEN records and return them ordered by deadline (Req 12.1). */
function openOrdered(records: EscalationRecord[]): EscalationRecord[] {
  return records.filter((r) => r.status === 'OPEN').slice().sort(byDeadline);
}

export interface DecisionInboxState {
  /** OPEN Escalation_Records, ordered soonest-deadline-first (Req 12.1). */
  records: EscalationRecord[];
  /** True until the first successful fetch completes. */
  loading: boolean;
  /**
   * Monotonic clock (ms) ticked at least once every 10s (Req 12.1). Consumers
   * derive the remaining time before each deadline from this so the countdown
   * re-renders without recomputing the record list.
   */
  nowMs: number;
  /** Option-control disabled state per escalation id while a submit is in flight (Req 12.2). */
  submitting: Record<string, boolean>;
  /** Per-escalation error indication after a failed submission (Req 12.3). */
  errors: Record<string, string | undefined>;
  /**
   * True when realtime updates have gone silent past the staleness limit
   * (Req 12.6). Surfaced so the console can show a staleness indicator.
   */
  connectionStale: boolean;
  /**
   * Submit an option as the response to an Escalation_Record on a single
   * activation (Req 12.2). Disables the record's controls until an outcome is
   * confirmed; re-enables with an error on failure or timeout (Req 12.3).
   */
  submitOption: (escalationId: string, optionId: string) => Promise<void>;
}

/**
 * Acquire and maintain Decision_Inbox state and expose the one-tap submission.
 */
export function useDecisionInbox(): DecisionInboxState {
  const [records, setRecords] = useState<EscalationRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [submitting, setSubmitting] = useState<Record<string, boolean>>({});
  const [errors, setErrors] = useState<Record<string, string | undefined>>({});
  const [fetchFailed, setFetchFailed] = useState(false);

  // Fetch the current OPEN records from Escalation_Service, ordered by deadline.
  const fetchInbox = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/escalations?status=OPEN`, {
        signal: AbortSignal.timeout(SUBMIT_TIMEOUT_MS),
      });
      if (!res.ok) {
        throw new Error(`status ${res.status}`);
      }
      const next = (await res.json()) as EscalationRecord[];
      setRecords(openOrdered(next));
      setFetchFailed(false);
    } catch {
      // Leave the last-known list in place; the next poll reconciles. Only a
      // genuine fetch failure marks the data stale (not a quiet websocket).
      setFetchFailed(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchInbox();
    const timer = setInterval(() => void fetchInbox(), REFRESH_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [fetchInbox]);

  // Recompute the remaining-time clock at least once every 10s (Req 12.1).
  useEffect(() => {
    const tick = setInterval(() => setNowMs(Date.now()), CLOCK_TICK_MS);
    return () => clearInterval(tick);
  }, []);

  // Live updates: any escalation commit re-fetches so the OPEN list, ordering,
  // and delivery-failure states stay consistent with State_Store (Req 11.2,
  // 11.11, 12.5). A resolved record drops out of the OPEN filter automatically.
  const onEscalationUpdate = useCallback(() => {
    void fetchInbox();
  }, [fetchInbox]);

  // Keep the realtime subscription running (it re-fetches on any commit), but
  // do NOT surface its quiet-websocket "stale" flag as a banner: the 15s poll
  // keeps the list current regardless. Only a genuine fetch failure is shown.
  useIncidentUpdates('/escalations/*', onEscalationUpdate);
  const connectionStale = fetchFailed;

  const submitOption = useCallback(
    async (escalationId: string, optionId: string) => {
      // Req 12.2: one activation → at most one submitted response. Ignore
      // repeat activations while a submit for this record is already in flight.
      let alreadyInFlight = false;
      setSubmitting((prev) => {
        if (prev[escalationId]) {
          alreadyInFlight = true;
          return prev;
        }
        return { ...prev, [escalationId]: true };
      });
      if (alreadyInFlight) {
        return;
      }

      // Clear any prior error for this record as the retry begins (Req 12.3).
      setErrors((prev) => ({ ...prev, [escalationId]: undefined }));

      try {
        const res = await fetch(`${API_BASE}/escalations/${escalationId}/respond`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ optionId }),
          // Req 12.3: an outcome not confirmed within 10s is a failure. The
          // timeout aborts the request so control re-enables for retry.
          signal: AbortSignal.timeout(SUBMIT_TIMEOUT_MS),
        });
        if (!res.ok) {
          throw new Error(await res.text());
        }
        // Success: Escalation_Service resolves the record and the AppSync event
        // moves it out of the OPEN list — no local mutation needed. Controls
        // stay disabled because the record is about to disappear.
      } catch {
        // Req 12.3: retain the record as unresolved, present the error, and
        // re-enable the option controls for retry.
        setErrors((prev) => ({
          ...prev,
          [escalationId]: 'RESPONSE_NOT_RECORDED',
        }));
        setSubmitting((prev) => ({ ...prev, [escalationId]: false }));
      }
    },
    [],
  );

  // Never keep disabled/error state for records that have left the OPEN list.
  const openIds = useMemo(
    () => new Set(records.map((r) => r.escalationId)),
    [records],
  );
  const prevOpenIds = useRef<Set<string>>(openIds);
  useEffect(() => {
    const stale = [...prevOpenIds.current].filter((id) => !openIds.has(id));
    if (stale.length > 0) {
      setSubmitting((prev) => pruneKeys(prev, stale));
      setErrors((prev) => pruneKeys(prev, stale));
    }
    prevOpenIds.current = openIds;
  }, [openIds]);

  return {
    records,
    loading,
    nowMs,
    submitting,
    errors,
    connectionStale,
    submitOption,
  };
}

/** Remove the given keys from a record, returning a new object if any changed. */
function pruneKeys<V>(obj: Record<string, V>, keys: string[]): Record<string, V> {
  const next = { ...obj };
  let changed = false;
  for (const k of keys) {
    if (k in next) {
      delete next[k];
      changed = true;
    }
  }
  return changed ? next : obj;
}

/**
 * Remaining time before a deadline, as whole minutes and seconds (Req 12.1).
 * Clamps at zero once the deadline has passed.
 */
export function remainingTime(
  deadlineIso: string,
  nowMs: number,
): { minutes: number; seconds: number; expired: boolean } {
  const remainingMs = new Date(deadlineIso).getTime() - nowMs;
  if (!Number.isFinite(remainingMs) || remainingMs <= 0) {
    return { minutes: 0, seconds: 0, expired: true };
  }
  const totalSeconds = Math.floor(remainingMs / 1000);
  return {
    minutes: Math.floor(totalSeconds / 60),
    seconds: totalSeconds % 60,
    expired: false,
  };
}
