/**
 * shared/realtime.ts — AppSync Events realtime subscriptions.
 *
 * Uses the `aws-amplify/data` **events** API (`events.connect`,
 * `channel.subscribe`). `stream_publisher_lambda` publishes DynamoDB commits to
 * channels namespaced by entity type (`/incidents/{id}`, `/escalations/{id}`,
 * `/shelters/{id}`); the console subscribes with a wildcard and reflects the
 * change within 5s (Req 12.5). A staleness indicator is raised when no update
 * confirmation arrives for more than 15s (Req 12.6, 14.9; Design §3.8).
 */

import { useEffect, useRef, useState } from 'react';
import { events, type EventsChannel } from 'aws-amplify/data';
import type { DocumentType } from '@aws-amplify/core/internals/utils';

/** No update for longer than this (ms) marks the displayed data stale. Req 12.6. */
const STALENESS_LIMIT_MS = 15_000;
/** How often (ms) the staleness watchdog re-evaluates the last-message age. */
const STALENESS_POLL_MS = 5_000;

/** An event delivered on an entity channel. `data` is the published payload. */
export interface IncidentEvent<T = unknown> {
  /** The channel path the event arrived on, e.g. `/incidents/inc-123`. */
  channel: string;
  /** Deserialized event payload as published by the stream publisher. */
  data: T;
}

export interface RealtimeState {
  /** True when no update has been confirmed within the staleness limit. */
  stale: boolean;
  /** Age in whole seconds of the most recently confirmed update. Req 12.6 / 14.9. */
  ageSeconds: number;
}

/**
 * Subscribe to a wildcard channel and invoke `onUpdate` for each event.
 *
 * @param channelPath wildcard channel, e.g. `/incidents/*`, `/shelters/*`.
 * @param onUpdate    called with each event; keep it stable (useCallback) to
 *                    avoid re-subscribing on every render.
 */
export function useIncidentUpdates<T = unknown>(
  channelPath: string,
  onUpdate: (evt: IncidentEvent<T>) => void,
): RealtimeState {
  const [stale, setStale] = useState(false);
  const [ageSeconds, setAgeSeconds] = useState(0);
  const lastMessageAt = useRef<number>(Date.now());
  const onUpdateRef = useRef(onUpdate);
  onUpdateRef.current = onUpdate;

  useEffect(() => {
    let closed = false;
    let channel: EventsChannel | undefined;

    lastMessageAt.current = Date.now();
    setStale(false);
    setAgeSeconds(0);

    (async () => {
      try {
        channel = await events.connect(channelPath);
        if (closed) {
          channel.close();
          return;
        }
        channel.subscribe({
          next: (data) => {
            lastMessageAt.current = Date.now();
            setStale(false);
            setAgeSeconds(0);
            onUpdateRef.current({ channel: channelPath, data: data as T });
          },
          // A subscription error means we are no longer receiving confirmed
          // updates — surface staleness immediately (Req 12.6).
          error: () => setStale(true),
        });
      } catch {
        setStale(true);
      }
    })();

    // Watchdog: recompute the age of the displayed data and flag staleness
    // once it exceeds the limit. Continues attempting to resume via the live
    // subscription; the timer only reports age, it never tears the channel down.
    const staleTimer = setInterval(() => {
      const age = Date.now() - lastMessageAt.current;
      setAgeSeconds(Math.floor(age / 1000));
      if (age > STALENESS_LIMIT_MS) {
        setStale(true);
      }
    }, STALENESS_POLL_MS);

    return () => {
      closed = true;
      clearInterval(staleTimer);
      channel?.close();
    };
  }, [channelPath]);

  return { stale, ageSeconds };
}

/**
 * One-shot publish helper (used by tests / tools that need to emit an event).
 * Frontend surfaces generally consume rather than publish, but exposing this
 * keeps all Events-API access behind this module.
 */
export async function publish(channelPath: string, payload: unknown) {
  return events.post(channelPath, payload as DocumentType);
}
