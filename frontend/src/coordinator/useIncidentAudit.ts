/**
 * coordinator/useIncidentAudit.ts — state acquisition for the per-incident
 * audit trail view (Req 12.7).
 *
 * Responsibilities:
 *  - Fetch every `Audit_Ledger` entry for the selected incident from the
 *    console API and keep it ordered by entry timestamp from oldest to newest
 *    (Req 12.7). Ordering mirrors `surface.queries.order_incident_audit`, whose
 *    persisted order is the `thunai-audit` GSI's `gsi1sk = "{timestamp}"`
 *    (ties broken by `entryId` ascending, matching the table's
 *    `sk = "{timestamp}#{entryId}"` composition).
 *  - Keep the trail live via AppSync Events on `/incidents/{incidentId}` so a
 *    newly-appended entry is reflected without a manual reload (Req 12.5),
 *    re-fetching + re-sorting on every change so the append-only trail always
 *    ends with the latest entry.
 *  - Surface a staleness indicator when realtime updates go silent past the
 *    limit (Req 12.6), consistent with the other console views.
 *
 * This view is read-only: audit entries are append-only by construction
 * (`memory.audit_ledger`), and their `inputs` are already redacted before
 * persistence — this hook fetches and orders them, it never mutates or
 * re-redacts them.
 */

import { useCallback, useEffect, useState } from 'react';
import { useIncidentUpdates } from '../shared/realtime';
import type { AuditView } from './types';

/** Request timeout for an audit-trail fetch (Design §3.8, console convention). */
const FETCH_TIMEOUT_MS = 10_000;

/** Fallback poll cadence when realtime is unavailable, to reconcile the trail. */
const REFRESH_INTERVAL_MS = 15_000;

/** Console API base URL for the per-incident audit trail (Design §3.8, §4.4). */
const API_BASE = (import.meta.env?.VITE_CONSOLE_API_BASE as string | undefined) ?? '';

/** Text-safe accessor mirroring `surface.queries._text` for total ordering. */
function text(value: unknown): string {
  return value === null || value === undefined ? '' : String(value);
}

/**
 * Order audit entries by entry timestamp, oldest first (Req 12.7). Ties on
 * `timestamp` are broken by `entryId` ascending — the same
 * `sk = "{timestamp}#{entryId}"` composition `thunai-audit` uses — so the
 * in-memory order matches the persisted order exactly and can never drift from
 * `surface.queries.order_incident_audit`.
 */
export function orderIncidentAudit(entries: AuditView[]): AuditView[] {
  return entries
    .slice()
    .sort((a, b) => {
      const tsDelta = text(a.timestamp).localeCompare(text(b.timestamp));
      if (tsDelta !== 0) {
        return tsDelta;
      }
      return text(a.entryId).localeCompare(text(b.entryId));
    });
}

export interface IncidentAuditState {
  /** The incident's audit entries, oldest→newest (Req 12.7). */
  entries: AuditView[];
  /** True until the first successful fetch completes. */
  loading: boolean;
  /**
   * True when realtime updates have gone silent past the staleness limit
   * (Req 12.6). Surfaced so the console can show a staleness indicator.
   */
  connectionStale: boolean;
}

/**
 * Acquire and maintain the per-incident audit trail, kept live via AppSync
 * Events and reconciled by a fallback poll.
 *
 * @param incidentId the incident whose audit entries to fetch; when empty no
 *                   fetch is issued (the trail renders its empty state).
 */
export function useIncidentAudit(incidentId: string | undefined): IncidentAuditState {
  const [entries, setEntries] = useState<AuditView[]>([]);
  const [loading, setLoading] = useState(true);

  const fetchAudit = useCallback(async () => {
    if (!incidentId) {
      setEntries([]);
      setLoading(false);
      return;
    }
    try {
      const res = await fetch(
        `${API_BASE}/incidents/${encodeURIComponent(incidentId)}/audit`,
        { signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) },
      );
      if (!res.ok) {
        throw new Error(`status ${res.status}`);
      }
      const next = (await res.json()) as AuditView[];
      setEntries(orderIncidentAudit(next));
    } catch {
      // Leave the last-known trail in place; realtime + the next poll reconcile.
    } finally {
      setLoading(false);
    }
  }, [incidentId]);

  useEffect(() => {
    setLoading(true);
    void fetchAudit();
    const timer = setInterval(() => void fetchAudit(), REFRESH_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [fetchAudit]);

  // Live updates: any commit on this incident's channel re-fetches so a newly
  // appended entry is reflected and the oldest→newest ordering stays correct
  // (Req 12.5). Subscribe to the specific incident channel, not the wildcard,
  // so the trail only reacts to its own incident.
  const onUpdate = useCallback(() => {
    void fetchAudit();
  }, [fetchAudit]);

  const channel = incidentId ? `/incidents/${incidentId}` : '/incidents/*';
  const { stale: connectionStale } = useIncidentUpdates(channel, onUpdate);

  return {
    entries,
    loading,
    connectionStale,
  };
}
