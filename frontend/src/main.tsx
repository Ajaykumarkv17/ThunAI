/**
 * main.tsx — application entry for the ThunAI frontend.
 *
 * Composes the three surfaces (Design §3.8) into a single router:
 *   - Resident_Status_Page   (public, no guard)               — residentRoutes()
 *   - Coordinator_Console     (coordinators group, guarded)    — coordinatorRoutes()
 *   - Responder_Interface     (responders group, guarded)      — responderRoutes()
 *
 * At startup Amplify is configured from the `VITE_*` env vars Amplify Hosting
 * injects (see infra/stacks/frontend_stack.py): the Cognito user pool + app
 * client back auth, and the AppSync Events HTTP/realtime endpoints back the
 * live subscriptions used by shared/realtime.ts. Configuration is best-effort —
 * when the env vars are absent (e.g. a bare local `vite build`/preview) the app
 * still mounts and serves, so the surfaces remain viewable without a backend.
 */

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { Amplify } from 'aws-amplify';
import { BrowserRouter, Link, Route, Routes } from 'react-router-dom';
import { residentRoutes } from './resident';
import { coordinatorRoutes } from './coordinator';
import { responderRoutes } from './responder';

/**
 * Configure Amplify from build-time env vars. Only the sections whose required
 * values are present are populated, so a partial or empty environment never
 * throws — the render proceeds regardless (calls into Auth/Events simply fail
 * gracefully at use time, which the surfaces already handle).
 */
function configureAmplify() {
  const env = import.meta.env;
  const region = env.VITE_AWS_REGION;
  const userPoolId = env.VITE_COGNITO_USER_POOL_ID;
  const userPoolClientId = env.VITE_COGNITO_APP_CLIENT_ID;
  const eventsEndpoint = env.VITE_APPSYNC_HTTP_ENDPOINT;

  const config: Record<string, unknown> = {};

  if (userPoolId && userPoolClientId) {
    config.Auth = {
      Cognito: {
        userPoolId,
        userPoolClientId,
      },
    };
  }

  if (eventsEndpoint) {
    config.API = {
      Events: {
        endpoint: eventsEndpoint,
        region,
        defaultAuthMode: 'userPool',
      },
    };
  }

  if (Object.keys(config).length === 0) {
    // Nothing to configure — leave Amplify unconfigured so the app can still
    // mount and be served locally.
    return;
  }

  try {
    Amplify.configure(config as Parameters<typeof Amplify.configure>[0]);
  } catch {
    // A malformed config must not prevent the app from mounting.
  }
}

/** Fallback view for any unmatched path. */
function NotFound() {
  return (
    <main role="main">
      <h1>Page not found</h1>
      <p>The page you requested does not exist.</p>
      <nav aria-label="Surfaces">
        <ul>
          <li>
            <Link to="/status">Resident status</Link>
          </li>
          <li>
            <Link to="/console">Coordinator console</Link>
          </li>
          <li>
            <Link to="/responder">Responder interface</Link>
          </li>
        </ul>
      </nav>
    </main>
  );
}

configureAmplify();

const container = document.getElementById('root');
if (!container) {
  throw new Error('Root element #root not found');
}

createRoot(container).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        {residentRoutes()}
        {coordinatorRoutes()}
        {responderRoutes()}
        <Route path="*" element={<NotFound />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);
