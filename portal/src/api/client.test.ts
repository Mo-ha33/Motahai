import { describe, expect, it } from 'vitest';
import { bearerAuth } from './auth';
import { ApiError, createStatsClient } from './client';
import { fakeFetch, jsonResponse } from '../test/fakeFetch';
import { summaryEnvelope } from '../test/fixtures/summary';

const window = { start: '2026-10-03', end: '2026-10-10' };

describe('stats client', () => {
  it('builds the summary URL with query params and returns the envelope', async () => {
    const f = fakeFetch(() => jsonResponse(summaryEnvelope));
    const client = createStatsClient({ fetchImpl: f });

    const result = await client.summary(7, window);

    expect(result).toEqual(summaryEnvelope);
    expect(f.mock.calls[0][0]).toBe('/v1/tenants/7/stats/summary?start=2026-10-03&end=2026-10-10');
  });

  it('prefixes baseUrl, encodes the tenant id and targets a section', async () => {
    const f = fakeFetch(() => jsonResponse({}));
    const client = createStatsClient({ baseUrl: 'http://api.test', fetchImpl: f });

    await client.section('refused-cod', 'a/b', window);

    expect(f.mock.calls[0][0]).toBe(
      'http://api.test/v1/tenants/a%2Fb/stats/refused-cod?start=2026-10-03&end=2026-10-10',
    );
  });

  it('sends no Authorization header by default and bearer auth when configured', async () => {
    const f1 = fakeFetch(() => jsonResponse({}));
    await createStatsClient({ fetchImpl: f1 }).summary(1, window);
    expect((f1.mock.calls[0][1]?.headers as Record<string, string>).Authorization).toBeUndefined();

    const f2 = fakeFetch(() => jsonResponse({}));
    await createStatsClient({ fetchImpl: f2, auth: bearerAuth(() => 'tok') }).summary(1, window);
    expect((f2.mock.calls[0][1]?.headers as Record<string, string>).Authorization).toBe('Bearer tok');
  });

  it('maps non-2xx responses to ApiError with the detail', async () => {
    const f = fakeFetch(() => jsonResponse({ detail: 'end must be after start' }, 422));
    const client = createStatsClient({ fetchImpl: f });

    await expect(client.summary(1, window)).rejects.toMatchObject({
      name: 'ApiError',
      status: 422,
      detail: 'end must be after start',
    });
  });

  it('maps network failures to ApiError status 0', async () => {
    const f = fakeFetch(() => {
      throw new TypeError('Failed to fetch');
    });
    const err = await createStatsClient({ fetchImpl: f }).summary(1, window).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(0);
  });

  it('passes the AbortSignal to fetch and rethrows aborts', async () => {
    const controller = new AbortController();
    const f = fakeFetch((_url, init) => {
      controller.abort();
      throw new DOMException('aborted', 'AbortError');
      void init;
    });
    const p = createStatsClient({ fetchImpl: f }).summary(1, window, controller.signal);
    await expect(p).rejects.toMatchObject({ name: 'AbortError' });
    expect(f.mock.calls[0][1]?.signal).toBe(controller.signal);
  });
});
