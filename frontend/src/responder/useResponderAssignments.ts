/**
 * responder/useResponderAssignments.ts — state acquisition + status submission
 * for the Responder_Interface (Req 13.1–13.9).
 *
 * Responsibilities:
 *  - Resolve the authenticated responder identity and fetch *only* the
 *    assignments whose assigned-responder identifier equals that identity
 *    (Req 13.6). The fetch is scoped to the caller's own identity and every
 *    returned assignment is re-checked client-side, so an assignment belonging
 *    to another responder is never surfaced. (The server is the real boundary —
 *    the Cognito group + identity are re-checked there — but the client refuses
 *    to render foreign content either way.)
 *  - Keep the assignment live via AppSync Events on the responder's own channel
 *    so a persisted assignment / re-dispatch / verification transition is
 *    reflected within 10s without a manual reload (Req 13.1, 12.5).
 *  - Submit a status response on a single control activation, gated by the
 *    current assignment state (Req 13.2). The client refuses to submit a
 *    response the state does not permit and, if the server rejects a submitted
 *    response as impermissible, surfaces the permitted set it names (Req 13.9).
 *
 * A decline or a non-acknowledgement frees the responder and re-dispatches the
 * request server-side (Req 13.3, 13.7); the client simply submits the response
 * and lets the resulting State_Store change flow back over realtime — it never
 * models the re-dispatch itself. A completed response routes the request into
 * verification server-side (Req 13.4); again the client only submits and
 * reflects the returned state.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { getCurrentUser } from '../shared/auth';
import { useIncidentUpdates } from '../shared/realtime';
import {
  isActionPermitted,
  PERMITTED_ACTIONS,
  type Assignment,
  type AssignmentState,
  type ResponderAction,
} from './types';

/** Request timeout for a status submission / fetch (mirrors the console bound). */
const SUBMIT_TIMEOUT_MS = 10_000;

/** Fallback poll cadence when realtime is unavailable, to reconcile the list. */
const REFRESH_INTERVAL_MS = 15_000;

/** Responder API base URL for fetch + status response (Design §3.8). */
const API_BASE = (import.meta.env?.VITE_RESPONDER_API_BASE as string | undefined) ?? '';

/** A submission rejected because the current state did not permit the action (Req 13.9). */
export interface InvalidTransitionError {
  kind: 'invalid_transition';
  /** The action the responder attempted. */
  attempted: ResponderAction;
  /** The responses the server (or the client gate) says are permitted now (Req 13.9). */
  permitted: ResponderAction[];
}

/** A submission that failed for a transport / server reason other than gating. */
export interface SubmitFailedError {
  kind: 'submit_failed';
}

export type ResponderError = InvalidTransitionError | SubmitFailedError;

export interface ResponderAssignmentsState {
  /**
   * The authenticated responder's own assignments (Req 13.6). Empty once
   * `loading` is false means the responder has no current assignment.
   */
  assignments: Assignment[];
  /** True until the identity resolves and the first fetch completes. */
  loading: boolean;
  /**
   * True when the visitor is not an authenticated responder. No assignment
   * content is exposed in this state (Req 13.6).
   */
  unauthorised: boolean;
  /** Per-assignment disabled state while a submission is in flight (Req 13.2). */
  submitting: Record<string, boolean>;
  /** Per-assignment error indication after a rejected/failed submission (Req 13.9). */
  errors: Record<string, ResponderError | undefined>;
  /** True when realtime updates have gone silent past the staleness limit (Req 12.6). */
  connectionStale: boolean;
  /**
   * Submit a status response on a single activation (Req 13.2). Refuses a
   * response the current state does not permit, and reflects a server
   * invalid-transition rejection with the permitted set (Req 13.9).
   */
  submitAction: (assignmentId: string, action: ResponderAction) => Promise<void>;
}

/**
 * Acquire the authenticated responder's own assignments and expose the
 * state-gated status submission.
 */
