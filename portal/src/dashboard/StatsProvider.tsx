import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { ApiError } from '../api/client';
import type { StatsEnvelope } from '../api/types';
import { useSession } from '../auth/SessionContext';
import { useFilters } from '../filters/FiltersContext';
import { useStatsClient } from './ApiContext';

export type StatsStatus = 'idle' | 'loading' | 'ready' | 'error';

export interface StatsState {
  status: StatsStatus;
  envelope: StatsEnvelope | null;
  error: ApiError | null;
  reload(): void;
}

const StatsContext = createContext<StatsState | null>(null);

/** Fetches `summary` once per tenant + window; stale requests are aborted. */
export function StatsProvider({ children }: { children: ReactNode }) {
  const client = useStatsClient();
  const { session, signOut } = useSession();
  const tenantMode = session?.mode === 'tenant';
  const { tenantId, window: range } = useFilters();
  const [nonce, setNonce] = useState(0);
  const [result, setResult] = useState<{
    status: StatsStatus;
    envelope: StatsEnvelope | null;
    error: ApiError | null;
  }>({ status: 'idle', envelope: null, error: null });

  const start = range?.start;
  const end = range?.end;

  useEffect(() => {
    if (tenantId === null || start === undefined || end === undefined) {
      setResult({ status: 'idle', envelope: null, error: null });
      return;
    }
    const controller = new AbortController();
    setResult({ status: 'loading', envelope: null, error: null });
    client
      .summary(tenantId, { start, end }, controller.signal)
      .then((envelope) => {
        if (!controller.signal.aborted) setResult({ status: 'ready', envelope, error: null });
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        const error = err instanceof ApiError ? err : new ApiError(0, String(err));
        // A rejected tenant key (revoked/expired) ends the session; the sign-in screen explains why.
        if (error.status === 401 && tenantMode) signOut('expired');
        setResult({ status: 'error', envelope: null, error });
      });
    return () => controller.abort();
  }, [client, tenantId, start, end, nonce, tenantMode, signOut]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  const value = useMemo<StatsState>(() => ({ ...result, reload }), [result, reload]);
  return <StatsContext.Provider value={value}>{children}</StatsContext.Provider>;
}

export function useStats(): StatsState {
  const value = useContext(StatsContext);
  if (!value) throw new Error('useStats must be used inside <StatsProvider>');
  return value;
}
