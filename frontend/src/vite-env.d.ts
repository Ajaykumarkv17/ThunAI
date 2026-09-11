/// <reference types="vite/client" />

/**
 * Typed environment variables read by the frontend. Vite injects `import.meta.env`
 * at build time; declaring the shape here keeps access type-safe under `strict`.
 *
 * The values are supplied by Amplify Hosting from `infra/stacks/frontend_stack.py`.
 */
interface ImportMetaEnv {
  /** Cognito user pool id backing the two authenticated surfaces. */
  readonly VITE_COGNITO_USER_POOL_ID?: string;
  /** Cognito user pool app client id used by Amplify Auth. */
  readonly VITE_COGNITO_APP_CLIENT_ID?: string;
  /** AWS region hosting the Cognito pool and AppSync Events API. */
  readonly VITE_AWS_REGION?: string;
  /** AppSync Events HTTP endpoint (publish / query). */
  readonly VITE_APPSYNC_HTTP_ENDPOINT?: string;
  /** AppSync Events realtime (WebSocket) endpoint (subscribe). */
  readonly VITE_APPSYNC_REALTIME_ENDPOINT?: string;
  /** Escalation_Service API base URL for one-tap coordinator responses. */
  readonly VITE_ESCALATION_API_URL?: string;
  /** Base URL of the public read API serving the aggregate resident status. */
  readonly VITE_STATUS_API_BASE?: string;
  /** IANA time zone used to render reading timestamps (Req 14.1). */
  readonly VITE_COMMUNITY_TIME_ZONE?: string;
  /** Responder API base URL for assignment fetch + status submission. */
  readonly VITE_RESPONDER_API_BASE?: string;
  /** Coordinator Console API base URL (inbox, incidents, audit, runs, chat). */
  readonly VITE_CONSOLE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
