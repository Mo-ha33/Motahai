import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { ApiError, type StatsClient } from '../api/client';
import type { SourceEnvelopeMap, StatsSourceId, StatsWindow } from '../api/types';
import { useSession } from '../auth/SessionContext';
import { useFilters } from '../filters/FiltersContext';
import { useStatsClient } from './ApiContext';

export type StatsStatus = 'idle' | 'loading' | 'ready' | 'error';

/** State of one data source. `envelope` is set only when `status` is 'ready'. */
export interface SourceState<S extends StatsSourceId = StatsSourceId> {
  status: StatsStatus;
  envelope: SourceEnvelopeMap[S] | null;
  error: ApiError | null;
}

export interface StatsState {
  /** Summary state, kept for summary-only consumers. */
  status: StatsStatus;
  envelope: SourceEnvelopeMap['summary'] | null;
  error: ApiError | null;
  /** Per-source state for every source the page declared (empty while idle). */
  sources: Partial<{ [S in StatsSourceId]: SourceState<S> }>;
  reload(): void;
}

/** Adding a source: add its id to `StatsSourceId`, a client method, and one entry here. */
const FETCHERS: {
  [S in StatsSourceId]: (
    client: StatsClient,
    tenantId: number | string,
    window: StatsWindow,
    signal: AbortSignal,
  ) => Promise<SourceEnvelopeMap[S]>;
} = {
  summary: (client, tenantId, window, signal) => client.summary(tenantId, window, signal),
  'health-score': (client, tenantId, window, signal) => client.healthScore(tenantId, window, signal),
};

const IDLE: SourceState = { status: 'idle', envelope: null, error: null };

const StatsContext = createContext<StatsState | null>(null);

/**
 * Fetches the page's sources (default `summary`) in parallel once per tenant + window under one abort controller.
 * Each source has its own state, so one failing source never blanks widgets fed by another.
 */
export function StatsProvider({
  children,
  sources: requested = ['summary'],
}: {
  children: ReactNode;
  sources?: StatsSourceId[];
}) {
  const client = useStatsClient();
  const { session, signOut } = useSession();
  const tenantMode = session?.mode === 'tenant';
  const { tenantId, window: range } = useFilters();
  const [nonce, setNonce] = useState(0);
  const [results, setResults] = useState<Partial<Record<StatsSourceId, SourceState>>>({});

  const start = range?.start;
  const end = range?.end;
  const sourceKey = [...new Set(requested)].join(',');

  useEffect(() => {
    const ids = (sourceKey ? sourceKey.split(',') : ['summary']) as StatsSourceId[];
    if (tenantId === null || start === undefined || end === undefined) {
      setResults({});
      return;
    }
    const controller = new AbortController();
    const loading: Partial<Record<StatsSourceId, SourceState>> = {};
    for (const id of ids) loading[id] = { status: 'loading', envelope: null, error: null };
    setResults(loading);

    for (const id of ids) {
      (FETCHERS[id] as (...args: Parameters<(typeof FETCHERS)['summary']>) => Promise<unknown>)(
        client,
        tenantId,
        { start, end },
        controller.signal,
      )
        .then((envelope) => {
          if (controller.signal.aborted) return;
          setResults((prev) => ({
            ...prev,
            [id]: { status: 'ready', envelope, error: null } as SourceState,
          }));
        })
        .catch((err: unknown) => {
          if (controller.signal.aborted) return;
          const error = err instanceof ApiError ? err : new ApiError(0, String(err));
          // A rejected tenant key (revoked/expired) ends the session; the sign-in screen explains why.
          if (error.status === 401 && tenantMode) signOut('expired');
          setResults((prev) => ({ ...prev, [id]: { status: 'error', envelope: null, error } }));
        });
    }
    return () => controller.abort();
  }, [client, tenantId, start, end, nonce, tenantMode, signOut, sourceKey]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  const value = useMemo<StatsState>(() => {
    const summary = (results.summary ?? IDLE) as SourceState<'summary'>;
    return {
      status: summary.status,
      envelope: summary.envelope,
      error: summary.error,
      sources: results as StatsState['sources'],
      reload,
    };
  }, [results, reload]);

  return <StatsContext.Provider value={value}>{children}</StatsContext.Provider>;
}

export function useStats(): StatsState {
  const value = useContext(StatsContext);
  if (!value) throw new Error('useStats must be used inside <StatsProvider>');
  return value;
}
