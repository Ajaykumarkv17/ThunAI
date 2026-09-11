/**
 * shared/auth.ts — authentication + client-side route guard.
 *
 * Wraps `aws-amplify/auth` (`signIn`, `fetchAuthSession`) and exposes a route
 * guard that checks the authenticated user's Cognito group claim against the
 * group a route requires.
 *
 * The guard runs client-side for UX only. The *actual* authorization boundary
 * is server-side: AppSync Events channel authorization and the API
 * Gateway/Lambda authorizer both re-check the same Cognito group claim, so a
 * client-side bypass discloses nothing (Req 12.9, 12.10, 13.6; Design §3.8).
 */

import { Fragment, type ReactNode, createElement, useEffect, useState } from 'react';
import {
  fetchAuthSession,
  signIn as amplifySignIn,
  signOut as amplifySignOut,
  type SignInInput,
} from 'aws-amplify/auth';

/** Cognito groups that gate the two authenticated surfaces. */
export type UserGroup = 'coordinators' | 'responders';

export interface AuthUser {
  /** Cognito `sub` — the stable authenticated identity. */
  userId: string;
  /** Preferred username / email surfaced by the token, when present. */
  username: string;
  /** Cognito groups the identity belongs to (from `cognito:groups`). */
  groups: UserGroup[];
}

/**
 * Sign in with username + password. Thin pass-through to Amplify so callers
 * import a single auth module rather than reaching into aws-amplify directly.
 */
export async function signIn(input: SignInInput) {
  return amplifySignIn(input);
}

/** Sign the current user out and clear the cached session. */
export async function signOut() {
  return amplifySignOut();
}

/**
 * Resolve the currently authenticated user from the ID token claims, or `null`
 * when there is no valid session. Never throws — an unauthenticated visitor is
 * a normal state, not an error.
 */
export async function getCurrentUser(): Promise<AuthUser | null> {
  try {
    const session = await fetchAuthSession();
    const claims = session.tokens?.idToken?.payload;
    if (!claims || !claims.sub) {
      return null;
    }

    const rawGroups = claims['cognito:groups'];
    const groups = normaliseGroups(rawGroups);

    return {
      userId: String(claims.sub),
      username: String(claims['cognito:username'] ?? claims.email ?? claims.sub),
      groups,
    };
  } catch {
    return null;
  }
}

/** True when the current session carries the given group claim. */
export async function isInGroup(group: UserGroup): Promise<boolean> {
  const user = await getCurrentUser();
  return user !== null && user.groups.includes(group);
}

/** Coerce the `cognito:groups` claim (string | string[] | undefined) to a typed list. */
function normaliseGroups(raw: unknown): UserGroup[] {
  const known: UserGroup[] = ['coordinators', 'responders'];
  const values = Array.isArray(raw)
    ? raw.map(String)
    : typeof raw === 'string'
      ? [raw]
      : [];
  return values.filter((g): g is UserGroup => (known as string[]).includes(g));
}

type GuardStatus = 'checking' | 'authorised' | 'unauthorised';

export interface RouteGuardProps {
  /** Cognito group the wrapped route requires. */
  requiredGroup: UserGroup;
  /** Protected subtree rendered only once authorisation succeeds. */
  children: ReactNode;
  /**
   * Optional override for the denial UI. Receives whether the visitor was
   * unauthenticated (no session) vs. authenticated-but-wrong-group.
   */
  renderDenied?: (reason: 'unauthenticated' | 'wrong_group') => ReactNode;
}

/**
 * Route guard component. While the session is being resolved it discloses
 * nothing; on mismatch it renders an authorisation-required message and never
 * renders `children`, so no incident / request / responder / resident /
 * Escalation_Record content leaks to an unauthorised visitor (Req 12.10).
 */
export function RouteGuard({ requiredGroup, children, renderDenied }: RouteGuardProps) {
  const [status, setStatus] = useState<GuardStatus>('checking');
  const [reason, setReason] = useState<'unauthenticated' | 'wrong_group'>('unauthenticated');

  useEffect(() => {
    let active = true;
    (async () => {
      const user = await getCurrentUser();
      if (!active) return;

      if (!user) {
        setReason('unauthenticated');
        setStatus('unauthorised');
        return;
      }
      if (!user.groups.includes(requiredGroup)) {
        setReason('wrong_group');
        setStatus('unauthorised');
        return;
      }
      setStatus('authorised');
    })();
    return () => {
      active = false;
    };
  }, [requiredGroup]);

  if (status === 'checking') {
    return createElement(
      'div',
      { role: 'status', 'aria-live': 'polite' },
      'Checking authorisation…',
    );
  }

  if (status === 'unauthorised') {
    if (renderDenied) {
      return createElement(Fragment, null, renderDenied(reason));
    }
    const surface = requiredGroup === 'coordinators' ? 'Coordinator' : 'Responder';
    return createElement(
      'div',
      { role: 'alert' },
      `${surface} authorisation is required to view this page.`,
    );
  }

  return createElement(Fragment, null, children);
}
