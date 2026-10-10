import { describe, expect, it } from 'vitest';
import { ApiError } from '../api/client';
import { bearerAuth } from '../api/auth';
import { createOnboardingClient } from '../api/onboarding';
import { fakeFetch, jsonResponse } from '../test/fakeFetch';

describe('onboarding client', () => {
  it('POST /tenants with a JSON body', async () => {
    const f = fakeFetch(() => jsonResponse({ tenant_id: 3 }, 201));
    const c = createOnboardingClient({ baseUrl: 'http://api', fetchImpl: f });
    const body = { shop_domain: 'a.myshopify.com', platform: 'shopify' as const, country: 'EG', currency: 'EGP' };
    await c.createTenant(body);
    const [url, init] = f.mock.calls[0]!;
    expect(url).toBe('http://api/v1/operator/onboarding/tenants');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(String(init?.body))).toEqual(body);
    expect((init?.headers as Record<string, string>)['Content-Type']).toBe('application/json');
  });

  it('meta, courier-secrets, api-key and checklist hit the right URLs and methods', async () => {
    const f = fakeFetch(() => jsonResponse({}));
    const c = createOnboardingClient({ fetchImpl: f });
    await c.storeMeta(5, { meta_dataset_id: '12345', meta_capi_token: 'tok-1234567' });
    await c.courierSecrets(5, { couriers: ['oto'], rotate: true });
    await c.issueApiKey(5);
    await c.checklist(5);
    const calls = f.mock.calls.map(([u, i]) => [u, i?.method ?? 'GET']);
    expect(calls).toEqual([
      ['/v1/operator/onboarding/tenants/5/meta', 'POST'],
      ['/v1/operator/onboarding/tenants/5/courier-secrets', 'POST'],
      ['/v1/operator/onboarding/tenants/5/api-key', 'POST'],
      ['/v1/operator/onboarding/tenants/5/checklist', 'GET'],
    ]);
    expect(JSON.parse(String(f.mock.calls[1]![1]?.body))).toEqual({ couriers: ['oto'], rotate: true });
    expect(f.mock.calls[2]![1]?.body).toBeUndefined();
  });

  it('adds auth headers', async () => {
    const f = fakeFetch(() => jsonResponse({}));
    await createOnboardingClient({ fetchImpl: f, auth: bearerAuth(() => 'k') }).checklist(1);
    expect((f.mock.calls[0]![1]?.headers as Record<string, string>).Authorization).toBe('Bearer k');
  });

  it.each([409, 422, 404, 503])('maps %i to ApiError with the server detail', async (status) => {
    const f = fakeFetch(() => jsonResponse({ detail: `boom ${status}` }, status));
    const err = await createOnboardingClient({ fetchImpl: f }).checklist(1).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(status);
    expect((err as ApiError).detail).toBe(`boom ${status}`);
  });

  it('maps network failures to status 0', async () => {
    const f = fakeFetch(() => {
      throw new Error('offline');
    });
    const err = await createOnboardingClient({ fetchImpl: f }).checklist(1).catch((e: unknown) => e);
    expect((err as ApiError).status).toBe(0);
  });
});
