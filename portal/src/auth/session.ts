import type { TenantProfile } from '../api/types';

/**
 * Portal session storage. The tenant API key lives in `sessionStorage` ONLY: it is cleared when the tab closes and is
 * never written to localStorage, logged, or put in a URL. Every storage call is wrapped: storage may be blocked.
 */
export type Session =
  | { mode: 'operator' }
  | { mode: 'tenant'; key: string; profile: TenantProfile };

export const SESSION_STORAGE_KEY = 'motahai.session';

/** `mtk_` + 8 hex chars + `_` + url-safe secret (secrets.token_urlsafe(32) is 43 chars; be lenient on length). */
export const TENANT_KEY_PATTERN = /^mtk_[0-9a-f]{8}_[A-Za-z0-9_-]{16,128}$/;

export function isValidKeyFormat(key: string): boolean {
  return TENANT_KEY_PATTERN.test(key);
}

function isProfile(value: unknown): value is TenantProfile {
  if (!value || typeof value !== 'object') return false;
  const p = value as Record<string, unknown>;
  return (
    typeof p.tenant_id === 'number' &&
    Number.isSafeInteger(p.tenant_id) &&
    typeof p.name === 'string' &&
    typeof p.currency === 'string' &&
    typeof p.country === 'string' &&
    typeof p.mode === 'string' &&
    typeof p.platform === 'string'
  );
}

function store(): Storage | null {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

/** The stored session, or null when absent/corrupt (a corrupt entry is removed). */
export function loadSession(): Session | null {
  const storage = store();
  if (!storage) return null;
  try {
    const raw = storage.getItem(SESSION_STORAGE_KEY);
    if (!raw) return null;
    const saved = JSON.parse(raw) as { mode?: unknown; key?: unknown; profile?: unknown };
    if (saved.mode === 'operator') return { mode: 'operator' };
    if (saved.mode === 'tenant' && typeof saved.key === 'string' && isValidKeyFormat(saved.key) && isProfile(saved.profile)) {
      return { mode: 'tenant', key: saved.key, profile: saved.profile };
    }
    storage.removeItem(SESSION_STORAGE_KEY);
  } catch {
    // Unreadable entry or blocked storage: treat as signed out.
  }
  return null;
}

export function saveSession(session: Session): void {
  const storage = store();
  if (!storage) return;
  try {
    storage.setItem(SESSION_STORAGE_KEY, JSON.stringify(session));
  } catch {
    // Non-fatal: the session simply won't survive a reload.
  }
}

export function clearSession(): void {
  const storage = store();
  if (!storage) return;
  try {
    storage.removeItem(SESSION_STORAGE_KEY);
  } catch {
    // Nothing more to do.
  }
}
