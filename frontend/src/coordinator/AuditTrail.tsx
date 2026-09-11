/**
 * coordinator/AuditTrail.tsx — per-incident audit trail view (Req 12.7).
 *
 * Lists every `Audit_Ledger` entry for the incident named in the route,
 * ordered from oldest to newest (Req 12.7). Each entry shows its timestamp, the
 * tool that ran, the recorded outcome, the (already-redacted) tool inputs, and
 * the approver id where a human approval applied. Inputs arrive redacted from
 * the harness — direct personal identifiers are excluded before persistence —
 * so this view renders them exactly as stored and never re-redacts them.
 *
 * This is a read-only view over an append-only ledger: it presents state only
 * and carries no mutating controls. It is kept current by AppSync Events on the
 * incident's channel (Req 12.5); a staleness indicator is shown when live
 * updates go silent (Req 12.6).
 *
 * Accessibility (Req 12.12): semantic landmarks (`<main>`, `<section>`,
 * headings), an ordered list to convey the oldest→newest sequence, a definition
 * list per entry, and `aria-live` on the loading/staleness regions. Full WCAG
 * 2.1 AA conformance still requires manual assistive-technology testing; this
 * component supplies the structural foundation for it.
 */

import { useParams } from 'react-router-dom';
import { useI18n, type I18n } from '../shared/i18n';
import { useIncidentAudit } from './useIncidentAudit';
import type { AuditView } from './types';

export function AuditTrail() {
  const i18n = useI18n();
  const { t } = i18n;
  const { incidentId } = useParams<{ incidentId: string }>();
  const { entries, loading, connectionStale } = useIncidentAudit(incidentId);

  return (
    <main className="audit-trail">
      <header className="audit-trail__header">
        <h1>{t('audit.title')}</h1>
        {incidentId && (
          <p className="audit-trail__incident">
            {t('audit.incident')}: <span>{incidentId}</span>
          </p>
        )}
      </header>

      {connectionStale && (
        <p className="audit-trail__stale" role="status" aria-live="polite">
          {t('audit.connectionStale')}
        </p>
      )}

      {loading && entries.length === 0 ? (
        <p role="status" aria-live="polite">
          {t('audit.loading')}
        </p>
      ) : entries.length === 0 ? (
        <p className="audit-trail__empty">{t('audit.empty')}</p>
      ) : (
        <ol className="audit-trail__list">
          {entries.map((entry, index) => (
            <li key={entry.entryId ?? `${entry.timestamp}#${index}`}>
              <AuditRow entry={entry} i18n={i18n} />
            </li>
          ))}
        </ol>
      )}
    </main>
  );
}

/**
 * One audit entry row: timestamp, tool, outcome, redacted inputs, and the
 * approver id where applicable (Req 12.7).
 */
function AuditRow({ entry, i18n }: { entry: AuditView; i18n: I18n }) {
  const { t } = i18n;
  const inputKeys = Object.keys(entry.inputs ?? {});

  return (
    <section className="audit-row" aria-label={`${entry.toolName} — ${entry.timestamp}`}>
      <dl className="audit-row__details">
        <dt>{t('audit.timestamp')}</dt>
        <dd>
          <time dateTime={entry.timestamp}>{entry.timestamp}</time>
        </dd>

        <dt>{t('audit.tool')}</dt>
        <dd>{entry.toolName}</dd>

        <dt>{t('audit.outcome')}</dt>
        <dd>{entry.outcome}</dd>

        <dt>{t('audit.approver')}</dt>
        <dd>
          {entry.approvingHumanId ? entry.approvingHumanId : t('audit.approver.none')}
        </dd>

        <dt>{t('audit.inputs')}</dt>
        <dd>
          {inputKeys.length === 0 ? (
            <span className="audit-row__inputs-empty">{t('audit.inputs.none')}</span>
          ) : (
            <dl className="audit-row__inputs">
              {inputKeys.map((key) => (
                <div key={key} className="audit-row__input">
                  <dt>{key}</dt>
                  <dd>{formatInputValue(entry.inputs[key])}</dd>
                </div>
              ))}
            </dl>
          )}
        </dd>
      </dl>
    </section>
  );
}

/**
 * Render one redacted input value as plain text. The value is already redacted
 * upstream (Req 12.7) — this only stringifies it for display, applying no
 * transformation to its content. Objects/arrays are shown as compact JSON so a
 * nested redacted structure remains inspectable.
 */
function formatInputValue(value: unknown): string {
  if (value === null || value === undefined) {
    return '—';
  }
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}
