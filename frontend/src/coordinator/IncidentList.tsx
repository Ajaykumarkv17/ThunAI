/**
 * coordinator/IncidentList.tsx — open-incident list + shelter availability view
 * (Req 12.4).
 *
 * Lists every open incident ordered by severity band descending
 * (EVACUATE > WARNING > WATCH > NORMAL) then most recently updated first
 * (Req 12.4). Each incident row shows its severity band, affected areas, the
 * count of open requests, and the count of assigned responders. A ward-wide
 * shelter panel shows each shelter's unoccupied places out of its total
 * capacity.
 *
 * This is a read-only view: it presents state only and carries no mutating
 * controls (mutating one-tap decisions live in Decision_Inbox — Req 12.1–12.3).
 * The list is kept current by AppSync Events (Req 12.5); a staleness indicator
 * is shown when live updates go silent (Req 12.6).
 *
 * Accessibility (Req 12.12): semantic landmarks (`<main>`, `<section>`,
 * headings), a definition list for per-incident counts, and `aria-live` on the
 * loading/staleness regions. Full WCAG 2.1 AA conformance still requires manual
 * assistive-technology testing; this component supplies the structural
 * foundation for it.
 */

import { useI18n, type I18n } from '../shared/i18n';
import { useOpenIncidents } from './useOpenIncidents';
import type { IncidentSummary, ShelterAvailability } from './types';

export function IncidentList() {
  const i18n = useI18n();
  const { t } = i18n;
  const { incidents, loading, connectionStale } = useOpenIncidents();

  // Shelter availability is ward-wide, not per-incident (Req 12.4 lists it on
  // the incident view but shelters are shared): take it from the first row that
  // carries it so the panel renders once.
  const shelters = incidents.find((i) => i.shelters.length > 0)?.shelters ?? [];

  return (
    <main className="incident-list">
      <header className="incident-list__header">
        <h1>{t('incidents.title')}</h1>
      </header>

      {connectionStale && (
        <p className="incident-list__stale" role="status" aria-live="polite">
          {t('incidents.connectionStale')}
        </p>
      )}

      {loading && incidents.length === 0 ? (
        <p role="status" aria-live="polite">
          {t('incidents.loading')}
        </p>
      ) : incidents.length === 0 ? (
        <p className="incident-list__empty">{t('incidents.empty')}</p>
      ) : (
        <ol className="incident-list__list">
          {incidents.map((incident) => (
            <li key={incident.incidentId}>
              <IncidentCard incident={incident} i18n={i18n} />
            </li>
          ))}
        </ol>
      )}

      <ShelterPanel shelters={shelters} i18n={i18n} />
    </main>
  );
}

/** One open-incident row: severity band, affected areas, and the two counts. */
function IncidentCard({ incident, i18n }: { incident: IncidentSummary; i18n: I18n }) {
  const { t } = i18n;
  const areas =
    incident.affectedAreas.length > 0
      ? incident.affectedAreas.join(', ')
      : t('incidents.affectedAreas.none');

  return (
    <section
      className="incident-card"
      aria-labelledby={`incident-${incident.incidentId}-band`}
    >
      <h2 id={`incident-${incident.incidentId}-band`} className="incident-card__band">
        <span className={`incident-card__severity incident-card__severity--${incident.severityBand}`}>
          {t(`incidents.severity.${incident.severityBand}`)}
        </span>
      </h2>

      <dl className="incident-card__details">
        <dt>{t('incidents.affectedAreas')}</dt>
        <dd>{areas}</dd>
        <dt>{t('incidents.openRequests')}</dt>
        <dd>{incident.openRequestCount}</dd>
        <dt>{t('incidents.assignedResponders')}</dt>
        <dd>{incident.assignedResponderCount}</dd>
      </dl>
    </section>
  );
}

/** Ward-wide shelter availability panel: unoccupied places out of total (Req 12.4). */
function ShelterPanel({
  shelters,
  i18n,
}: {
  shelters: ShelterAvailability[];
  i18n: I18n;
}) {
  const { t } = i18n;

  return (
    <section className="incident-list__shelters" aria-labelledby="incident-shelters-title">
      <h2 id="incident-shelters-title">{t('incidents.shelters.title')}</h2>

      {shelters.length === 0 ? (
        <p className="incident-list__shelters-empty">{t('incidents.shelters.none')}</p>
      ) : (
        <ul className="incident-list__shelter-list">
          {shelters.map((shelter) => (
            <li key={shelter.shelterId} className="shelter-availability">
              <span className="shelter-availability__name">{shelter.name}</span>
              <span className="shelter-availability__places">
                {t('incidents.shelters.available')}: {shelter.unoccupiedPlaces} /{' '}
                {shelter.totalCapacity}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
