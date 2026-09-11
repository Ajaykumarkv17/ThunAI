/**
 * coordinator/RunList.tsx — recent-runs cost/latency view (Req 1.7, Req 12.8).
 *
 * Lists up to the 20 most recent runs, ordered from most recent to least recent
 * (Req 1.7). Each run row shows its identity and outcome — run id, trigger type,
 * trigger source id, start timestamp, and terminal status (Req 1.7) — alongside
 * the cost/latency columns: token counts, wall-clock latency in milliseconds,
 * the invoked model id, and the estimated cost with its currency named
 * (Req 12.8). When no runs exist an explicit empty state is shown (Req 1.7).
 *
 * The cost/latency columns are recorded only when a run reaches a terminal
 * status (Req 20.3); a run still in progress leaves them unset, so each such
 * cell renders an explicit placeholder rather than a bare blank, and the
 * estimated cost is always shown with its currency so an amount is never
 * presented unlabelled.
 *
 * This is a read-only view: it presents run state only and carries no mutating
 * controls. It is kept current by AppSync Events (Req 12.5); a staleness
 * indicator is shown when live updates go silent (Req 12.6).
 *
 * Accessibility (Req 12.12): semantic landmarks (`<main>`, `<section>`,
 * headings), an ordered list to convey the most-recent-first sequence, a
 * definition list per run, and `aria-live` on the loading/staleness regions.
 * Full WCAG 2.1 AA conformance still requires manual assistive-technology
 * testing; this component supplies the structural foundation for it.
 */

import { useI18n, type I18n } from '../shared/i18n';
import { useRecentRuns } from './useRecentRuns';
import type { RunSummary } from './types';

export function RunList() {
  const i18n = useI18n();
  const { t } = i18n;
  const { runs, loading, connectionStale } = useRecentRuns();

  return (
    <main className="run-list">
      <header className="run-list__header">
        <h1>{t('runs.title')}</h1>
      </header>

      {connectionStale && (
        <p className="run-list__stale" role="status" aria-live="polite">
          {t('runs.connectionStale')}
        </p>
      )}

      {loading && runs.length === 0 ? (
        <p role="status" aria-live="polite">
          {t('runs.loading')}
        </p>
      ) : runs.length === 0 ? (
        <p className="run-list__empty">{t('runs.empty')}</p>
      ) : (
        <ol className="run-list__list">
          {runs.map((run) => (
            <li key={run.runId}>
              <RunRow run={run} i18n={i18n} />
            </li>
          ))}
        </ol>
      )}
    </main>
  );
}

/**
 * One run row: identity + outcome (Req 1.7) and the cost/latency columns
 * (Req 12.8). Unrecorded values render an explicit placeholder.
 */
function RunRow({ run, i18n }: { run: RunSummary; i18n: I18n }) {
  const { t } = i18n;
  const none = t('runs.value.none');

  return (
    <section className="run-row" aria-label={`${run.runId} — ${run.startedAt}`}>
      <dl className="run-row__details">
        <dt>{t('runs.runId')}</dt>
        <dd>{run.runId}</dd>

        <dt>{t('runs.triggerType')}</dt>
        <dd>{orNone(run.triggerType, none)}</dd>

        <dt>{t('runs.triggerSource')}</dt>
        <dd>{orNone(run.triggerSourceId, none)}</dd>

        <dt>{t('runs.startedAt')}</dt>
        <dd>
          <time dateTime={run.startedAt}>{run.startedAt}</time>
        </dd>

        <dt>{t('runs.terminalStatus')}</dt>
        <dd>
          {run.terminalStatus ? run.terminalStatus : t('runs.status.inProgress')}
        </dd>

        <dt>{t('runs.tokens')}</dt>
        <dd>{formatTokens(run, t)}</dd>

        <dt>{t('runs.latency')}</dt>
        <dd>{formatLatency(run.latencyMs, t, none)}</dd>

        <dt>{t('runs.model')}</dt>
        <dd>{orNone(run.modelId, none)}</dd>

        <dt>{t('runs.cost')}</dt>
        <dd>{formatCost(run.estimatedCost, run.currency, none)}</dd>
      </dl>
    </section>
  );
}

/** Render a nullable string value, or the explicit placeholder when unset. */
function orNone(value: string | null | undefined, none: string): string {
  return value !== null && value !== undefined && value !== '' ? value : none;
}

/**
 * Token summary: the total token count as a whole number (Req 12.8), with the
 * input/output split appended when both are known. Falls back to whichever of
 * the three counts is present, and to the placeholder when none is.
 */
function formatTokens(run: RunSummary, t: I18n['t']): string {
  const { totalTokens, inputTokens, outputTokens } = run;
  const hasInput = inputTokens !== null && inputTokens !== undefined;
  const hasOutput = outputTokens !== null && outputTokens !== undefined;

  // Prefer the recorded total; otherwise derive it from whichever of the split
  // counts are present. When none of the three is recorded, show the placeholder.
  let total: number | null = null;
  if (totalTokens !== null && totalTokens !== undefined) {
    total = totalTokens;
  } else if (hasInput || hasOutput) {
    total = (inputTokens ?? 0) + (outputTokens ?? 0);
  }

  if (total === null) {
    return t('runs.value.none');
  }

  if (hasInput && hasOutput) {
    return `${total} (${t('runs.tokens.in')} ${inputTokens} / ${t('runs.tokens.out')} ${outputTokens})`;
  }
  return String(total);
}

/** Render the wall-clock latency in milliseconds (Req 12.8), or the placeholder. */
function formatLatency(
  latencyMs: number | null | undefined,
  t: I18n['t'],
  none: string,
): string {
  if (latencyMs === null || latencyMs === undefined) {
    return none;
  }
  return `${latencyMs} ${t('runs.latency.unit')}`;
}

/**
 * Render the estimated cost with its currency named (Req 12.8): the currency
 * identifier precedes the amount so the value is never shown unlabelled. When
 * the cost is unrecorded, or its currency is unknown, the explicit placeholder
 * is shown instead of a bare or ambiguous number.
 */
function formatCost(
  estimatedCost: number | null | undefined,
  currency: string | null | undefined,
  none: string,
): string {
  if (estimatedCost === null || estimatedCost === undefined) {
    return none;
  }
  if (!currency) {
    // An amount with no currency would be ambiguous; withhold rather than
    // present an unlabelled figure (Req 12.8 requires the currency identified).
    return none;
  }
  return `${currency} ${estimatedCost}`;
}
