/**
 * resident/useResidentStatus.ts — public state acquisition for Resident_Status_Page.
 *
 * Fetches the current aggregate public status from State_Store over the public
 * read endpoint, then keeps it current two ways:
 *  - AppSync Events wildcard subscriptions on `/incidents/*` and `/shelters/*`,
 *    so a severity-band / affected-area / shelter-capacity change is reflected
 *    within 15s without a manual reload (Req 14.3), via `shared/realtime.ts`.
 *  - A polling refresh at the configured interval, which also provides the
 *    retrieval-failure fallback: if State_Store cannot be reached, the last
 *    retrieved values are kept, the retrieval timestamp is surfaced, and
 *    retrieval attempts continue at the configured interval (Req 14.9).
 *
 * All state exposed here is aggregate / non-identifying by construction — see
 * `types.ts` (Req 14.5). No resident name, contact, or household location is
 * ever fetched or stored.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useIncidentUpdates } from '../shared/realtime';
import type { ResidentStatus } from './types';

/** Configured public refresh interval (ms). Bounds public fan-out cost. */
const REFRESH_INTERVAL_MS = 10_000;

/** Public read endpoint base for the aggregate status snapshot (Req 14.1). */
const STATUS_API_BASE = (import.meta.env?.VITE_STATUS_API_BASE as string | undefined) ?? '';

export interface ResidentStatusState {
  /** The most recently retrieved public status, or `null` before first load. */
  status: ResidentStatus | null;
  /** True until the first successful retrieval completes. */
  loading: boolean;
  /**
   * True when the last retrieval attempt failed and we are showing the most
   * recently retrieved values (Req 14.9).
   */
  retrievalFailed: boolean;
  /** ISO timestamp of the last successful retrieval (Req 14.9). */
  lastRetrievedAt: string | null;
  /**
   * True when no realtime update has been confirmed within the staleness limit
   * (Req 14.3 / 14.9). Distinct from `status.readingStale`, which is about the
   * age of the sensor reading itself (Req 14.8).
   */
  connectionStale: boolean;
}

/**
 * Acquire and maintain the public resident status. Combines an initial + polled
 * fetch (with retrieval-failure fallback) and live AppSync Events updates.
 */
export function useResidentStatus(): ResidentStatusState {
  const [status, setStatus] = useState<ResidentStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [retrievalFailed, setRetrievalFailed] = useState(false);
  const [lastRetrievedAt, setLastRetrievedAt] = useState<string | null>(null);

  // Keep the latest known status in a ref so realtime merges never race the
  // polling fetch's state setter.
  const latestStatus = useRef<ResidentStatus | null>(null);
  latestStatus.current = status;

  const fetchStatus = useCallback(async () => {
    try {
      const res = await fetch(`${STATUS_API_BASE}/public/status`, {
        signal: AbortSignal.timeout(10_000),
      });
      if (!res.ok) {
        throw new Error(`status ${res.status}`);
      }
      const next = (await res.json()) as ResidentStatus;
      setStatus(next);
      setLastRetrievedAt(new Date().toISOString());
      setRetrievalFailed(false);
    } catch {
      // Req 14.9: keep the most recently retrieved values, mark the retrieval
      // as failed, and keep trying at the configured interval. Never clear the
      // last-good status on failure.
      setRetrievalFailed(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchStatus();
    const timer = setInterval(() => void fetchStatus(), REFRESH_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [fetchStatus]);

  // Live updates: any incident/shelter commit triggers a re-fetch so the
  // reflected change stays aggregate-only and consistent with State_Store.
  const onUpdate = useCallback(() => {
    void fetchStatus();
  }, [fetchStatus]);

  const incidentRealtime = useIncidentUpdates('/incidents/*', onUpdate);
  const shelterRealtime = useIncidentUpdates('/shelters/*', onUpdate);

  return {
    status,
    loading,
    retrievalFailed,
    lastRetrievedAt,
    connectionStale: incidentRealtime.stale || shelterRealtime.stale,
  };
}
