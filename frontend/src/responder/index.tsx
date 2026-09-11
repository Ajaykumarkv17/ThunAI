/**
 * responder/index.tsx — route wiring for Responder_Interface (Design §3.8).
 *
 * The interface is gated to the `responders` Cognito group (Req 13.6) via
 * `RouteGuard`:
 *
 *   /responder             -> redirect to /responder/assignments
 *   /responder/assignments -> Responder_Interface (responders group — Req 13.6)
 *
 * `RouteGuard` denies a non-responder with an authorisation-required message and
 * discloses no assignment content on mismatch; the interface itself
 * additionally scopes every assignment to the authenticated responder's own
 * identity (Req 13.6). The client guard is UX only — AppSync Events channel
 * authorization and the API authorizer re-check the same Cognito group and
 * identity server-side, so a client bypass discloses nothing (Design §3.8).
 */

import { Navigate, Route } from 'react-router-dom';
import { RouteGuard } from '../shared/auth';
import { ResponderInterface } from './ResponderInterface';

export { ResponderInterface } from './ResponderInterface';
export { useResponderAssignments } from './useResponderAssignments';
export type {
  ResponderError,
  InvalidTransitionError,
  SubmitFailedError,
  ResponderAssignmentsState,
} from './useResponderAssignments';
export type { Assignment, AssignmentState, ResponderAction } from './types';
export { PERMITTED_ACTIONS, isActionPermitted } from './types';

/**
 * The responder routes as a fragment of `<Route>` elements, ready to drop into
 * the app-level `<Routes>`. Every route is wrapped in the responder
 * `RouteGuard` so no protected content renders for a non-responder (Req 13.6).
 */
export function responderRoutes() {
  return (
    <>
      <Route
        path="/responder"
        element={<Navigate to="/responder/assignments" replace />}
      />
      <Route
        path="/responder/assignments"
        element={
          <RouteGuard requiredGroup="responders">
            <ResponderInterface />
          </RouteGuard>
        }
      />
    </>
  );
}
