/**
 * responder/types.ts — the shape of a responder Assignment and its lifecycle
 * (Req 13.1–13.10; Design §3.8, §4.3).
 *
 * An Assignment is the responder-facing projection of a dispatched emergency
 * request: it carries exactly the fields delivered in the assignment
 * notification (Req 13.1) plus the current lifecycle state that gates which
 * status controls are reachable (Req 13.2). The lifecycle mirrors the
 * `Request_Lifecycle` state machine (Design §4.3):
 *
 *   ASSIGNED --accept--> ACCEPTED --en-route--> EN_ROUTE --on-scene--> ON_SCENE
 *   ON_SCENE --completed--> VERIFICATION            (route completion, Req 13.4)
 *   ASSIGNED --decline--> (re-dispatch, Req 13.3)   (responder freed, Req 13.3)
 *
 * The interface renders only values that come from the assignment; it never
 * generates status text of its own.
 */

/**
 * The lifecycle state of an assignment as the responder sees it. This is the
 * responder-facing view of the request state relevant to their assignment
 * (Design §4.3). `AWAITING_ACK` is the state a freshly-delivered assignment
 * sits in — the accept/decline controls are only enabled here (Req 13.2).
 */
export type AssignmentState =
  /** Delivered and awaiting the responder's accept/decline (Req 13.2). */
  | 'AWAITING_ACK'
  /** Responder accepted; the en-route control is now enabled (Req 13.2). */
  | 'ACCEPTED'
  /** Responder reported en-route; the on-scene control is now enabled (Req 13.2). */
  | 'EN_ROUTE'
  /** Responder reported on-scene; the completed control is now enabled (Req 13.2). */
  | 'ON_SCENE'
  /** Responder completed; request routed into verification (Req 13.4). */
  | 'VERIFICATION'
  /** Responder declined; freed for re-dispatch (Req 13.3). Terminal for this responder. */
  | 'DECLINED';

/**
 * A status response the responder can submit. Each maps to one lifecycle
 * transition; which are permitted depends on the current `AssignmentState`
 * (Req 13.2, 13.9).
 */
export type ResponderAction =
  | 'accept'
  | 'decline'
  | 'en-route'
  | 'on-scene'
  | 'completed';

/**
 * One responder assignment. Every field except `state`, `assignmentId`, and
 * `assignedResponderId` is carried verbatim from the assignment notification
 * (Req 13.1); the interface presents exactly these fields (Req 13.2).
 */
export interface Assignment {
  /** Stable assignment identifier used to submit status responses. */
  assignmentId: string;
  /** The request this assignment fulfils (Req 13.1). */
  requestId: string;
  /**
   * The responder this assignment belongs to. The interface presents only
   * assignments whose `assignedResponderId` equals the authenticated
   * responder's identity (Req 13.6); a mismatch is never rendered.
   */
  assignedResponderId: string;
  /** Location reference for the request (Req 13.1). */
  locationReference: string;
  /** Number of occupants at the location (Req 13.1). */
  occupantCount: number;
  /** Whether mobility assistance is required (Req 13.1). */
  mobilityAssistance: boolean;
  /** Whether a medical need is indicated (Req 13.1). */
  medicalNeed: boolean;
  /** The equipment the request requires (Req 13.1). */
  equipmentRequirement: string;
  /** ISO 8601 acknowledgement-deadline timestamp with time zone (Req 13.1). */
  acknowledgementDeadline: string;
  /** Current lifecycle state; gates the reachable controls (Req 13.2, §4.3). */
  state: AssignmentState;
}

/**
 * The responses permitted from each assignment state (Req 13.2, 13.9). This is
 * the single authoritative client-side gate, mirroring the server-side
 * `Request_Lifecycle` transitions (Design §4.3). It is a UX gate only — the
 * server re-validates every transition and rejects a response the state does
 * not permit with the permitted set named (Req 13.9).
 */
export const PERMITTED_ACTIONS: Record<AssignmentState, readonly ResponderAction[]> = {
  AWAITING_ACK: ['accept', 'decline'],
  ACCEPTED: ['en-route'],
  EN_ROUTE: ['on-scene'],
  ON_SCENE: ['completed'],
  VERIFICATION: [],
  DECLINED: [],
};

/** True when `action` is permitted from `state` (Req 13.2, 13.9). */
export function isActionPermitted(state: AssignmentState, action: ResponderAction): boolean {
  return PERMITTED_ACTIONS[state].includes(action);
}
