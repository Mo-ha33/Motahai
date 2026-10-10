import { sameOriginAuth, type AuthProvider } from './auth';
import type {
  HealthScoreData,
  SectionDataMap,
  StatsEnvelope,
  StatsSection,
  StatsSummaryData,
  StatsWindow,
  TenantProfile,
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
  /** The signed-in tenant's profile (tenant key required; the operator key is a 401). */
  me(signal?: AbortSignal): Promise<TenantProfile>;
  summary(
    tenantId: TenantRef,
    window: StatsWindow,
    signal?: AbortSignal,
  ): Promise<StatsEnvelope<StatsSummaryData>>;
  healthScore(
    tenantId: TenantRef,
    window: StatsWindow,
    signal?: AbortSignal,
  ): Promise<StatsEnvelope<HealthScoreData>>;
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

export interface TransportOptions {
  baseUrl?: string;
  auth?: AuthProvider;
  fetchImpl?: typeof fetch;
}

export interface RequestOptions {
  method?: 'GET' | 'POST';
  /** JSON-serialised when present. */
  body?: unknown;
  signal?: AbortSignal;
}

export interface Transport {
  baseUrl: string;
  /** `path` is appended to baseUrl. Errors are mapped to ApiError (status 0 = network failure). */
  request<T>(path: string, options?: RequestOptions): Promise<T>;
}

/** The one place that builds headers, calls fetch and maps failures to ApiError. */
export function createTransport({ baseUrl = '', auth = sameOriginAuth, fetchImpl }: TransportOptions = {}): Transport {
  const doFetch: typeof fetch = fetchImpl ?? ((...args) => fetch(...args));

  async function request<T>(path: string, { method = 'GET', body, signal }: RequestOptions = {}): Promise<T> {
    const headers: Record<string, string> = { Accept: 'application/json', ...(await auth.headers()) };
    const init: RequestInit = { headers, signal };
    if (method !== 'GET') {
      init.method = method;
      if (body !== undefined) {
        headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(body);
      }
    }

    let response: Response;
    try {
      response = await doFetch(`${baseUrl}${path}`, init);
    } catch (err) {
      if (signal?.aborted) throw err;
      throw new ApiError(0, err instanceof Error ? err.message : 'Network error');
    }

    let parsed: unknown = null;
    try {
      parsed = await response.json();
    } catch {
      parsed = null;
    }
    if (!response.ok) {
      throw new ApiError(response.status, detailFrom(parsed, response.statusText || 'Request failed'));
    }
    return parsed as T;
  }

  return { baseUrl, request };
}

export function createStatsClient({
  baseUrl = '',
  auth = sameOriginAuth,
  fetchImpl,
}: StatsClientOptions = {}): StatsClient {
  const transport = createTransport({ baseUrl, auth, fetchImpl });
  const request = <T>(path: string, signal?: AbortSignal) => transport.request<T>(path, { signal });

  function get<T>(name: string, tenantId: TenantRef, window: StatsWindow, signal?: AbortSignal): Promise<T> {
    const query = new URLSearchParams({ start: window.start, end: window.end });
    return request<T>(
      `/v1/tenants/${encodeURIComponent(String(tenantId))}/stats/${name}?${query}`,
      signal,
    );
  }

  return {
    me: (signal) => request<TenantProfile>(`/v1/tenant/me`, signal),
    summary: (tenantId, window, signal) => get('summary', tenantId, window, signal),
    healthScore: (tenantId, window, signal) => get('health-score', tenantId, window, signal),
    section: (name, tenantId, window, signal) => get(name, tenantId, window, signal),
  };
}
