/**
 * coordinator/index.tsx — route wiring for Coordinator_Console (Design §3.8).
 *
 * The console is gated to the `coordinators` Cognito group (Req 12.9, 12.10)
 * via `RouteGuard`. Decision_Inbox is the default landing view (Design §3.8):
 *
 *   /console                              -> redirect to /console/inbox
 *   /console/inbox                        -> Decision_Inbox            (coordinators group — Req 12.9)
 *   /console/incidents                    -> open-incident list + shelters (coordinators — Req 12.4)
 *   /console/incidents/:incidentId/audit  -> per-incident audit trail  (coordinators — Req 12.7)
 *   /console/runs                         -> recent-runs cost/latency  (coordinators — Req 1.7, 12.8)
 *   /console/chat                         -> Coordinator_Orchestrator conversational panel (coordinators — Req 12.11, secondary surface)
 *
 * This module wires the Decision_Inbox (task 25.1), the open-incident list +
 * shelter availability (task 25.2), the per-incident audit trail (task 25.3),
 * the recent-runs cost/latency view (task 25.4), and the Coordinator_Orchestrator
 * conversational panel (task 25.5). The chat panel is a *secondary* surface: it
 * is a self-contained route/component with its own local state and no shared
 * state or subscription, so its failure or unavailability leaves Decision_Inbox
 * and the open-incident list fully operable (Req 12.11).
 */

import { Navigate, Route } from 'react-router-dom';
import { RouteGuard } from '../shared/auth';
import { AuditTrail } from './AuditTrail';
import { ChatPanel } from './ChatPanel';
import { DecisionInbox } from './DecisionInbox';
import { IncidentList } from './IncidentList';
import { RunList } from './RunList';

export { AuditTrail } from './AuditTrail';
export { ChatPanel } from './ChatPanel';
export { DecisionInbox } from './DecisionInbox';
export { IncidentList } from './IncidentList';
export { RunList } from './RunList';
export { useDecisionInbox, remainingTime } from './useDecisionInbox';
export { useOpenIncidents, orderOpenIncidents } from './useOpenIncidents';
export { useIncidentAudit, orderIncidentAudit } from './useIncidentAudit';
export { useRecentRuns, orderRuns, MAX_RECENT_RUNS } from './useRecentRuns';
export { useCoordinatorChat } from './useCoordinatorChat';
export type {
  EscalationRecord,
  EscalationOption,
  EscalationStatus,
  IncidentSummary,
  ShelterAvailability,
  SeverityBand,
  AuditView,
  RunSummary,
  ChatTurn,
} from './types';
export { SEVERITY_BANDS_ASCENDING } from './types';

/**
 * The coordinator console routes as a fragment of `<Route>` elements, ready to
 * drop into the app-level `<Routes>`. Every route is wrapped in the coordinator
 * `RouteGuard` so no protected content renders for a non-coordinator (Req 12.10).
 */
export function coordinatorRoutes() {
  return (
    <>
      <Route path="/console" element={<Navigate to="/console/inbox" replace />} />
      <Route
        path="/console/inbox"
        element={
          <RouteGuard requiredGroup="coordinators">
            <DecisionInbox />
          </RouteGuard>
        }
      />
      <Route
        path="/console/incidents"
        element={
          <RouteGuard requiredGroup="coordinators">
            <IncidentList />
          </RouteGuard>
        }
      />
      <Route
        path="/console/incidents/:incidentId/audit"
        element={
          <RouteGuard requiredGroup="coordinators">
            <AuditTrail />
          </RouteGuard>
        }
      />
      <Route
        path="/console/runs"
        element={
          <RouteGuard requiredGroup="coordinators">
            <RunList />
          </RouteGuard>
        }
      />
      <Route
        path="/console/chat"
        element={
          <RouteGuard requiredGroup="coordinators">
            <ChatPanel />
          </RouteGuard>
        }
      />
    </>
  );
}
