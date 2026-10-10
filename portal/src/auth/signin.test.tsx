import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import App from '../App';
import { fakeFetch, jsonResponse } from '../test/fakeFetch';
import { summaryEnvelope } from '../test/fixtures/summary';
import { SESSION_STORAGE_KEY } from './session';

const KEY = 'mtk_0a1b2c3d_' + 'A'.repeat(43);
const PROFILE = { tenant_id: 7, name: 'Acme Store', currency: 'EGP', country: 'EG', mode: 'live', platform: 'shopify' };
const UNAUTH = { detail: 'Invalid or missing API key' };

function authHeader(init?: RequestInit): string | undefined {
  return (init?.headers as Record<string, string> | undefined)?.Authorization;
}

function router(onStats?: () => Response) {
  return fakeFetch((url, init) => {
    if (url === '/v1/tenant/me') {
      return authHeader(init) === `Bearer ${KEY}` ? jsonResponse(PROFILE) : jsonResponse(UNAUTH, 401);
    }
    if (url.includes('/stats/summary')) return onStats ? onStats() : jsonResponse(summaryEnvelope);
    return jsonResponse({}, 404);
  });
}

function signIn(key: string) {
  fireEvent.change(screen.getByLabelText('Tenant API key'), { target: { value: key } });
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
}

describe('sign-in', () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.localStorage.setItem('motahai.lang', 'en');
    window.location.hash = '#/roas';
  });
  afterEach(cleanup);

  it('shows the sign-in screen first, with a masked key input and an operator button', () => {
    const f = router();
    render(<App fetchImpl={f} />);
    const input = screen.getByLabelText('Tenant API key') as HTMLInputElement;
    expect(input.type).toBe('password');
    expect(input.autocomplete).toBe('off');
    expect(screen.getByRole('button', { name: 'Operator mode (local proxy)' })).toBeTruthy();
    expect(f).not.toHaveBeenCalled();
  });

  it('rejects a malformed key without any network call', () => {
    const f = router();
    render(<App fetchImpl={f} />);
    signIn('hello');
    return screen.findByText(/key format is wrong/).then(() => {
      expect(f).not.toHaveBeenCalled();
      expect(window.sessionStorage.length).toBe(0);
    });
  });

  it('signs in with a valid key: bearer sent, key in sessionStorage only, name shown, tenant filter hidden', async () => {
    const f = router();
    render(<App fetchImpl={f} />);
    signIn(KEY);

    expect(await screen.findByText('Acme Store')).toBeTruthy();
    expect(await screen.findByText('Orders placed')).toBeTruthy();
    expect(screen.queryByLabelText('Tenant ID')).toBeNull();
    expect(screen.getByRole('button', { name: 'Sign out' })).toBeTruthy();

    const stats = f.mock.calls.find(([url]) => String(url).includes('/stats/summary'))!;
    expect(String(stats[0])).toMatch(/^\/v1\/tenants\/7\/stats\/summary\?/);
    expect(authHeader(stats[1])).toBe(`Bearer ${KEY}`);
    expect(window.sessionStorage.getItem(SESSION_STORAGE_KEY)).toContain(KEY);
    expect(JSON.stringify({ ...window.localStorage })).not.toContain('mtk_');
    expect(window.location.href).not.toContain('mtk_');
  });

  it('shows "key not valid" on 401 and stores nothing', async () => {
    const f = router();
    render(<App fetchImpl={f} />);
    signIn('mtk_deadbeef_' + 'B'.repeat(43));

    expect((await screen.findByRole('alert')).textContent).toBe('Key not valid.');
    expect(window.sessionStorage.length).toBe(0);
    expect(screen.getByLabelText('Tenant API key')).toBeTruthy();
    expect((screen.getByLabelText('Tenant API key') as HTMLInputElement).value).toBe('');
  });

  it('operator mode sends no Authorization header and keeps the tenant filter', async () => {
    window.localStorage.setItem('motahai.filters', JSON.stringify({ tenantId: '7' }));
    const f = router();
    render(<App fetchImpl={f} />);
    fireEvent.click(screen.getByRole('button', { name: 'Operator mode (local proxy)' }));

    expect(await screen.findByText('Orders placed')).toBeTruthy();
    expect(screen.getByLabelText('Tenant ID')).toBeTruthy();
    expect(screen.getByText('Operator')).toBeTruthy();
    const stats = f.mock.calls.find(([url]) => String(url).includes('/stats/summary'))!;
    expect(authHeader(stats[1])).toBeUndefined();
  });

  it('a 401 from stats in tenant mode returns to sign-in with a session-expired note', async () => {
    let revoked = false;
    const f = router(() => (revoked ? jsonResponse(UNAUTH, 401) : jsonResponse(summaryEnvelope)));
    render(<App fetchImpl={f} />);
    signIn(KEY);
    await screen.findByText('Orders placed');

    revoked = true;
    fireEvent.click(screen.getByRole('button', { name: 'Last 30 days' }));

    expect(await screen.findByText('Session expired. Please sign in again.')).toBeTruthy();
    expect(screen.getByLabelText('Tenant API key')).toBeTruthy();
    expect(window.sessionStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
  });

  it('sign out clears the session', async () => {
    render(<App fetchImpl={router()} />);
    signIn(KEY);
    await screen.findByText('Acme Store');
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }));
    await waitFor(() => expect(screen.getByLabelText('Tenant API key')).toBeTruthy());
    expect(window.sessionStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
    expect(screen.queryByText(/expired/)).toBeNull();
  });

  it('restores a tab session from sessionStorage on reload', async () => {
    window.sessionStorage.setItem(
      SESSION_STORAGE_KEY,
      JSON.stringify({ mode: 'tenant', key: KEY, profile: PROFILE }),
    );
    render(<App fetchImpl={router()} />);
    expect(await screen.findByText('Orders placed')).toBeTruthy();
  });
});
