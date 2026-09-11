/**
 * resident/index.tsx — public route wiring for Resident_Status_Page.
 *
 * Exposes the route group the app router mounts (design §3.8):
 *
 *   /         -> redirect to /status  (public)
 *   /status   -> Resident_Status_Page (no auth — Req 14.1)
 *
 * The resident surface requires no authentication, so — unlike the coordinator
 * and responder surfaces — these routes are not wrapped in `RouteGuard`.
 */

import { Navigate, Route } from 'react-router-dom';
import { ResidentStatusPage } from './ResidentStatusPage';

export { ResidentStatusPage } from './ResidentStatusPage';
export { useResidentStatus } from './useResidentStatus';
export type {
  ResidentStatus,
  SeverityBand,
  AffectedArea,
  ShelterInfo,
  RuleSet,
  RuleThreshold,
} from './types';

/**
 * The public resident routes as a fragment of `<Route>` elements, ready to drop
 * into the app-level `<Routes>`.
 */
export function residentRoutes() {
  return (
    <>
      <Route path="/" element={<Navigate to="/status" replace />} />
      <Route path="/status" element={<ResidentStatusPage />} />
    </>
  );
}
