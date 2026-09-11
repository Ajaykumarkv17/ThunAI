/**
 * coordinator/useOpenIncidents.ts — state acquisition for the open-incident
 * list + shelter availability view (Req 12.4).
 *
 * Responsibilities:
 *  - Fetch every open incident from the console API and keep it ordered by
 *    severity band descending (EVACUATE > WARNING > WATCH > NORMAL), then by
 *    most recent update first (Req 12.4). Ordering mirrors
 *    `surface.queries.order_open_incidents`, whose authoritative band ordering
 *    is `SEVERITY_BANDS_ASCENDING`.
 *  - Keep the list live via AppSync Events on `/incidents/*` and `/shelters/*`
 *    so a persisted change is reflected without a manual reload (Req 12.5),
 *    re-fetching + re-sorting on every change so ordering stays correct.
 *  - Surface a staleness indicator when realtime updates go silent past the
 *    limit (Req 12.6), consistent with the Decision_Inbox view.
 *
 * This view is read-only: it presents open-request/assigned counts per incident
 * and unoccupied/total places per shelter, with no mutating controls.
 */

import { useCallback, useEffect, useState } from 'react';
import { useIncidentUpdates } from '../shared/realtime';
import { SEVERITY_BANDS_ASCENDING, type IncidentSummary } from './types';

/** Request timeout for a list fetch (Design §3.8, matching the console convention). */
const FETCH_TIMEOUT_MS = 10_000;

/** Fallback poll cadence when realtime is unavailable, to reconcile the list. */
const REFRESH_INTERVAL_MS = 15_000;

/** Console API base URL for the open-incident list (Design §3.8). */
const API_BASE = (import.meta.env?.VITE_CONSOLE_API_BASE as string | undefined) ?? '';

/** Rank of a severity band; higher is more severe. Unknown bands sort last. */
function severityRank(band: string): number {
  const rank = SEVERITY_BANDS_ASCENDING.indexOf(band as IncidentSummary['severityBand']);
  return rank === -1 ? -1 : rank;
}

/**
 * Order incidents by severity band descending, then most recently updated
 * first (Req 12.4). Mirrors `surface.queries.order_open_incidents` so the
 * client ordering can never drift from the server's GSI-backed ordering.
 */
export function orderOpenIncidents(incidents: IncidentSummary[]): IncidentSummary[] {
  return incidents
    .slice()
    .sort((a, b) => {
      const bandDelta = severityRank(b.severityBand) - severityRank(a.severityBand);
      if (bandDelta !== 0) {
        return bandDelta;
      }
      // Recency tie-break: most recently updated first.
      return new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime();
    });
}

export interface OpenIncidentsState {
  /** Open incidents, ordered severity-descending then most-recent-first (Req 12.4). */
  incidents: IncidentSummary[];
  /** True until the first successful fetch completes. */
  loading: boolean;
  /**
   * True when realtime updates have gone silent past the staleness limit
   * (Req 12.6). Surfaced so the console can show a staleness indicator.
   */
  connectionStale: boolean;
}

/**
 * Acquire and maintain the open-incident list + shelter availability, kept live
 * via AppSync Events and reconciled by a fallback poll.
 */
export function useOpenIncidents(): OpenIncidentsState {
  const [incidents, setIncidents] = useState<IncidentSummary[]>([]);
  const [loading, setLoading] = useState(true);

  const fetchIncidents = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/incidents?status=OPEN`, {
        signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
      });
      if (!res.ok) {
        throw new Error(`status ${res.status}`);
      }
      const next = (await res.json()) as IncidentSummary[];
      setIncidents(orderOpenIncidents(next));
    } catch {
      // Leave the last-known list in place; realtime + the next poll reconcile.
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchIncidents();
    const timer = setInterval(() => void fetchIncidents(), REFRESH_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [fetchIncidents]);

  // Live updates: any incident or shelter commit re-fetches so the list, its
  // ordering, the per-incident counts, and shelter availability stay consistent
  // with State_Store (Req 12.5).
  const onUpdate = useCallback(() => {
    void fetchIncidents();
  }, [fetchIncidents]);

  const { stale: incidentsStale } = useIncidentUpdates('/incidents/*', onUpdate);
  const { stale: sheltersStale } = useIncidentUpdates('/shelters/*', onUpdate);

  return {
    incidents,
    loading,
    connectionStale: incidentsStale || sheltersStale,
  };
}
