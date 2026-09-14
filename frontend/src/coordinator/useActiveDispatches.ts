/**
 * useActiveDispatches.ts — coordinator view of dispatched responder assignments.
 *
 * Polls the responder API's all-assignments endpoint so the coordinator can see
 * every dispatch they approved progress through its lifecycle (accepted →
 * en-route → on-scene → completed). Closes the loop: the coordinator approves a
 * dispatch, then watches it through to completion.
 */

import { useCallback, useEffect, useState } from 'react';

const API_BASE = (import.meta.env?.VITE_RESPONDER_API_BASE as string | undefined) ?? '';
const REFRESH_INTERVAL_MS = 8_000;

export interface Dispatch {
  assignmentId: string;
  requestId: string;
  locationReference: string;
  occupantCount: number;
  equipmentRequirement: string;
  state: string;
  acceptedAt?: string | null;
  enRouteAt?: string | null;
  onSceneAt?: string | null;
  completedAt?: string | null;
}

export interface ActiveDispatchesState {
  dispatches: Dispatch[];
  loading: boolean;
}

export function useActiveDispatches(): ActiveDispatchesState {
  const [dispatches, setDispatches] = useState<Dispatch[]>([]);
  const [loading, setLoading] = useState(true);

  const fetchDispatches = useCallback(async () => {
    if (!API_BASE) {
      setLoading(false);
      return;
    }
    try {
      const res = await fetch(`${API_BASE}/assignments`, {
        signal: AbortSignal.timeout(10_000),
      });
      if (!res.ok) throw new Error(`status ${res.status}`);
      setDispatches((await res.json()) as Dispatch[]);
    } catch {
      // Keep the last-known list; the next poll reconciles.
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchDispatches();
    const timer = setInterval(() => void fetchDispatches(), REFRESH_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [fetchDispatches]);

  return { dispatches, loading };
}
