/**
 * coordinator/DecisionInbox.tsx — the Decision_Inbox view (Req 12.1–12.3).
 *
 * Lists every OPEN Escalation_Record ordered by response deadline from soonest
 * to latest (Req 12.1). Each entry shows the decision summary, the reason for
 * asking, the stakes, the default action, the remaining time before the
 * deadline in whole minutes and seconds (recomputed at least every 10s), and
 * one control per available option.
 *
 * One-tap semantics (Req 12.2): activating an option submits it on a single
 * activation and disables every option control of that record until
 * Escalation_Service confirms an outcome. On an error or a no-confirmation
 * within 10s the record stays in the inbox, an error indication is shown, and
 * the controls are re-enabled for retry (Req 12.3). A record whose out-of-band
 * delivery failed is shown with a delivery-failure state but remains resolvable
 * here (Req 11.11).
 *
 * Accessibility (Req 12.12): semantic landmarks (`<main>`, `<section>`,
 * headings), real `<button>` controls, `aria-live` on the countdown and error
 * regions, and `aria-busy` on a record while its submission is in flight. Full
 * WCAG 2.1 AA conformance still requires manual assistive-technology testing;
 * this component supplies the structural foundation for it.
 */

import { useI18n, type I18n } from '../shared/i18n';
import { ActiveDispatches } from './ActiveDispatches';
import { remainingTime, useDecisionInbox } from './useDecisionInbox';
import type { EscalationRecord } from './types';

/** Format remaining time as `M:SS` whole minutes and seconds (Req 12.1). */
function formatRemaining(minutes: number, seconds: number): string {
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

export function DecisionInbox() {
  const i18n = useI18n();
  const { t } = i18n;
  const {
    records,
    loading,
    nowMs,
    submitting,
    errors,
    connectionStale,
    submitOption,
  } = useDecisionInbox();

  return (
    <main className="decision-inbox">
      <header className="decision-inbox__header">
        <h1>{t('inbox.title')}</h1>
      </header>

      {connectionStale && (
        <p className="decision-inbox__stale" role="status" aria-live="polite">
          {t('inbox.connectionStale')}
        </p>
      )}

      {loading && records.length === 0 ? (
        <p role="status" aria-live="polite">
          {t('inbox.loading')}
        </p>
      ) : records.length === 0 ? (
        <p className="decision-inbox__empty">{t('inbox.empty')}</p>
      ) : (
        <ol className="decision-inbox__list">
          {records.map((record) => (
            <li key={record.escalationId}>
              <EscalationCard
                record={record}
                nowMs={nowMs}
                disabled={Boolean(submitting[record.escalationId])}
                error={errors[record.escalationId]}
                onSelect={submitOption}
                i18n={i18n}
              />
            </li>
          ))}
        </ol>
      )}

      {/* Live view of dispatches the coordinator approved, closing the loop. */}
      <ActiveDispatches />
    </main>
  );
}

/** One Escalation_Record card with its per-option one-tap controls. */
function EscalationCard({
  record,
  nowMs,
  disabled,
  error,
  onSelect,
  i18n,
}: {
  record: EscalationRecord;
  nowMs: number;
  disabled: boolean;
  error: string | undefined;
  onSelect: (escalationId: string, optionId: string) => void;
  i18n: I18n;
}) {
  const { t } = i18n;
  const { minutes, seconds, expired } = remainingTime(record.responseDeadline, nowMs);

  const urgent = !expired && minutes < 10;

  return (
    <section
      className="escalation-card"
      aria-labelledby={`esc-${record.escalationId}-summary`}
      aria-busy={disabled}
    >
      {/* Header: decision-required tag + live countdown chip. */}
      <div className="escalation-card__head">
        <span className="escalation-card__tag">Decision required</span>
        <span
          className={`escalation-card__timer${urgent ? ' escalation-card__timer--urgent' : ''}${expired ? ' escalation-card__timer--expired' : ''}`}
          role="timer"
          aria-live="polite"
        >
          {expired ? t('inbox.deadlinePassed') : formatRemaining(minutes, seconds)}
        </span>
      </div>

      <h2 id={`esc-${record.escalationId}-summary`} className="escalation-card__summary">
        {record.decisionSummary}
      </h2>

      {record.deliveryFailed && (
        <p className="escalation-card__delivery-failed" role="status">
          {t('inbox.deliveryFailed')}
        </p>
      )}

      <dl className="escalation-card__details">
        <div className="escalation-card__field">
          <dt>{t('inbox.reason')}</dt>
          <dd>{record.reasonForAsking}</dd>
        </div>
        <div className="escalation-card__field">
          <dt>{t('inbox.stakes')}</dt>
          <dd>{record.stakes}</dd>
        </div>
      </dl>

      {/* Footer: default-action note + the decision controls. */}
      <div className="escalation-card__footer">
        <p className="escalation-card__default">
          If no response by the deadline: <strong>{record.defaultAction}</strong>
        </p>
        <div className="escalation-card__options" role="group" aria-label={record.decisionSummary}>
          {record.options.map((option, idx) => (
            <button
              key={option.optionId}
              type="button"
              className={
                idx === 0
                  ? 'escalation-card__option escalation-card__option--primary'
                  : 'escalation-card__option escalation-card__option--secondary'
              }
              disabled={disabled}
              onClick={() => onSelect(record.escalationId, option.optionId)}
            >
              {disabled ? t('inbox.submitting') : option.label}
            </button>
          ))}
        </div>
      </div>

      {/* Error indication; controls above are already re-enabled for retry (Req 12.3). */}
      {error && (
        <p className="escalation-card__error" role="alert" aria-live="assertive">
          {t('inbox.submitError')}
        </p>
      )}
    </section>
  );
}
