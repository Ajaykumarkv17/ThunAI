/**
 * coordinator/types.ts — the shape of Decision_Inbox state (Req 12.1–12.3).
 *
 * An Escalation_Record is one pending human decision persisted by
 * Escalation_Service (Req 11.1). Decision_Inbox lists every OPEN record ordered
 * by response deadline from soonest to latest, each entry showing the decision
 * summary, the reason for asking, the stakes, the default action, the remaining
 * time before the deadline, and one control per available option (Req 12.1).
 *
 * These types mirror the persisted Escalation_Record fields exactly: the inbox
 * renders only values that come from the record, never model-generated text
 * (Req 11.3).
 */

/** Status of an Escalation_Record. Only OPEN records appear in Decision_Inbox. */
export type EscalationStatus =
  | 'OPEN'
  | 'RESOLVED'
  | 'RESOLVED_BY_DEFAULT';

/**
 * One selectable option on an Escalation_Record. Each carries a distinct option
 * identifier; a record has between 2 and 5 options (Req 11.1).
 */
export interface EscalationOption {
  /** Distinct option identifier submitted back to Escalation_Service (Req 11.1). */
  optionId: string;
  /** Human-readable label for the one-tap control (from the persisted record). */
  label: string;
}

/**
 * One pending human decision as shown in Decision_Inbox. Every field is drawn
 * from the persisted Escalation_Record (Req 11.1, 11.3) — no field is generated
 * by a model invocation.
 */
export interface EscalationRecord {
  /** Interrupt identifier — the stable id used to submit a response (Req 11.1). */
  escalationId: string;
  /** The incident this decision belongs to. */
  incidentId: string;
  /** Current status; Decision_Inbox shows only OPEN records (Req 12.1). */
  status: EscalationStatus;
  /** One-sentence decision summary, ≤200 chars (Req 11.1). */
  decisionSummary: string;
  /** Why a human is being asked (Req 11.1, 12.1). */
  reasonForAsking: string;
  /** The stakes, including any quantity (Req 11.1, 12.1). */
  stakes: string;
  /** The default action applied if no response arrives by the deadline (Req 11.1). */
  defaultAction: string;
  /** Between 2 and 5 available options, each with a distinct id (Req 11.1). */
  options: EscalationOption[];
  /** Absolute response-deadline timestamp with time zone (ISO 8601) (Req 11.1). */
  responseDeadline: string;
  /**
   * True when every out-of-band delivery attempt failed; the record stays OPEN
   * and Decision_Inbox shows the delivery-failure state (Req 11.11).
   */
  deliveryFailed?: boolean;
}

/**
 * The four Rule_Engine severity bands. Ordered ascending here; the open-incident
 * view (Req 12.4) presents incidents by band descending
 * (EVACUATE > WARNING > WATCH > NORMAL) then most-recently-updated first.
 */
export type SeverityBand = 'NORMAL' | 'WATCH' | 'WARNING' | 'EVACUATE';

/**
 * Severity bands in ascending order — the single authoritative UI ordering,
 * mirroring `policy.escalation_policy.SEVERITY_BANDS_ASCENDING` /
 * `surface.queries.order_open_incidents`. An incident's rank is its index here;
 * a higher index means a more severe band.
 */
export const SEVERITY_BANDS_ASCENDING: readonly SeverityBand[] = [
  'NORMAL',
  'WATCH',
  'WARNING',
  'EVACUATE',
];

/**
 * One shelter's availability as shown on the open-incident view (Req 12.4):
 * the number of unoccupied places and the total capacity. Mirrors
 * `surface.queries.ShelterAvailability`.
 */
export interface ShelterAvailability {
  shelterId: string;
  /** Public shelter name. */
  name: string;
  /** Unoccupied places (whole number, never below zero) (Req 12.4). */
  unoccupiedPlaces: number;
  /** Total capacity (whole number). */
  totalCapacity: number;
}

/**
 * One open incident row (Req 12.4). Mirrors `surface.queries.IncidentSummary`:
 * the severity band, the affected areas, the count of open requests, the count
 * of assigned responders, ward-wide shelter availability, and the last-update
 * timestamp used for the recency tie-break.
 */
export interface IncidentSummary {
  incidentId: string;
  /** Current severity band; drives the descending band ordering (Req 12.4). */
  severityBand: SeverityBand;
  /** Public affected-area labels. */
  affectedAreas: string[];
  /** Count of currently open requests on this incident (Req 12.4). */
  openRequestCount: number;
  /** Count of responders currently assigned to this incident's requests (Req 12.4). */
  assignedResponderCount: number;
  /** Ward-wide shelter availability shown on the incident view (Req 12.4). */
  shelters: ShelterAvailability[];
  /** ISO 8601 timestamp of the last update; recency tie-break within a band (Req 12.4). */
  updatedAt: string;
}

