/**
 * coordinator/useCoordinatorChat.ts — state + submission for the
 * Coordinator_Orchestrator conversational panel (Req 12.11).
 *
 * Responsibilities:
 *  - Hold the chat transcript in *local* component state only. Nothing here is
 *    shared with Decision_Inbox or the open-incident list, and no realtime
 *    subscription is opened, so a chat failure or an unavailable orchestrator
 *    can never break those other views (Req 12.11 — the panel is a secondary,
 *    isolated surface).
 *  - Send the coordinator's message to the single mode-dispatching AgentCore
 *    entrypoint with `mode=chat` (Design §3.8; the entrypoint routes `chat` to
 *    `run_coordinator_chat()`), append the returned assistant reply, and keep
 *    the input re-enabled between turns.
 *  - Bound every request with `AbortSignal.timeout` (Design §3.8 convention)
 *    and, on any failure, surface an error turn and re-enable the input for
 *    retry rather than leaving the panel stuck (Req 12.11).
 *
 * The orchestrator is read-only over incident/dispatch/alert data (Req 4.10)
 * and may return an unroutable-request indication (Req 4.11); both are ordinary
 * assistant replies to this panel and need no special handling beyond display.
 */

import { useCallback, useRef, useState } from 'react';
import type { ChatTurn } from './types';

/** Request timeout for a chat turn (Design §3.8, matching the console convention). */
const CHAT_TIMEOUT_MS = 30_000;

/** Console API base URL for the chat entrypoint (Design §3.8, §4.4). */
const API_BASE = (import.meta.env?.VITE_CONSOLE_API_BASE as string | undefined) ?? '';

/** Generate a stable-enough local turn id (transcript is display-only). */
function turnId(): string {
  const rand = Math.random().toString(36).slice(2, 10);
  return `${Date.now().toString(36)}-${rand}`;
}

/**
 * Pull the assistant reply text out of the entrypoint response. The `chat` mode
 * returns the orchestrator's message; accept a couple of shapes so the hook is
 * robust to the exact envelope (`{ reply }`, `{ message }`, `{ text }`, or a
 * bare string) without silently rendering an empty bubble.
 */
function extractReply(payload: unknown): string | null {
  if (typeof payload === 'string') {
    return payload.trim() === '' ? null : payload;
  }
  if (payload && typeof payload === 'object') {
    const obj = payload as Record<string, unknown>;
    for (const key of ['reply', 'message', 'text', 'response'] as const) {
      const value = obj[key];
      if (typeof value === 'string' && value.trim() !== '') {
        return value;
      }
    }
  }
  return null;
}

export interface CoordinatorChatState {
  /** The chat transcript, oldest turn first; local to the panel only (Req 12.11). */
  turns: ChatTurn[];
  /** True while a chat turn is in flight; the input is disabled meanwhile. */
  sending: boolean;
  /**
   * Send one coordinator message. Appends the coordinator turn immediately,
   * then the orchestrator's reply (or an error turn on failure/timeout), and
   * always re-enables the input (Req 12.11). A blank/whitespace message and a
   * re-entrant send while one is already in flight are both ignored.
   */
  send: (message: string) => Promise<void>;
}

/**
 * Acquire and maintain the conversational-panel state. Deliberately holds no
 * cross-view state and opens no subscription, so its failure is contained to
 * this surface (Req 12.11).
 */
export function useCoordinatorChat(): CoordinatorChatState {
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [sending, setSending] = useState(false);
  const inFlight = useRef(false);

  const send = useCallback(async (message: string) => {
    const text = message.trim();
    // Ignore empty input and re-entrant sends (one turn at a time).
    if (text === '' || inFlight.current) {
      return;
    }
    inFlight.current = true;
    setSending(true);

    setTurns((prev) => [
      ...prev,
      { id: turnId(), role: 'coordinator', text },
    ]);

    try {
      const res = await fetch(`${API_BASE}/invocations`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: 'chat', message: text }),
        signal: AbortSignal.timeout(CHAT_TIMEOUT_MS),
      });
      if (!res.ok) {
        throw new Error(`status ${res.status}`);
      }
      const reply = extractReply(await res.json());
      if (reply === null) {
        throw new Error('empty reply');
      }
      setTurns((prev) => [
        ...prev,
        { id: turnId(), role: 'orchestrator', text: reply },
      ]);
    } catch {
      // Contain the failure to this panel: show an error turn and re-enable the
      // input for retry. The other console views are untouched (Req 12.11).
      setTurns((prev) => [
        ...prev,
        { id: turnId(), role: 'orchestrator', text: 'CHAT_UNAVAILABLE', isError: true },
      ]);
    } finally {
      inFlight.current = false;
      setSending(false);
    }
  }, []);

  return { turns, sending, send };
}
