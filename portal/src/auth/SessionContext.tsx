import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { bearerAuth } from '../api/auth';
import { ApiError, createStatsClient } from '../api/client';
import {
  clearSession,
  isValidKeyFormat,
  loadSession,
  saveSession,
  type Session,
} from './session';

export type SignInResult = 'ok' | 'format' | 'invalid' | 'error';
export type SessionNotice = 'expired' | null;

export interface SessionValue {
  /** null = signed out (show the sign-in screen). */
  session: Session | null;
  /** Set when a tenant session was ended by a 401 from the API. */
  notice: SessionNotice;
  signInTenant(key: string): Promise<SignInResult>;
  signInOperator(): void;
  signOut(reason?: 'expired'): void;
}

/** Default (no provider): operator mode, as before sign-in existed. Lets isolated components render in tests. */
const DEFAULT_VALUE: SessionValue = {
  session: { mode: 'operator' },
  notice: null,
  signInTenant: async () => 'error',
  signInOperator: () => undefined,
  signOut: () => undefined,
};

const SessionContext = createContext<SessionValue>(DEFAULT_VALUE);

export function SessionProvider({
  children,
  fetchImpl,
  initial,
}: {
  children: ReactNode;
  fetchImpl?: typeof fetch;
  /** Tests may seed the session; otherwise it is read from sessionStorage. */
  initial?: Session | null;
}) {
  const [session, setSession] = useState<Session | null>(() =>
    initial !== undefined ? initial : loadSession(),
  );
  const [notice, setNotice] = useState<SessionNotice>(null);

  const signInTenant = useCallback(
    async (rawKey: string): Promise<SignInResult> => {
      const key = rawKey.trim();
      if (!isValidKeyFormat(key)) return 'format';
      const client = createStatsClient({ auth: bearerAuth(() => key), fetchImpl });
      try {
        const profile = await client.me();
        const next: Session = { mode: 'tenant', key, profile };
        saveSession(next);
        setSession(next);
        setNotice(null);
        return 'ok';
      } catch (err) {
        // Only the status matters; the key and the server's detail are never surfaced or logged.
        return err instanceof ApiError && err.status === 401 ? 'invalid' : 'error';
      }
    },
    [fetchImpl],
  );

  const signInOperator = useCallback(() => {
    const next: Session = { mode: 'operator' };
    saveSession(next);
    setSession(next);
    setNotice(null);
  }, []);

  const signOut = useCallback((reason?: 'expired') => {
    clearSession();
    setSession(null);
    setNotice(reason ?? null);
  }, []);

  const value = useMemo<SessionValue>(
    () => ({ session, notice, signInTenant, signInOperator, signOut }),
    [session, notice, signInTenant, signInOperator, signOut],
  );
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionValue {
  return useContext(SessionContext);
}
