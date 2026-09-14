/**
 * AuthStatus.tsx — signed-in identity + sign-out control for the brand bar.
 *
 * Shows the current user's email and their role badge(s) when signed in, plus
 * a Sign out button. When signed out it shows nothing (the RouteGuard renders
 * the sign-in form on the guarded surfaces). Re-checks the session whenever the
 * route changes so it reflects a fresh login/logout without a manual reload.
 */

import { useEffect, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { type AuthUser, getCurrentUser, signOut } from './auth';

export function AuthStatus() {
  const { pathname } = useLocation();
  const [user, setUser] = useState<AuthUser | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let active = true;
    (async () => {
      const u = await getCurrentUser();
      if (active) setUser(u);
    })();
    return () => {
      active = false;
    };
  }, [pathname]);

  async function handleSignOut() {
    setBusy(true);
    try {
      await signOut();
    } finally {
      setUser(null);
      setBusy(false);
      // Reload so every guard/hook re-evaluates against the cleared session.
      window.location.assign('/status');
    }
  }

  if (!user) {
    return null;
  }

  return (
    <div className="auth-status">
      <span className="auth-status__user" title={user.username}>
        {user.username}
      </span>
      {user.groups.map((g) => (
        <span key={g} className="auth-status__role">
          {g === 'coordinators' ? 'Coordinator' : 'Responder'}
        </span>
      ))}
      <button
        type="button"
        className="auth-status__signout"
        onClick={handleSignOut}
        disabled={busy}
      >
        {busy ? 'Signing out…' : 'Sign out'}
      </button>
    </div>
  );
}
