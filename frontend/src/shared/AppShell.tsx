/**
 * AppShell.tsx — the branded flood-theme chrome shared by every surface.
 *
 * Renders a fixed top brand bar (logo + wordmark + surface nav) above the
 * routed content, so the three surfaces read as one product. Purely
 * presentational; it wraps the router outlet.
 */

import { type ReactNode, useEffect, useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { AuthStatus } from './AuthStatus';

/** A small inline water-drop / wave mark so we need no image asset. */
function WaveMark() {
  return (
    <svg
      className="brand__mark"
      width="30"
      height="30"
      viewBox="0 0 32 32"
      aria-hidden="true"
    >
      <path
        d="M16 2c6 7 10 11.5 10 17a10 10 0 0 1-20 0C6 13.5 10 9 16 2Z"
        fill="url(#g)"
      />
      <defs>
        <linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#7dd3fc" />
          <stop offset="1" stopColor="#0284c7" />
        </linearGradient>
      </defs>
    </svg>
  );
}

const NAV = [
  { to: '/status', label: 'Public Status' },
  { to: '/console/inbox', label: 'Coordinator' },
  { to: '/responder/assignments', label: 'Responder' },
];

export function AppShell({ children }: { children: ReactNode }) {
  const { pathname } = useLocation();
  const [theme, setTheme] = useState<'light' | 'dark'>(
    () => (localStorage.getItem('thunai-theme') as 'light' | 'dark') || 'light',
  );

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('thunai-theme', theme);
  }, [theme]);

  return (
    <div className="app-shell">
      {/* Ambient rain overlay (decorative, non-interactive). */}
      <div className="rain" aria-hidden="true">
        {Array.from({ length: 70 }).map((_, i) => (
          <span
            key={i}
            className="rain__drop"
            style={{
              left: `${(i * 1.45 + (i % 7)) % 100}%`,
              animationDelay: `${(i % 14) * 0.22}s`,
              animationDuration: `${0.55 + (i % 6) * 0.1}s`,
              opacity: 0.35 + (i % 4) * 0.15,
            }}
          />
        ))}
      </div>
      {/* Rising-water band pinned to the bottom of the viewport. */}
      <div className="floodline" aria-hidden="true">
        <div className="floodline__wave floodline__wave--back" />
        <div className="floodline__wave floodline__wave--front" />
      </div>

      <header className="brand-bar">
        <Link to="/status" className="brand" aria-label="ThunAI home">
          <WaveMark />
          <span className="brand__name">
            Thun<span className="brand__ai">AI</span>
          </span>
          <span className="brand__tag">Flood Response</span>
        </Link>
        <nav className="brand-nav" aria-label="Surfaces">
          {NAV.map((item) => {
            const active = pathname.startsWith(item.to.split('/').slice(0, 2).join('/'));
            return (
              <Link
                key={item.to}
                to={item.to}
                className={active ? 'brand-nav__link brand-nav__link--active' : 'brand-nav__link'}
              >
                {item.label}
              </Link>
            );
          })}
          <button
            type="button"
            className="theme-toggle"
            onClick={() => setTheme((cur) => (cur === 'light' ? 'dark' : 'light'))}
            aria-label={theme === 'light' ? 'Switch to night mode' : 'Switch to day mode'}
            title={theme === 'light' ? 'Night mode' : 'Day mode'}
          >
            {theme === 'light' ? 'Night' : 'Day'}
          </button>
          <AuthStatus />
        </nav>
      </header>
      <div className="app-shell__body">{children}</div>
    </div>
  );
}
