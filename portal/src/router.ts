import { useEffect, useState } from 'react';

export const ROUTES = ['signal', 'roas', 'audiences', 'onboarding'] as const;
export type Route = (typeof ROUTES)[number];

export function isRoute(value: string): value is Route {
  return (ROUTES as readonly string[]).includes(value);
}

/** Parses "#/roas" (also "#roas") into a route. Unknown or empty values yield null. */
export function parseHash(hash: string): Route | null {
  const path = hash.replace(/^#\/?/, '').replace(/\/+$/, '');
  return isRoute(path) ? path : null;
}

export function hrefFor(route: Route): string {
  return `#/${route}`;
}

/**
 * Minimal hash router. Returns the active route (or null for an unknown hash),
 * falling back to `defaultRoute` when there is no hash at all.
 */
export function useHashRoute(defaultRoute: Route = 'signal'): Route | null {
  const [hash, setHash] = useState(() => window.location.hash);

  useEffect(() => {
    const onChange = () => setHash(window.location.hash);
    window.addEventListener('hashchange', onChange);
    return () => window.removeEventListener('hashchange', onChange);
  }, []);

  if (hash === '' || hash === '#' || hash === '#/') return defaultRoute;
  return parseHash(hash);
}
