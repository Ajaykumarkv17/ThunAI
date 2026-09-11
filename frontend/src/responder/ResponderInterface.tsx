/**
 * responder/ResponderInterface.tsx — the Responder_Interface view (Req 13.1–13.10).
 *
 * Presents the authenticated responder's own current assignment(s) with the
 * fields carried in the assignment notification — request id, location,
 * occupant count, mobility-assistance / medical-need indicators, equipment
 * requirement, and the acknowledgement deadline (Req 13.1, 13.2). Every status
 * control permitted by the current assignment state is reachable in a single
 * activation (Req 13.2); controls not permitted by the current state are
 * disabled, so accept/decline appear only while awaiting acknowledgement,
 * en-route only after accept, on-scene only after en-route, and completed only
 * after on-scene (Req 13.2, Design §4.3).
 *
 * A decline frees the responder and re-dispatches the request (Req 13.3); a
 * completed response routes the request into verification (Req 13.4) — both are
 * handled server-side, this view only submits and reflects the returned state
 * over realtime. If the responder submits a response the state does not permit
 * and the server rejects it, the interface shows an error naming the permitted
 * responses (Req 13.9). Access is restricted to the responders group and only
 * the responder's own assignments are shown; anything else is denied with an
 * authorisation indication and no assignment content (Req 13.6).
 *
 * Accessibility: semantic landmarks/headings, real `<button>` controls,
 * `aria-live` on the countdown and error regions, and `aria-busy` while a
 * submission is in flight. Full WCAG 2.1 AA conformance still requires manual
 * assistive-technology testing.
 */

import { useI18n, type I18n } from '../shared/i18n';
import { useResponderAssignments, type ResponderError } from './useResponderAssignments';
import {
  PERMITTED_ACTIONS,
  type Assignment,
  type ResponderAction,
} from './types';

/** The status controls, in lifecycle order, that the interface renders. */
const ACTION_ORDER: readonly ResponderAction[] = [
  'accept',
  'decline',
  'en-route',
  'on-scene',
  'completed',
];

/** i18n key for a control label. */
function actionLabelKey(action: ResponderAction): string {
  return `responder.action.${action}`;
}

export function ResponderInterface() {
  const i18n = useI18n();
  const { t } = i18n;
  const {
    assignments,
    loading,
    unauthorised,
    submitting,
    errors,
    connectionStale,
    submitAction,
  } = useResponderAssignments();

  // Req 13.6: a non-responder (unauthenticated or wrong group) sees an
  // authorisation indication and no assignment content whatsoever.
  if (unauthorised) {
    return (
      <main className="responder">
        <p className="responder__denied" role="alert">
          {t('responder.unauthorised')}
        </p>
      </main>
    );
  }

  return (
    <main className="responder">
      <header className="responder__header">
        <h1>{t('responder.title')}</h1>
      </header>

      {connectionStale && (
        <p className="responder__stale" role="status" aria-live="polite">
          {t('responder.connectionStale')}
        </p>
      )}

      {loading && assignments.length === 0 ? (
        <p role="status" aria-live="polite">
          {t('responder.loading')}
        </p>
      ) : assignments.length === 0 ? (
        <p className="responder__empty">{t('responder.empty')}</p>
      ) : (
        <ol className="responder__list">
          {assignments.map((assignment) => (
            <li key={assignment.assignmentId}>
              <AssignmentCard
                assignment={assignment}
                disabled={Boolean(submitting[assignment.assignmentId])}
                error={errors[assignment.assignmentId]}
                onAction={submitAction}
                i18n={i18n}
              />
            </li>
          ))}
        </ol>
      )}
    </main>
  );
}

/** One assignment with its state-gated status controls (Req 13.1, 13.2, 13.9). */
function AssignmentCard({
  assignment,
  disabled,
  error,
  onAction,
  i18n,
}: {
  assignment: Assignment;
  disabled: boolean;
  error: ResponderError | undefined;
  onAction: (assignmentId: string, action: ResponderAction) => void;
  i18n: I18n;
}) {
  const { t } = i18n;
  const permitted = PERMITTED_ACTIONS[assignment.state];

  return (
    <section
      className="assignment-card"
      aria-labelledby={`asg-${assignment.assignmentId}-request`}
      aria-busy={disabled}
    >
      <h2 id={`asg-${assignment.assignmentId}-request`} className="assignment-card__request">
        {t('responder.request')}: {assignment.requestId}
      </h2>

      {/* Current lifecycle state, announced so a responder always knows where the
          assignment stands (Req 13.2). */}
      <p className="assignment-card__state" role="status" aria-live="polite">
        {t('responder.state')}: {t(`responder.state.${assignment.state}`)}
      </p>

      {/* Assignment fields carried from the notification (Req 13.1, 13.2). */}
      <dl className="assignment-card__details">
        <dt>{t('responder.location')}</dt>
        <dd>{assignment.locationReference}</dd>
        <dt>{t('responder.occupants')}</dt>
        <dd>{assignment.occupantCount}</dd>
        <dt>{t('responder.mobility')}</dt>
        <dd>{assignment.mobilityAssistance ? t('responder.yes') : t('responder.no')}</dd>
        <dt>{t('responder.medical')}</dt>
        <dd>{assignment.medicalNeed ? t('responder.yes') : t('responder.no')}</dd>
        <dt>{t('responder.equipment')}</dt>
        <dd>{assignment.equipmentRequirement}</dd>
        <dt>{t('responder.ackDeadline')}</dt>
        <dd>
          <time dateTime={assignment.acknowledgementDeadline}>
            {formatDeadline(assignment.acknowledgementDeadline)}
          </time>
        </dd>
      </dl>

      {/* State-gated controls: every permitted response is reachable in one
          activation; responses not permitted by the current state are disabled
          (Req 13.2). Rendered in lifecycle order so the flow is predictable. */}
      <div
        className="assignment-card__actions"
        role="group"
        aria-label={t('responder.actions')}
      >
        {ACTION_ORDER.map((action) => {
          const allowed = permitted.includes(action);
          return (
            <button
              key={action}
              type="button"
              className={`assignment-card__action assignment-card__action--${action}`}
              // Disabled when the state does not permit it, or while a submit is
              // in flight so a single activation yields at most one response.
              disabled={!allowed || disabled}
              aria-disabled={!allowed || disabled}
              onClick={() => onAction(assignment.assignmentId, action)}
            >
              {disabled && allowed ? t('responder.submitting') : t(actionLabelKey(action))}
            </button>
          );
        })}
      </div>

      {/* Error indication. An invalid-transition rejection names the permitted
          responses from the current state (Req 13.9); other failures show a
          generic retry message. Controls above are re-enabled for retry. */}
      {error && (
        <p className="assignment-card__error" role="alert" aria-live="assertive">
          {error.kind === 'invalid_transition'
            ? `${t('responder.invalidTransition')} ${error.permitted
                .map((a) => t(actionLabelKey(a)))
                .join(', ') || t('responder.noneAllowed')}`
            : t('responder.submitError')}
        </p>
      )}
    </section>
  );
}

/** Render an ISO 8601 deadline in the visitor's locale; fall back to the raw string. */
function formatDeadline(iso: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) {
    return iso;
  }
  return parsed.toLocaleString();
}
