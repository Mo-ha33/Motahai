import { sameOriginAuth } from './auth';
import { createTransport, type TransportOptions } from './client';
import type {
  ChecklistResponse,
  CourierSecretsRequest,
  CourierSecretsResponse,
  CreateTenantRequest,
  CreateTenantResponse,
  IssueKeyResponse,
  MetaRequest,
  MetaResponse,
} from './types';

export type OnboardingClientOptions = TransportOptions;

export interface OnboardingClient {
  createTenant(body: CreateTenantRequest, signal?: AbortSignal): Promise<CreateTenantResponse>;
  storeMeta(tenantId: number, body: MetaRequest, signal?: AbortSignal): Promise<MetaResponse>;
  courierSecrets(
    tenantId: number,
    body: CourierSecretsRequest,
    signal?: AbortSignal,
  ): Promise<CourierSecretsResponse>;
  issueApiKey(tenantId: number, signal?: AbortSignal): Promise<IssueKeyResponse>;
  checklist(tenantId: number, signal?: AbortSignal): Promise<ChecklistResponse>;
}

const BASE = '/v1/operator/onboarding';

/** Operator-only endpoints; the operator key is added by the server-side proxy (same-origin auth). */
export function createOnboardingClient({
  baseUrl = '',
  auth = sameOriginAuth,
  fetchImpl,
}: OnboardingClientOptions = {}): OnboardingClient {
  const { request } = createTransport({ baseUrl, auth, fetchImpl });
  const tenant = (id: number) => `${BASE}/tenants/${encodeURIComponent(String(id))}`;
  return {
    createTenant: (body, signal) => request(`${BASE}/tenants`, { method: 'POST', body, signal }),
    storeMeta: (id, body, signal) => request(`${tenant(id)}/meta`, { method: 'POST', body, signal }),
    courierSecrets: (id, body, signal) =>
      request(`${tenant(id)}/courier-secrets`, { method: 'POST', body, signal }),
    issueApiKey: (id, signal) => request(`${tenant(id)}/api-key`, { method: 'POST', signal }),
    checklist: (id, signal) => request(`${tenant(id)}/checklist`, { signal }),
  };
}

export const onboardingClient = createOnboardingClient();