/**
 * One audit entry as the per-incident audit trail view presents it (Req 12.7).
 *
 * Mirrors `surface.queries.AuditView` (the server-side projection of an
 * `Audit_Ledger` `AuditEntry`) exactly — only the fields that view exposes are
 * carried here, so the trail can never render a field the ledger does not
 * persist:
 *  - `timestamp` — ISO 8601 with time zone; the oldest→newest ordering key
 *    (Req 12.7). Ties break by `entryId` ascending, matching the persisted
 *    `sk = "{timestamp}#{entryId}"` composition (`memory.audit_ledger`).
 *  - `toolName` — the write tool the entry records.
 *  - `inputs` — the tool inputs **with direct personal identifiers already
 *    excluded** (Req 12.7). Redaction happens once, before persistence, in
 *    `harness/audit.py::append_audit_entry` → `harness.redaction.redact_fields`;
 *    the view (and this client) render the stored value as-is and never
 *    re-redact, since re-redacting a stored entry would imply the ledger might
 *    hold un-redacted data.
 *  - `outcome` — the recorded result of the tool call.
 *  - `approvingHumanId` — the id of the human who approved the action, where an
 *    approval applied (Req 12.7); absent for autonomously-executed entries.
 */
/**
 * One recent run as the recent-runs cost/latency view presents it (Req 1.7,
 * Req 12.8). Mirrors `surface.queries.RunSummary` exactly — the client renders
 * only fields that server-side projection exposes, so the view can never show a
 * value the run record does not carry.
 *
 * Req 1.7 fields (the run's identity and outcome): `runId`, `triggerType`,
 * `triggerSourceId`, `startedAt`, and the *terminal* `terminalStatus` — a run
 * still in progress has no terminal status yet, so `terminalStatus` is null
 * until the run reaches a terminal state rather than passing off a live status
 * as terminal.
 *
 * Req 12.8 columns (cost/latency): token counts, the wall-clock `latencyMs`,
 * the invoked `modelId`, and the `estimatedCost` with its `currency` named. All
 * are optional/nullable because they are recorded only when a run reaches a
 * terminal status (Req 20.3); an in-progress or metrics-less run leaves them
 * unset, and the view renders an explicit placeholder rather than a bare blank.
 */
export interface RunSummary {
  /** The run identifier (Req 1.7). */
  runId: string;
  /** The trigger type, e.g. `sweep` or `ingestion` (Req 1.7); null if unrecorded. */
  triggerType?: string | null;
  /** The trigger source identifier (Req 1.7); null if unrecorded. */
  triggerSourceId?: string | null;
  /** ISO 8601 start timestamp (UTC); the most-recent-first ordering key (Req 1.7). */
  startedAt: string;
  /** The terminal run status (Req 1.7); null while the run is still in progress. */
  terminalStatus?: string | null;
  /** Input token count (Req 12.8); null when not yet recorded. */
  inputTokens?: number | null;
  /** Output token count (Req 12.8); null when not yet recorded. */
  outputTokens?: number | null;
  /** Total token count as a whole number (Req 12.8); null when not yet recorded. */
  totalTokens?: number | null;
  /** Run wall-clock latency in milliseconds (Req 12.8); null when not yet recorded. */
  latencyMs?: number | null;
  /** The invoked model identifier (Req 12.8); null when not yet recorded. */
  modelId?: string | null;
  /**
   * Estimated cost in the configured currency (Req 12.8); null when not yet
   * recorded. Paired with `currency` so the amount is never shown unlabelled.
   */
  estimatedCost?: number | null;
  /** The currency the `estimatedCost` is expressed in (Req 12.8); null if unrecorded. */
  currency?: string | null;
}

export interface AuditView {
  /** ISO 8601 timestamp with time zone; oldest→newest ordering key (Req 12.7). */
  timestamp: string;
  /**
   * Stable per-entry id used only as the ascending tie-break on equal
   * timestamps, mirroring the persisted sort-key composition. Optional because
   * the server `AuditView` projection does not require it for display.
   */
  entryId?: string;
  /** The write tool this entry records. */
  toolName: string;
  /** Redacted tool inputs — rendered as stored, never re-redacted (Req 12.7). */
  inputs: Record<string, unknown>;
  /** The recorded outcome of the tool call. */
  outcome: string;
  /** Approver id where a human approval applied; absent otherwise (Req 12.7). */
  approvingHumanId?: string | null;
}

/**
 * One turn in the Coordinator_Orchestrator conversational panel (Req 12.11).
 *
 * The panel is a *secondary* surface: it sends the coordinator's message to the
 * AgentCore entrypoint with `mode=chat` (Design §3.8 — the single
 * mode-dispatching `/invocations` entrypoint routes `chat` to
 * `run_coordinator_chat()`) and renders the returned assistant reply. Turns are
 * held only in the panel's local component state so a chat failure can never
 * disturb Decision_Inbox or the open-incident list (Req 12.11).
 *
 * A turn carries no persisted identity — it is display-only chat history, not
 * an operational record — so the fields are exactly what the panel renders: who
 * spoke, the text, and whether the turn is an error notice (so it can be styled
 * and announced as an alert rather than a normal reply).
 */
export interface ChatTurn {
  /** Stable per-turn key for rendering; local to the panel, never persisted. */
  id: string;
  /** Who produced the turn: the coordinator, or the orchestrator's reply. */
  role: 'coordinator' | 'orchestrator';
  /** The message text shown for this turn. */
  text: string;
  /**
   * True when this orchestrator turn is an error notice (request failed or the
   * request could not be routed — Req 4.11). Rendered as an alert, not a reply.
   */
  isError?: boolean;
}
