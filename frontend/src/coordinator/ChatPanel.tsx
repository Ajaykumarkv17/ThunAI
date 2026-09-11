/**
 * coordinator/ChatPanel.tsx — the Coordinator_Orchestrator conversational panel
 * (Req 12.11), a *secondary* surface of Coordinator_Console.
 *
 * The panel sends the coordinator's message to the mode-dispatching AgentCore
 * entrypoint with `mode=chat` (Design §3.8) and renders the returned reply. It
 * is deliberately isolated: its own route (`/console/chat`), its own local
 * state, no realtime subscription, and no shared state with Decision_Inbox or
 * the open-incident list. A chat failure is contained here — it surfaces an
 * error turn and re-enables the input — so Decision_Inbox and the open-incident
 * list remain fully operable even when this panel is unavailable (Req 12.11).
 *
 * Accessibility (Req 12.12): a semantic `<main>` landmark, a heading, an
 * `aria-live="polite"` transcript log so new turns are announced, real
 * `<form>`/`<button>` controls with a labelled `<textarea>`, `aria-busy` while
 * a turn is in flight, and error turns announced via `role="alert"`. Full WCAG
 * 2.1 AA conformance still requires manual assistive-technology testing; this
 * component supplies the structural foundation for it.
 */

import { useState, type FormEvent } from 'react';
import { useI18n, type I18n } from '../shared/i18n';
import { useCoordinatorChat } from './useCoordinatorChat';
import type { ChatTurn } from './types';

export function ChatPanel() {
  const i18n = useI18n();
  const { t } = i18n;
  const { turns, sending, send } = useCoordinatorChat();
  const [draft, setDraft] = useState('');

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    const message = draft.trim();
    if (message === '' || sending) {
      return;
    }
    // Clear the input immediately; the hook appends the coordinator turn.
    setDraft('');
    void send(message);
  };

  return (
    <main className="chat-panel" aria-busy={sending}>
      <header className="chat-panel__header">
        <h1>{t('chat.title')}</h1>
        <p className="chat-panel__intro">{t('chat.intro')}</p>
      </header>

      <section
        className="chat-panel__transcript"
        aria-label={t('chat.transcript')}
        aria-live="polite"
      >
        {turns.length === 0 ? (
          <p className="chat-panel__empty">{t('chat.empty')}</p>
        ) : (
          <ol className="chat-panel__turns">
            {turns.map((turn) => (
              <li key={turn.id}>
                <ChatBubble turn={turn} i18n={i18n} />
              </li>
            ))}
          </ol>
        )}
      </section>

      <form className="chat-panel__form" onSubmit={onSubmit}>
        <label className="chat-panel__label" htmlFor="chat-panel-input">
          {t('chat.inputLabel')}
        </label>
        <textarea
          id="chat-panel-input"
          className="chat-panel__input"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder={t('chat.placeholder')}
          rows={2}
          disabled={sending}
        />
        <button
          type="submit"
          className="chat-panel__send"
          disabled={sending || draft.trim() === ''}
        >
          {sending ? t('chat.sending') : t('chat.send')}
        </button>
      </form>
    </main>
  );
}

/** One chat turn. Error turns are announced as alerts rather than replies. */
function ChatBubble({ turn, i18n }: { turn: ChatTurn; i18n: I18n }) {
  const { t } = i18n;
  const speaker =
    turn.role === 'coordinator' ? t('chat.speaker.you') : t('chat.speaker.orchestrator');

  if (turn.isError) {
    return (
      <p className="chat-bubble chat-bubble--error" role="alert" aria-live="assertive">
        {t('chat.error')}
      </p>
    );
  }

  return (
    <p className={`chat-bubble chat-bubble--${turn.role}`}>
      <span className="chat-bubble__speaker">{speaker}</span>
      <span className="chat-bubble__text">{turn.text}</span>
    </p>
  );
}
