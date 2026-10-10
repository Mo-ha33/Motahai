import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import App from '../App';
import { fakeFetch, jsonResponse } from '../test/fakeFetch';
import { healthScoreEnvelope } from '../test/fixtures/healthScore';
import { summaryEnvelope } from '../test/fixtures/summary';
import { SESSION_STORAGE_KEY } from './session';

const KEY = 'mtk_0a1b2c3d_' + 'A'.repeat(43);
const PROFILE = { tenant_id: 7, name: 'Acme Store', currency: 'EGP', country: 'EG', mode: 'live', platform: 'shopify' };
const UNAUTH = { detail: 'Invalid or missing API key' };

function authHeader(init?: RequestInit): string | undefined {
  return (init?.headers as Record<string, string> | undefined)?.Authorization;
}

/** Tenant key is accepted by /me; the health-score reply is controlled per test. */
function router(health: () => Response) {
  return fakeFetch((url, init) => {
    if (url === '/v1/tenant/me') {
      return authHeader(init) === `Bearer ${KEY}` ? jsonResponse(PROFILE) : jsonResponse(UNAUTH, 401);
    }
    if (url.includes('/stats/summary')) return jsonResponse(summaryEnvelope);
    if (url.includes('/stats/health-score')) return health();
    return jsonResponse({}, 404);
  });
}

function signIn(key: string) {
  fireEvent.change(screen.getByLabelText('Tenant API key'), { target: { value: key } });
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
}

describe('tenant mode: health-score auth failures', () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.localStorage.setItem('motahai.lang', 'en');
    window.location.hash = '#/signal';
  });
  afterEach(cleanup);

  it('a 401 from health-score signs out with the session-expired note', async () => {
    let revoked = false;
    const f = router(() => (revoked ? jsonResponse(UNAUTH, 401) : jsonResponse(healthScoreEnvelope)));
    render(<App fetchImpl={f} />);
    signIn(KEY);
    expect(await screen.findByText('Degraded')).toBeTruthy();

    revoked = true;
    fireEvent.click(screen.getByRole('button', { name: 'Last 30 days' }));

    expect(await screen.findByText('Session expired. Please sign in again.')).toBeTruthy();
    expect(screen.getByLabelText('Tenant API key')).toBeTruthy();
    expect(window.sessionStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
    expect(screen.queryByText('Degraded')).toBeNull();
    // The bearer key was sent on the health-score request.
    const healthCalls = f.mock.calls.filter(([url]) => String(url).includes('/stats/health-score'));
    expect(authHeader(healthCalls[healthCalls.length - 1][1])).toBe(`Bearer ${KEY}`);
  });

  it('a 500 from health-score keeps the tenant signed in and shows only that card', async () => {
    let broken = false;
    const f = router(() => (broken ? jsonResponse({ detail: 'boom' }, 500) : jsonResponse(healthScoreEnvelope)));
    render(<App fetchImpl={f} />);
    signIn(KEY);
    expect(await screen.findByText('Degraded')).toBeTruthy();

    broken = true;
    fireEvent.click(screen.getByRole('button', { name: 'Last 30 days' }));

    await waitFor(() => expect(document.querySelector('.widget-source-error[data-widget="health-score"]')).not.toBeNull());
    const card = document.querySelector<HTMLElement>('.widget-source-error[data-widget="health-score"]')!;
    expect(within(card).getByText('Unexpected error (500).')).toBeTruthy();
    expect(screen.queryByText('Session expired. Please sign in again.')).toBeNull();
    expect(screen.getByRole('button', { name: 'Sign out' })).toBeTruthy();
    expect(window.sessionStorage.getItem(SESSION_STORAGE_KEY)).toContain(KEY);
    expect(screen.getByText('400')).toBeTruthy();
  });
});
