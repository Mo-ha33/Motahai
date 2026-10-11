import { describe, expect, it } from 'vitest';
import { bearerAuth } from './auth';
import { createAudiencesClient } from './audiences';
import { ApiError } from './client';
import { fakeFetch, jsonResponse } from '../test/fakeFetch';
import { AUDIENCES_STATUS, HASHED_CSV } from '../test/fixtures/audiences';

const bearer = bearerAuth(() => 'mtk_abcd1234_secretsecretsecret');

describe('audiences client', () => {
  it('status() GETs the tenant audiences root with auth and returns the status', async () => {
    const f = fakeFetch(() => jsonResponse(AUDIENCES_STATUS));
    const client = createAudiencesClient({ auth: bearer, fetchImpl: f });

    expect(await client.status(7)).toEqual(AUDIENCES_STATUS);
    const [url, init] = f.mock.calls[0];
    expect(url).toBe('/v1/tenants/7/audiences');
    expect(init?.method).toBeUndefined(); // GET
    expect((init?.headers as Record<string, string>).Authorization).toBe('Bearer mtk_abcd1234_secretsecretsecret');
    expect((init?.headers as Record<string, string>).Accept).toBe('application/json');
  });

  it('download() GETs the fixed list CSV and resolves a Blob with the bearer header', async () => {
    const f = fakeFetch(() => new Response(HASHED_CSV, { status: 200, headers: { 'Content-Type': 'text/csv' } }));
    const client = createAudiencesClient({ auth: bearer, fetchImpl: f });

    const blob = await client.download(7, 'seed_delivered_buyers');
    expect(blob.type).toBe('text/csv'); // realm-safe check (jsdom Blob vs Node Response Blob)
    expect(await new Response(blob).text()).toBe(HASHED_CSV);
    const [url, init] = f.mock.calls[0];
    expect(url).toBe('/v1/tenants/7/audiences/seed_delivered_buyers.csv');
    expect((init?.headers as Record<string, string>).Authorization).toBe('Bearer mtk_abcd1234_secretsecretsecret');
    expect((init?.headers as Record<string, string>).Accept).toBe('text/csv');
  });

  it('exportNow() POSTs to /export with no body and returns the refreshed status', async () => {
    const f = fakeFetch(() => jsonResponse(AUDIENCES_STATUS));
    const client = createAudiencesClient({ fetchImpl: f });

    expect(await client.exportNow(7)).toEqual(AUDIENCES_STATUS);
    const [url, init] = f.mock.calls[0];
    expect(url).toBe('/v1/tenants/7/audiences/export');
    expect(init?.method).toBe('POST');
    expect(init?.body).toBeUndefined();
  });

  it('prefixes baseUrl on every route', async () => {
    const f = fakeFetch(() => jsonResponse(AUDIENCES_STATUS));
    const client = createAudiencesClient({ baseUrl: 'http://api.test', fetchImpl: f });
    await client.status(3);
    await client.exportNow(3);
    expect(f.mock.calls.map((c) => c[0])).toEqual([
      'http://api.test/v1/tenants/3/audiences',
      'http://api.test/v1/tenants/3/audiences/export',
    ]);
  });

  it('maps a 401 on status to ApiError(401) with the server detail', async () => {
    const f = fakeFetch(() => jsonResponse({ detail: 'Invalid or missing API key' }, 401));
    const client = createAudiencesClient({ auth: bearer, fetchImpl: f });
    await expect(client.status(7)).rejects.toMatchObject({ status: 401, detail: 'Invalid or missing API key' });
    await expect(client.status(7)).rejects.toBeInstanceOf(ApiError);
  });

  it('maps a 403 on export (tenant key) to ApiError(403)', async () => {
    const f = fakeFetch(() => jsonResponse({ detail: 'Operator key required' }, 403));
    const client = createAudiencesClient({ auth: bearer, fetchImpl: f });
    await expect(client.exportNow(7)).rejects.toMatchObject({ status: 403, detail: 'Operator key required' });
  });

  it('maps a 404 on download to ApiError(404), also when the body is not JSON', async () => {
    const json = fakeFetch(() => jsonResponse({ detail: 'Audience not exported yet' }, 404));
    await expect(createAudiencesClient({ fetchImpl: json }).download(7, 'exclude_refusers')).rejects.toMatchObject({
      status: 404,
      detail: 'Audience not exported yet',
    });

    const html = fakeFetch(() => new Response('<html>nope</html>', { status: 404, statusText: 'Not Found' }));
    await expect(createAudiencesClient({ fetchImpl: html }).download(7, 'exclude_refusers')).rejects.toMatchObject({
      status: 404,
      detail: 'Not Found',
    });
  });

  it('maps a network failure to ApiError(0) for every route', async () => {
    const f = fakeFetch(() => {
      throw new TypeError('Failed to fetch');
    });
    const client = createAudiencesClient({ fetchImpl: f });
    await expect(client.status(7)).rejects.toMatchObject({ status: 0 });
    await expect(client.download(7, 'exclude_refusers')).rejects.toMatchObject({ status: 0 });
    await expect(client.exportNow(7)).rejects.toMatchObject({ status: 0 });
  });
});
