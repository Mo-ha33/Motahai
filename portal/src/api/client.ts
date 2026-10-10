import { sameOriginAuth, type AuthProvider } from './auth';
import type {
  SectionDataMap,
  StatsEnvelope,
  StatsSection,
  StatsSummaryData,
  StatsWindow,
} from './types';

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(`API error ${status}: ${detail}`);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

export interface StatsClientOptions {
  baseUrl?: string;
  auth?: AuthProvider;
  fetchImpl?: typeof fetch;
}

export type TenantRef = number | string;

export interface StatsClient {
  summary(
    tenantId: TenantRef,
    window: StatsWindow,
    signal?: AbortSignal,
  ): Promise<StatsEnvelope<StatsSummaryData>>;
  section<N extends StatsSection>(
    name: N,
    tenantId: TenantRef,
    window: StatsWindow,
    signal?: AbortSignal,
  ): Promise<StatsEnvelope<SectionDataMap[N]>>;
}

function detailFrom(body: unknown, fallback: string): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === 'string') return detail;
    if (detail !== undefined) return JSON.stringify(detail);
  }
  return fallback;
}

export function createStatsClient({
  baseUrl = '',
  auth = sameOriginAuth,
  fetchImpl,
}: StatsClientOptions = {}): StatsClient {
  const doFetch: typeof fetch = fetchImpl ?? ((...args) => fetch(...args));

  async function get<T>(
    name: string,
    tenantId: TenantRef,
    window: StatsWindow,
    signal?: AbortSignal,
  ): Promise<T> {
    const query = new URLSearchParams({ start: window.start, end: window.end });
    const url = `${baseUrl}/v1/tenants/${encodeURIComponent(String(tenantId))}/stats/${name}?${query}`;
    const headers = { Accept: 'application/json', ...(await auth.headers()) };

    let response: Response;
    try {
      response = await doFetch(url, { headers, signal });
    } catch (err) {
      if (signal?.aborted) throw err;
      throw new ApiError(0, err instanceof Error ? err.message : 'Network error');
    }

    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    if (!response.ok) {
      throw new ApiError(response.status, detailFrom(body, response.statusText || 'Request failed'));
    }
    return body as T;
  }

  return {
    summary: (tenantId, window, signal) => get('summary', tenantId, window, signal),
    section: (name, tenantId, window, signal) => get(name, tenantId, window, signal),
  };
}
