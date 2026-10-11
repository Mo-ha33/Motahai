import { describe, expect, it } from 'vitest';
import { bearerAuth } from './auth';
import { ApiError, createStatsClient } from './client';
import { fakeFetch, jsonResponse } from '../test/fakeFetch';
import { healthScoreEnvelope } from '../test/fixtures/healthScore';

const window = { start: '2026-10-03', end: '2026-10-10' };

function authOf(init?: RequestInit): string | undefined {
  return (init?.headers as Record<string, string> | undefined)?.Authorization;
}

describe('stats client: healthScore', () => {
  it('builds the health-score URL with start and end and returns the envelope', async () => {
    const f = fakeFetch(() => jsonResponse(healthScoreEnvelope));
    const client = createStatsClient({ fetchImpl: f });

    const result = await client.healthScore(7, window);

    expect(result).toEqual(healthScoreEnvelope);
    expect(f).toHaveBeenCalledTimes(1);
    expect(f.mock.calls[0][0]).toBe('/v1/tenants/7/stats/health-score?start=2026-10-03&end=2026-10-10');
  });

  it('prefixes baseUrl and encodes the tenant id', async () => {
    const f = fakeFetch(() => jsonResponse(healthScoreEnvelope));
    await createStatsClient({ baseUrl: 'http://api.test', fetchImpl: f }).healthScore('a/b', window);
    expect(f.mock.calls[0][0]).toBe(
      'http://api.test/v1/tenants/a%2Fb/stats/health-score?start=2026-10-03&end=2026-10-10',
    );
  });

  it('sends the tenant key as a bearer header when auth is configured', async () => {
    const f = fakeFetch(() => jsonResponse(healthScoreEnvelope));
    await createStatsClient({ fetchImpl: f, auth: bearerAuth(() => 'tk_live') }).healthScore(7, window);
    expect(authOf(f.mock.calls[0][1])).toBe('Bearer tk_live');
  });

  it('sends no Authorization header when the session has no key', async () => {
    const f = fakeFetch(() => jsonResponse(healthScoreEnvelope));
    await createStatsClient({ fetchImpl: f, auth: bearerAuth(() => null) }).healthScore(7, window);
    expect(authOf(f.mock.calls[0][1])).toBeUndefined();
  });

  it('maps a 401 to ApiError with the server detail', async () => {
    const f = fakeFetch(() => jsonResponse({ detail: 'Invalid or missing API key' }, 401));
    const err = await createStatsClient({ fetchImpl: f }).healthScore(7, window).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ name: 'ApiError', status: 401, detail: 'Invalid or missing API key' });
  });

  it('maps a 500 with a non-JSON body to ApiError using the status text', async () => {
    const f = fakeFetch(() => new Response('<html>oops</html>', { status: 500, statusText: 'Internal Server Error' }));
    await expect(createStatsClient({ fetchImpl: f }).healthScore(7, window)).rejects.toMatchObject({
      name: 'ApiError',
      status: 500,
      detail: 'Internal Server Error',
    });
  });

  it('maps a 422 detail that is not a string to its JSON form', async () => {
    const f = fakeFetch(() => jsonResponse({ detail: [{ msg: 'bad' }] }, 422));
    await expect(createStatsClient({ fetchImpl: f }).healthScore(7, window)).rejects.toMatchObject({
      status: 422,
      detail: JSON.stringify([{ msg: 'bad' }]),
    });
  });

  it('maps a network failure to ApiError status 0', async () => {
    const f = fakeFetch(() => {
      throw new TypeError('Failed to fetch');
    });
    const err = await createStatsClient({ fetchImpl: f }).healthScore(7, window).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(0);
  });

  it('rethrows an abort instead of wrapping it', async () => {
    const controller = new AbortController();
    const f = fakeFetch(() => {
      controller.abort();
      throw new DOMException('aborted', 'AbortError');
    });
    const p = createStatsClient({ fetchImpl: f }).healthScore(7, window, controller.signal);
    await expect(p).rejects.toMatchObject({ name: 'AbortError' });
    expect(f.mock.calls[0][1]?.signal).toBe(controller.signal);
  });
});
