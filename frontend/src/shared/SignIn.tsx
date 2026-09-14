/**
 * shared/SignIn.tsx — minimal Cognito sign-in form.
 *
 * Shown by RouteGuard when a visitor has no session. Uses the existing
 * `signIn` helper (aws-amplify/auth). On success it calls `onSignedIn` so the
 * guard re-checks the session and renders the protected surface. Handles the
 * NEW_PASSWORD_REQUIRED challenge that a freshly created Cognito user may hit.
 */

import { type FormEvent, useState } from 'react';
import { confirmSignIn, signIn } from './auth';

export interface SignInProps {
  /** Which surface the login unlocks, for the heading text. */
  surface: 'Coordinator' | 'Responder';
  /** Called after a fully-complete sign in so the caller can re-check auth. */
  onSignedIn: () => void;
}

export function SignIn({ surface, onSignedIn }: SignInProps) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [needsNewPassword, setNeedsNewPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (needsNewPassword) {
        const res = await confirmSignIn({ challengeResponse: newPassword });
        if (res.isSignedIn) {
          onSignedIn();
          return;
        }
      }
      const res = await signIn({ username: email, password });
      if (res.isSignedIn) {
        onSignedIn();
        return;
      }
      if (res.nextStep?.signInStep === 'CONFIRM_SIGN_IN_WITH_NEW_PASSWORD_REQUIRED') {
        setNeedsNewPassword(true);
        setError('A new password is required for this account. Set one below.');
        return;
      }
      setError(`Additional step required: ${res.nextStep?.signInStep ?? 'unknown'}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sign in failed.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <main role="main" style={{ maxWidth: 360, margin: '3rem auto', fontFamily: 'system-ui' }}>
      <h1>{surface} sign in</h1>
      <form onSubmit={handleSubmit} aria-label={`${surface} sign in`}>
        <label style={{ display: 'block', marginBottom: 8 }}>
          Email
          <input
            type="email"
            autoComplete="username"
            value={email}
            onChange={(ev) => setEmail(ev.target.value)}
            required
            style={{ width: '100%', padding: 8, marginTop: 4 }}
          />
        </label>
        <label style={{ display: 'block', marginBottom: 8 }}>
          Password
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(ev) => setPassword(ev.target.value)}
            required
            style={{ width: '100%', padding: 8, marginTop: 4 }}
          />
        </label>
        {needsNewPassword && (
          <label style={{ display: 'block', marginBottom: 8 }}>
            New password
            <input
              type="password"
              autoComplete="new-password"
              value={newPassword}
              onChange={(ev) => setNewPassword(ev.target.value)}
              required
              style={{ width: '100%', padding: 8, marginTop: 4 }}
            />
          </label>
        )}
        <button type="submit" disabled={busy} style={{ padding: '8px 16px', marginTop: 8 }}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
      {error && (
        <p role="alert" style={{ color: '#b00', marginTop: 12 }}>
          {error}
        </p>
      )}
    </main>
  );
}