export function useResponderAssignments(): ResponderAssignmentsState {
  const [responderId, setResponderId] = useState<string | null>(null);
  const [isResponder, setIsResponder] = useState<boolean | null>(null);
  const [assignments, setAssignments] = useState<Assignment[]>([]);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState<Record<string, boolean>>({});
  const [errors, setErrors] = useState<Record<string, ResponderError | undefined>>({});

  // Resolve the authenticated identity + responder-group membership once. The
  // interface is restricted to authenticated members of the responders group
  // (Req 13.6); a non-responder gets no assignment content.
  useEffect(() => {
    let active = true;
    (async () => {
      const user = await getCurrentUser();
      if (!active) return;
      if (!user || !user.groups.includes('responders')) {
        setIsResponder(false);
        setResponderId(null);
        setLoading(false);
        return;
      }
      setIsResponder(true);
      setResponderId(user.userId);
    })();
    return () => {
      active = false;
    };
  }, []);

  // Fetch the current assignments for *this* responder only (Req 13.6). The
  // request is scoped to the resolved identity and every returned assignment is
  // re-checked so a foreign assignment is never rendered even if one leaks back.
  const fetchAssignments = useCallback(async () => {
    if (!responderId) return;
    try {
      const res = await fetch(
        `${API_BASE}/responders/${encodeURIComponent(responderId)}/assignments`,
        { signal: AbortSignal.timeout(SUBMIT_TIMEOUT_MS) },
      );
      if (!res.ok) {
        throw new Error(`status ${res.status}`);
      }
      const next = (await res.json()) as Assignment[];
      // Own-assignments-only: keep only assignments whose assigned responder is
      // the authenticated identity (Req 13.6). Defence in depth over the scoped
      // fetch — the client refuses to present another responder's assignment.
      setAssignments(next.filter((a) => a.assignedResponderId === responderId));
    } catch {
      // Leave the last-known list in place; realtime + the next poll reconcile.
    } finally {
      setLoading(false);
    }
  }, [responderId]);

  useEffect(() => {
    if (!responderId) return;
    void fetchAssignments();
    const timer = setInterval(() => void fetchAssignments(), REFRESH_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [responderId, fetchAssignments]);

  // Live updates: a commit on this responder's own channel re-fetches so a
  // freshly-delivered assignment (Req 13.1), a re-dispatch that frees this
  // responder (Req 13.3, 13.7), or a verification transition (Req 13.4) is
  // reflected without a manual reload. Subscribing to the responder's own
  // channel (not a wildcard) keeps the surface scoped to its own identity.
  const channel = responderId
    ? `/responders/${encodeURIComponent(responderId)}/*`
    : '/responders/none';
  const onUpdate = useCallback(() => {
    void fetchAssignments();
  }, [fetchAssignments]);
  const { stale: connectionStale } = useIncidentUpdates(channel, onUpdate);

  const submitAction = useCallback(
    async (assignmentId: string, action: ResponderAction) => {
      const assignment = assignments.find((a) => a.assignmentId === assignmentId);
      if (!assignment) return;

      // Own-assignments-only enforcement at the point of mutation (Req 13.6):
      // never submit a response against an assignment that is not ours.
      if (responderId && assignment.assignedResponderId !== responderId) {
        return;
      }

      // Client-side state gate (Req 13.2, 13.9): refuse a response the current
      // state does not permit, surfacing the permitted set — matching what the
      // server would return — without a round trip.
      if (!isActionPermitted(assignment.state, action)) {
        setErrors((prev) => ({
          ...prev,
          [assignmentId]: {
            kind: 'invalid_transition',
            attempted: action,
            permitted: [...PERMITTED_FROM(assignment.state)],
          },
        }));
        return;
      }

      // One activation → at most one in-flight submission per assignment (Req 13.2).
      let alreadyInFlight = false;
      setSubmitting((prev) => {
        if (prev[assignmentId]) {
          alreadyInFlight = true;
          return prev;
        }
        return { ...prev, [assignmentId]: true };
      });
      if (alreadyInFlight) return;

      setErrors((prev) => ({ ...prev, [assignmentId]: undefined }));

      try {
        const res = await fetch(
          `${API_BASE}/responders/${encodeURIComponent(responderId ?? '')}/assignments/${encodeURIComponent(assignmentId)}/respond`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action }),
            signal: AbortSignal.timeout(SUBMIT_TIMEOUT_MS),
          },
        );
        if (res.status === 409) {
          // Server rejected the response as impermissible for the current state
          // and named the permitted set (Req 13.9). Surface it and re-enable.
          const body = (await res.json().catch(() => null)) as
            | { permitted?: ResponderAction[] }
            | null;
          setErrors((prev) => ({
            ...prev,
            [assignmentId]: {
              kind: 'invalid_transition',
              attempted: action,
              permitted: body?.permitted ?? [...PERMITTED_FROM(assignment.state)],
            },
          }));
          setSubmitting((prev) => ({ ...prev, [assignmentId]: false }));
          return;
        }
        if (!res.ok) {
          throw new Error(await res.text());
        }
        // Success: the server transitions the assignment/request state and the
        // AppSync event reflects the new state — no local mutation needed. A
        // decline/completed response drops or moves the assignment via realtime.
      } catch {
        setErrors((prev) => ({ ...prev, [assignmentId]: { kind: 'submit_failed' } }));
        setSubmitting((prev) => ({ ...prev, [assignmentId]: false }));
      }
    },
    [assignments, responderId],
  );

  // Drop disabled/error state for assignments that have left the list.
  const currentIds = useMemo(
    () => new Set(assignments.map((a) => a.assignmentId)),
    [assignments],
  );
  const prevIds = useRef<Set<string>>(currentIds);
  useEffect(() => {
    const stale = [...prevIds.current].filter((id) => !currentIds.has(id));
    if (stale.length > 0) {
      setSubmitting((prev) => pruneKeys(prev, stale));
      setErrors((prev) => pruneKeys(prev, stale));
    }
    prevIds.current = currentIds;
  }, [currentIds]);

  return {
    assignments,
    loading,
    unauthorised: isResponder === false,
    submitting,
    errors,
    connectionStale,
    submitAction,
  };
}

/** Permitted actions from a state (Req 13.2, 13.9). */
function PERMITTED_FROM(state: AssignmentState): readonly ResponderAction[] {
  return PERMITTED_ACTIONS[state];
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
