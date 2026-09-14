/**
 * resident/ResidentStatusPage.tsx — the public Resident_Status_Page (Req 14).
 *
 * A no-auth public surface (route `/status`, Req 14.1) that presents:
 *  - the current severity band + recommended action + reading timestamp (Req 14.1),
 *  - the affected-area list as aggregate counts only (Req 14.1, 14.5),
 *  - every shelter with name / location / available capacity + full indicator (Req 14.2),
 *  - the advisory notice, visible on load without interaction (Req 14.6),
 *  - the active rule set: thresholds, units, staleness limit, Rule_Set_Version (Req 2.7),
 *  - a language selector applying Tamil / English to every field (Req 14.4),
 *  - stale-reading and out-of-date-data indicators (Req 14.8, 14.9),
 *  - per-field "translation unavailable" indicators (Req 14.10).
 *
 * There is no PII on this page by construction — the `ResidentStatus` type has
 * no field for a resident name, contact, or household location (Req 14.5).
 *
 * Accessibility (Req 14.7): semantic landmarks (`<main>`, `<section>`,
 * headings), a real `<select>` for language, `aria-live` on the status banner
 * and the data-freshness notices, and no icon-only or non-text-only content.
 * Full WCAG 2.1 AA conformance still requires manual assistive-technology
 * testing; this component supplies the structural foundation for it.
 */

import { useI18n, type LanguageCode, type Translation } from '../shared/i18n';
import { LANGUAGE_LABELS, SUPPORTED_LANGUAGES } from '../shared/i18n';
import { useResidentStatus } from './useResidentStatus';
import type { SeverityBand } from './types';

/** Community time zone for reading timestamps (Req 14.1). */
const COMMUNITY_TIME_ZONE =
  (import.meta.env?.VITE_COMMUNITY_TIME_ZONE as string | undefined) ?? 'Asia/Kolkata';

/** Format an ISO timestamp in the configured community time zone (Req 14.1). */
function formatInCommunityZone(iso: string, language: LanguageCode): string {
  const locale = language === 'ta' ? 'ta-IN' : 'en-IN';
  try {
    return new Intl.DateTimeFormat(locale, {
      dateStyle: 'medium',
      timeStyle: 'short',
      timeZone: COMMUNITY_TIME_ZONE,
    }).format(new Date(iso));
  } catch {
    return iso;
  }
}

/** Whole-minutes age of a reading, for the stale-reading indicator (Req 14.8). */
function ageMinutes(iso: string): number {
  const ms = Date.now() - new Date(iso).getTime();
  return Math.max(0, Math.floor(ms / 60_000));
}

/**
 * Render a translated field, appending the per-field "translation unavailable"
 * note when the selected language lacked the key and the default was used
 * (Req 14.10).
 */
function TranslatedText({ tr, note }: { tr: Translation; note: string }) {
  if (!tr.fallback) {
    return <>{tr.value}</>;
  }
  return (
    <>
      {tr.value} <span className="translation-fallback"> ({note})</span>
    </>
  );
}

