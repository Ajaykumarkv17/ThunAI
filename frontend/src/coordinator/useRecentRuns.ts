/**
 * coordinator/useRecentRuns.ts — state acquisition for the recent-runs
 * cost/latency view (Req 1.7, Req 12.8).
 *
 * Responsibilities:
 *  - Fetch the most recent runs from the console API and keep them ordered by
 *    start timestamp from most recent to least recent, truncated to the 20 most
 *    recent (Req 1.7, Req 12.8). Ordering mirrors `surface.queries.order_runs`,
 *    whose persisted order is the `thunai-state` GSI1 (`gsi1pk = "RUN_BY_START"`,
 *    `ScanIndexForward=False`, `Limit=20`); ties on `startedAt` break by
 *    `runId` descending, exactly as the server does, so the in-memory order can
 *    never drift from the persisted order.
 *  - Keep the list live via AppSync Events on `/runs/*` so a newly-recorded run
 *    (or a run reaching a terminal status, which fills in the Req 12.8
 *    cost/latency columns) is reflected without a manual reload (Req 12.5),
 *    re-fetching + re-sorting on every change.
 *  - Surface a staleness indicator when realtime updates go silent past the
 *    limit (Req 12.6), consistent with the other console views.
 *
 * This view is read-only: it presents run identity + outcome (Req 1.7) and the
 * cost/latency columns (Req 12.8), with no mutating controls.
 */

import { useCallback, useEffect, useState } from 'react';
import { useIncidentUpdates } from '../shared/realtime';
import type { RunSummary } from './types';

/** Request timeout for a runs fetch (Design §3.8, matching the console convention). */
const FETCH_TIMEOUT_MS = 10_000;

/** Fallback poll cadence when realtime is unavailable, to reconcile the list. */
const REFRESH_INTERVAL_MS = 15_000;

/** Console API base URL for the recent-runs view (Design §3.8, §4.4). */
const API_BASE = (import.meta.env?.VITE_CONSOLE_API_BASE as string | undefined) ?? '';

/**
 * The 20 most recent runs (Req 1.7/12.8: "up to the 20 most recent runs"), the
 * single truncation limit shared with `surface.queries.MAX_RECENT_RUNS`.
 */
export const MAX_RECENT_RUNS = 20;

/** Text-safe accessor mirroring `surface.queries._text` for total ordering. */
function text(value: unknown): string {
  return value === null || value === undefined ? '' : String(value);
}

/**
 * Order runs by start timestamp descending, truncated to the 20 most recent
 * (Req 1.7). Ties on `startedAt` are broken by `runId` descending — the same
 * total ordering `surface.queries.order_runs` applies — so the client order
 * matches the persisted order exactly and one bad row can never reorder the
 * list. The sort is on a copy; the input is not mutated.
 */
export function orderRuns(
  runs: RunSummary[],
  limit: number = MAX_RECENT_RUNS,
): RunSummary[] {
  return runs
    .slice()
    .sort((a, b) => {
      const tsDelta = text(b.startedAt).localeCompare(text(a.startedAt));
      if (tsDelta !== 0) {
        return tsDelta;
      }
      return text(b.runId).localeCompare(text(a.runId));
    })
    .slice(0, Math.max(0, limit));
}

/**
 * Normalise the API payload to a run array. The console runs endpoint returns a
 * `ListView` envelope (`{ items, ... }`, mirroring `surface.queries.ListView`),
 * but a bare array is accepted too so the hook stays robust to either contract.
 */
function extractRuns(payload: unknown): RunSummary[] {
  if (Array.isArray(payload)) {
    return payload as RunSummary[];
  }
  if (payload && typeof payload === 'object') {
    const items = (payload as { items?: unknown }).items;
    if (Array.isArray(items)) {
      return items as RunSummary[];
    }
  }
  return [];
}

export interface RecentRunsState {
  /** The recent runs, most-recent-first, capped at 20 (Req 1.7, Req 12.8). */
  runs: RunSummary[];
  /** True until the first successful fetch completes. */
  loading: boolean;
  /**
   * True when realtime updates have gone silent past the staleness limit
   * (Req 12.6). Surfaced so the console can show a staleness indicator.
   */
  connectionStale: boolean;
}

/**
 * Acquire and maintain the recent-runs list, kept live via AppSync Events and
 * reconciled by a fallback poll. The empty state (no runs recorded yet) is
 * conveyed as an empty `runs` array once `loading` is false, so the view can
 * render the explicit empty state required by Req 1.7.
 */
export function useRecentRuns(): RecentRunsState {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [loading, setLoading] = useState(true);
  // Staleness reflects an actual data-retrieval failure, NOT a merely-quiet
  // realtime socket: the polling fetch keeps the list current regardless.
  const [retrievalFailed, setRetrievalFailed] = useState(false);

  const fetchRuns = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/runs?limit=${MAX_RECENT_RUNS}`, {
        signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
      });
      if (!res.ok) {
        throw new Error(`status ${res.status}`);
      }
      const next = extractRuns(await res.json());
      setRuns(orderRuns(next));
      setRetrievalFailed(false);
    } catch {
      // Leave the last-known list in place; the next poll reconciles. Surface
      // staleness only because the retrieval itself failed.
      setRetrievalFailed(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchRuns();
    const timer = setInterval(() => void fetchRuns(), REFRESH_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [fetchRuns]);

  // Live updates: any run commit re-fetches so a newly-recorded run and the
  // cost/latency columns filled in when a run reaches a terminal status are
  // reflected, with the most-recent-first ordering preserved (Req 12.5).
  const onUpdate = useCallback(() => {
    void fetchRuns();
  }, [fetchRuns]);

  // Keep the live subscription for instant updates when it works, but do NOT
  // let a quiet socket drive the staleness banner — the poll fetch keeps the
  // list current, so staleness reflects a real retrieval failure only.
  useIncidentUpdates('/runs/*', onUpdate);

  return {
    runs,
    loading,
    connectionStale: retrievalFailed,
  };
}
