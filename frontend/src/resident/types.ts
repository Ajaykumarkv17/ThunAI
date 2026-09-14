/**
 * resident/types.ts — the public, aggregate-only shape of Resident_Status_Page state.
 *
 * Every field here is deliberately aggregate or non-identifying. The page MUST
 * exclude every resident name, every resident contact identifier, and every
 * household-level location detail, and present emergency-request information
 * only as aggregate counts per affected area (Req 14.5). These types encode
 * that contract structurally: there is no place to put PII.
 */

/** The ordered severity bands published by the Rule_Engine (Req 14.1). */
export type SeverityBand = 'NORMAL' | 'WATCH' | 'WARNING' | 'EVACUATE';

/** Severity bands in ascending order — kept in sync with policy/rule_engine_rules. */
export const SEVERITY_ORDER: readonly SeverityBand[] = [
  'NORMAL',
  'WATCH',
  'WARNING',
  'EVACUATE',
];

/**
 * One affected area, reported as an aggregate count only (Req 14.5). No resident
 * name, contact, or household-level location — just the area label and how many
 * requests have been reported there.
 */
export interface AffectedArea {
  /** Public area label, e.g. "North Ward". Never a household address. */
  area: string;
  /** Aggregate count of reported requests in this area. Never per-resident. */
  requestCount: number;
}

/**
 * One shelter as shown publicly (Req 14.2). Aggregate capacity/availability only.
 */
export interface ShelterInfo {
  shelterId: string;
  /** Public shelter name. */
  name: string;
  /** Public location reference (landmark / area), not a household address. */
  location: string;
  /** Available capacity as a whole number of spaces, never below zero (Req 14.2). */
  availableCapacity: number;
}

/**
 * One hazard reading shown to residents: the current measured level vs the
 * danger (evacuate) threshold, plainly, so anyone can see how close things are.
 */
export interface HazardLevel {
  /** Reading label, e.g. "River level". */
  label: string;
  /** Current measured value, or null when no reading is available. */
  currentValue: number | null;
  /** The threshold at which evacuation is advised. */
  thresholdValue: number;
  /** Unit, e.g. "m", "mm/h", "m³/s". */
  unit: string;
  /** True when the current value is at or above the threshold. */
  exceeded: boolean;
}

/**
 * The full public state rendered by Resident_Status_Page. Assembled from
 * State_Store snapshots and kept current by AppSync Events (Req 14.3).
 */
export interface ResidentStatus {
  /** Current severity band; the last known band if the reading is stale (Req 14.8). */
  severity: SeverityBand;
  /**
   * Factual, deterministic one-line reason for the current band, built from
   * which readings exceeded threshold (never model-generated). Empty for
   * NORMAL or when no reading is above threshold.
   */
  severityReason?: string;
  /** Public affected-area list with aggregate counts only (Req 14.1, 14.5). */
  affectedAreas: AffectedArea[];
  /** All shelters recorded in State_Store (Req 14.2). */
  shelters: ShelterInfo[];
  /** Current hazard readings vs their evacuate thresholds (Req 14.1). */
  levels: HazardLevel[];
  /** ISO timestamp of the reading that produced the current severity band (Req 14.1). */
  readingTimestamp: string;
  /** True when the reading is older than the staleness limit or unavailable (Req 14.8). */
  readingStale: boolean;
}