export function ResidentStatusPage() {
  const i18n = useI18n();
  const { t, tf, language, setLanguage } = i18n;
  const { status, loading, retrievalFailed, lastRetrievedAt } =
    useResidentStatus();

  const translationUnavailable = t('translation.unavailable');

  return (
    <main className="resident-status" lang={language}>
      <header className="resident-status__header">
        <h1>{t('status.title')}</h1>
        <label className="language-select">
          <span>{t('language.label')}</span>
          <select
            value={language}
            onChange={(e) => setLanguage(e.target.value as LanguageCode)}
            aria-label={t('language.label')}
          >
            {SUPPORTED_LANGUAGES.map((code) => (
              <option key={code} value={code}>
                {LANGUAGE_LABELS[code]}
              </option>
            ))}
          </select>
        </label>
      </header>

      {/* Advisory notice — visible on load without interaction (Req 14.6). */}
      <section className="advisory" aria-labelledby="advisory-heading" role="note">
        <h2 id="advisory-heading">{t('advisory.title')}</h2>
        <p>
          <TranslatedText tr={tf('advisory.notice')} note={translationUnavailable} />
        </p>
      </section>

      {/* Data-freshness notice (Req 14.8, 14.9). Shown only when the actual
          data retrieval fails — NOT when the realtime websocket is merely
          quiet, since the 15s polling fetch keeps the values current
          regardless (a quiet socket is not stale data). */}
      {retrievalFailed && (
        <p className="data-freshness" role="status" aria-live="polite">
          {t('status.dataMayBeOutdated')}
          {lastRetrievedAt && (
            <>
              {' '}
              — {t('status.lastRetrieved')}:{' '}
              {formatInCommunityZone(lastRetrievedAt, language)}
            </>
          )}
        </p>
      )}

      {loading && !status ? (
        <p role="status" aria-live="polite">
          …
        </p>
      ) : status ? (
        <>
          <SeveritySection
            severity={status.severity}
            severityReason={status.severityReason}
            readingTimestamp={status.readingTimestamp}
            readingStale={status.readingStale}
            i18n={i18n}
            translationUnavailable={translationUnavailable}
          />

          {/* Affected areas — aggregate counts only, no PII (Req 14.1, 14.5). */}
          <section className="affected-areas" aria-labelledby="areas-heading">
            <h2 id="areas-heading">{t('status.affectedAreas')}</h2>
            {status.affectedAreas.length === 0 ? (
              <p>{t('status.affectedAreas.none')}</p>
            ) : (
              <ul>
                {status.affectedAreas.map((a) => (
                  <li key={a.area}>
                    {a.area}: {a.requestCount} {t('status.affectedAreas.requestCount')}
                  </li>
                ))}
              </ul>
            )}
          </section>

          {/* Shelters — aggregate capacity/availability only (Req 14.2). */}
          <section className="shelters" aria-labelledby="shelters-heading">
            <h2 id="shelters-heading">{t('shelters.title')}</h2>
            {status.shelters.length === 0 ? (
              <p>{t('shelters.none')}</p>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th scope="col">{t('shelters.name')}</th>
                    <th scope="col">{t('shelters.location')}</th>
                    <th scope="col">{t('shelters.available')}</th>
                  </tr>
                </thead>
                <tbody>
                  {status.shelters.map((s) => {
                    const available = Math.max(0, s.availableCapacity);
                    const full = available === 0;
                    return (
                      <tr key={s.shelterId} className={full ? 'shelter--full' : undefined}>
                        <td>{s.name}</td>
                        <td>{s.location}</td>
                        <td>{full ? t('shelters.full') : available}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </section>

          {/* Current hazard levels vs the evacuate threshold — the plain
              "how close are we to danger?" view residents care about. */}
          <section className="levels" aria-labelledby="levels-heading">
            <h2 id="levels-heading">Current levels</h2>
            <table>
              <thead>
                <tr>
                  <th scope="col">Measurement</th>
                  <th scope="col">Current level</th>
                  <th scope="col">Danger threshold</th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {(status.levels ?? []).map((lvl) => (
                  <tr key={lvl.label} className={lvl.exceeded ? 'level--exceeded' : undefined}>
                    <td>{lvl.label}</td>
                    <td>
                      {lvl.currentValue == null ? '—' : `${lvl.currentValue} ${lvl.unit}`}
                    </td>
                    <td>
                      {lvl.thresholdValue} {lvl.unit}
                    </td>
                    <td>
                      <span className={lvl.exceeded ? 'level-pill level-pill--danger' : 'level-pill level-pill--safe'}>
                        {lvl.exceeded ? 'Above danger' : 'Within safe range'}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </>
      ) : (
        <p className="resident-status__no-data" role="status" aria-live="polite">
          Live status is not available right now. Please check official emergency channels.
        </p>
      )}
    </main>
  );
}

/** Severity banner: band, recommended action, reading time, stale indicator. */
function SeveritySection({
  severity,
  readingTimestamp,
  readingStale,
  i18n,
  translationUnavailable,
  severityReason,
}: {
  severity: SeverityBand;
  severityReason?: string;
  readingTimestamp: string;
  readingStale: boolean;
  i18n: ReturnType<typeof useI18n>;
  translationUnavailable: string;
}) {
  const { t, tf, language } = i18n;
  return (
    <section
      className={`severity severity--${severity}`}
      aria-labelledby="severity-heading"
      aria-live="polite"
    >
      <h2 id="severity-heading">{t('status.severity.label')}</h2>
      <p className="severity__band">
        <TranslatedText tr={tf(`status.severity.${severity}`)} note={translationUnavailable} />
      </p>
      {severityReason && (
        <p className="severity__reason">
          <strong>Why:</strong> {severityReason}
        </p>
      )}
      <p className="severity__action">
        <strong>{t('status.recommendedAction')}:</strong>{' '}
        <TranslatedText
          tr={tf(`status.severity.action.${severity}`)}
          note={translationUnavailable}
        />
      </p>
      <p className="severity__reading-time">
        {t('status.readingTime')}: {formatInCommunityZone(readingTimestamp, language)}
      </p>
      {readingStale && (
        <p className="severity__stale" role="status" aria-live="polite">
          {t('status.readingStale')} — {t('status.readingAge')}:{' '}
          {ageMinutes(readingTimestamp)} min
        </p>
      )}
    </section>
  );
}
