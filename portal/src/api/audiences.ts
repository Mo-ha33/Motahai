import { sameOriginAuth } from './auth';
import { createTransport, type TransportOptions } from './client';
import type { AudienceListName, AudiencesStatus } from './types';

export type AudiencesClientOptions = TransportOptions;

export interface AudiencesClient {
  /** Needs the operator key or a tenant key holding `audiences:read` (a tenant key without it is a 401). */
  status(tenantId: number, signal?: AbortSignal): Promise<AudiencesStatus>;
  /** The CSV as a Blob: a plain link cannot carry the bearer header. */
  download(tenantId: number, name: AudienceListName, signal?: AbortSignal): Promise<Blob>;
  /** Operator only (a tenant key is a 403). Returns the refreshed status. */
  exportNow(tenantId: number, signal?: AbortSignal): Promise<AudiencesStatus>;
}

export function createAudiencesClient({
  baseUrl = '',
  auth = sameOriginAuth,
  fetchImpl,
}: AudiencesClientOptions = {}): AudiencesClient {
  const { request, requestBlob } = createTransport({ baseUrl, auth, fetchImpl });
  const root = (id: number) => `/v1/tenants/${encodeURIComponent(String(id))}/audiences`;
  return {
    status: (id, signal) => request<AudiencesStatus>(root(id), { signal }),
    download: (id, name, signal) => requestBlob(`${root(id)}/${name}.csv`, { signal }),
    exportNow: (id, signal) => request<AudiencesStatus>(`${root(id)}/export`, { method: 'POST', signal }),
  };
}

export const audiencesClient = createAudiencesClient();
