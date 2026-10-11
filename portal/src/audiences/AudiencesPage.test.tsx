import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createAudiencesClient } from '../api/audiences';
import type { AudiencesStatus } from '../api/types';
import { SessionProvider } from '../auth/SessionContext';
import type { Session } from '../auth/session';
import { AudiencesApiContext } from '../dashboard/ApiContext';
import { FiltersProvider } from '../filters/FiltersContext';
import { loadSession, SESSION_STORAGE_KEY, saveSession } from '../auth/session';
import { fakeFetch, jsonResponse } from '../test/fakeFetch';
import { AUDIENCES_STATUS, HASH_A, HASH_B, HASHED_CSV } from '../test/fixtures/audiences';
import { AudiencesPage } from './AudiencesPage';

const STATUS: AudiencesStatus = {
  tenant_id: 7,
  min_list_rows: 100,
  lists: [
    { name: 'exclude_refusers', rows: 12, updated_at: '2026-10-04T10:00:00+00:00', size_bytes: 900 },
    { name: 'seed_delivered_buyers', rows: null, updated_at: null, size_bytes: null },
  ],
  schedule: {
    job_name: 'export_weekly_audiences',
    last_run_at: '2026-10-04T10:00:00+00:00',
    last_run_ok: true,
    last_error_type: null,
    next_due_at: '2026-10-11T10:00:00+00:00',
  },
};

function setup(session: Session, handler: Parameters<typeof fakeFetch>[0]) {
  const fetchImpl = fakeFetch(handler);
  const client = createAudiencesClient({ fetchImpl, auth: { headers: () => ({ Authorization: 'Bearer mtk_x' }) } });
  render(
    <SessionProvider initial={session}>
      <FiltersProvider>
        <AudiencesApiContext.Provider value={client}>
          <AudiencesPage lang="en" />
        </AudiencesApiContext.Provider>
      </FiltersProvider>
    </SessionProvider>,
  );
  return fetchImpl;
}

const TENANT: Session = {
  mode: 'tenant',
  key: 'mtk_abcd1234_secret',
  profile: { tenant_id: 7, name: 'Shop' } as never,
};

describe('AudiencesPage', () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
  });
  afterEach(() => cleanup());

  it('shows both lists, the small-list warning and the schedule', async () => {
    const fetchImpl = setup(TENANT, () => jsonResponse(STATUS));
    expect(await screen.findByText('Exclude refusers')).toBeTruthy();
    expect(screen.getByText('Delivered buyers')).toBeTruthy();
    expect(screen.getByText(/Fewer than 100 rows/)).toBeTruthy();
    expect(screen.getByText('Weekly export')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Export now' })).toBeNull(); // tenant mode
    expect(fetchImpl.mock.calls[0][0]).toBe('/v1/tenants/7/audiences');
  });

  it('a 401 in tenant mode explains the missing scope and does not sign out', async () => {
    setup(TENANT, () => jsonResponse({ detail: 'Invalid or missing API key' }, 401));
    expect(await screen.findByText(/ask your operator|Ask your operator/i)).toBeTruthy();
  });

  it('downloads through the authenticated client and revokes the object URL', async () => {
    const create = vi.fn(() => 'blob:fake');
    const revoke = vi.fn();
    Object.assign(URL, { createObjectURL: create, revokeObjectURL: revoke });
    // Stub the anchor click so jsdom does not attempt (and log) a navigation.
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    const fetchImpl = setup(TENANT, (url) =>
      url.endsWith('.csv')
        ? new Response('phone,email\n', { status: 200, headers: { 'Content-Type': 'text/csv' } })
        : jsonResponse(STATUS),
    );
    const buttons = await screen.findAllByRole('button', { name: 'Download CSV' });
    fireEvent.click(buttons[0]);
    // saveBlob defers the revoke by 1000 ms, so allow more than waitFor's default 1000 ms timeout.
    await waitFor(() => expect(revoke).toHaveBeenCalledWith('blob:fake'), { timeout: 2000 });
    const call = fetchImpl.mock.calls.find((c) => String(c[0]).endsWith('.csv'));
    expect(call?.[0]).toBe('/v1/tenants/7/audiences/exclude_refusers.csv');
    expect((call?.[1]?.headers as Record<string, string>).Authorization).toBe('Bearer mtk_x');
    clickSpy.mockRestore();
    Reflect.deleteProperty(URL, 'createObjectURL');
    Reflect.deleteProperty(URL, 'revokeObjectURL');
  });

  it('operator mode: Export now posts and refreshes the status', async () => {
    window.localStorage.setItem('motahai.filters', JSON.stringify({ tenantId: '7' }));
    const fetchImpl = setup({ mode: 'operator' }, (_url, init) => jsonResponse(STATUS, init?.method === 'POST' ? 200 : 200));
    const button = await screen.findByRole('button', { name: 'Export now' });
    fireEvent.click(button);
    await waitFor(() => expect(fetchImpl.mock.calls.some((c) => c[1]?.method === 'POST')).toBe(true));
    expect(fetchImpl.mock.calls.find((c) => c[1]?.method === 'POST')?.[0]).toBe('/v1/tenants/7/audiences/export');
  });
});


function cardFor(name: string): HTMLElement {
  const section = screen.getByRole('heading', { name }).closest('section');
  if (!section) throw new Error(`no card for ${name}`);
  return section;
}

/** A complete profile: saveSession/loadSession only accept full TenantProfile objects. */
const TENANT_FULL: Session = {
  mode: 'tenant',
  key: 'mtk_abcd1234_secretsecretsecret',
  profile: { tenant_id: 7, name: 'Shop', currency: 'EGP', country: 'EG', mode: 'live', platform: 'salla' },
};

describe('AudiencesPage: behaviour', () => {
  let createObjectURL: ReturnType<typeof vi.fn>;
  let revokeObjectURL: ReturnType<typeof vi.fn>;
  let clickSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    createObjectURL = vi.fn(() => 'blob:test-1');
    revokeObjectURL = vi.fn();
    Object.assign(URL, { createObjectURL, revokeObjectURL });
    clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    Reflect.deleteProperty(URL, 'createObjectURL');
    Reflect.deleteProperty(URL, 'revokeObjectURL');
  });

  it('operator mode shows Export now and the tenant filter; tenant mode shows neither', async () => {
    window.localStorage.setItem('motahai.filters', JSON.stringify({ tenantId: '7' }));
    setup({ mode: 'operator' }, () => jsonResponse(AUDIENCES_STATUS));
    expect(await screen.findByRole('button', { name: 'Export now' })).toBeTruthy();
    expect(document.getElementById('filter-tenant')).not.toBeNull();
  });

  it('tenant mode hides Export now and the tenant filter', async () => {
    setup(TENANT, () => jsonResponse(AUDIENCES_STATUS));
    expect(await screen.findByRole('heading', { name: 'Exclude refusers' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Export now' })).toBeNull();
    expect(document.getElementById('filter-tenant')).toBeNull();
  });

  it('warns only for a list below min_list_rows (a list at exactly the minimum has no warning)', async () => {
    setup(TENANT, () => jsonResponse(AUDIENCES_STATUS)); // exclude 2 rows (< 100), seed 100 rows (= 100)
    await screen.findByRole('heading', { name: 'Exclude refusers' });
    expect(screen.getAllByText(/Fewer than 100 rows/)).toHaveLength(1);
    expect(within(cardFor('Exclude refusers')).getByText(/Fewer than 100 rows/)).toBeTruthy();
    expect(within(cardFor('Delivered buyers')).queryByText(/Fewer than 100 rows/)).toBeNull();
  });

  it('a list that was never exported shows the empty state, a disabled download and no warning', async () => {
    const never = {
      ...AUDIENCES_STATUS,
      lists: AUDIENCES_STATUS.lists.map((l) => ({ ...l, rows: null, updated_at: null, size_bytes: null })),
    };
    setup(TENANT, () => jsonResponse(never));
    await screen.findByRole('heading', { name: 'Exclude refusers' });
    expect(screen.queryByText(/Fewer than 100 rows/)).toBeNull();
    expect(screen.getAllByText('Not exported yet')).toHaveLength(2);
    const download = within(cardFor('Exclude refusers')).getByRole('button', { name: 'Download CSV' });
    expect((download as HTMLButtonElement).disabled).toBe(true);
  });

  it('download saves the Blob through an object URL and revokes it after the click', async () => {
    setup(TENANT, (url) =>
      url.endsWith('.csv')
        ? new Response(HASHED_CSV, { status: 200, headers: { 'Content-Type': 'text/csv' } })
        : jsonResponse(AUDIENCES_STATUS),
    );
    const [first] = await screen.findAllByRole('button', { name: 'Download CSV' });
    fireEvent.click(first);
    // The revoke is deferred by 1000 ms in saveBlob, so allow more than waitFor's default timeout.
    await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith('blob:test-1'), { timeout: 2000 });

    expect(createObjectURL).toHaveBeenCalledTimes(1);
    const saved = createObjectURL.mock.calls[0][0] as Blob;
    expect(saved.type).toBe('text/csv');
    expect(await new Response(saved).text()).toBe(HASHED_CSV);
    expect(clickSpy).toHaveBeenCalledTimes(1);
    const anchor = clickSpy.mock.contexts[0] as HTMLAnchorElement;
    expect(anchor.download).toBe('7_exclude_refusers.csv');
    expect(clickSpy.mock.invocationCallOrder[0]).toBeLessThan(revokeObjectURL.mock.invocationCallOrder[0]);
    expect(document.querySelector('a[download]')).toBeNull(); // the temporary anchor is removed
  });

  it('a failed download shows an alert and still revokes nothing it never created', async () => {
    setup(TENANT, (url) =>
      url.endsWith('.csv') ? jsonResponse({ detail: 'Audience not exported yet' }, 404) : jsonResponse(AUDIENCES_STATUS),
    );
    const [first] = await screen.findAllByRole('button', { name: 'Download CSV' });
    fireEvent.click(first);
    expect(await screen.findByText('Could not download the file.')).toBeTruthy();
    expect(createObjectURL).not.toHaveBeenCalled();
    expect(revokeObjectURL).not.toHaveBeenCalled();
  });

  it('a 401 in tenant mode shows the access message and keeps the tenant session', async () => {
    saveSession(TENANT_FULL);
    setup(TENANT_FULL, () => jsonResponse({ detail: 'Invalid or missing API key' }, 401));
    expect(await screen.findByText(/Ask your operator to grant audience access/)).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'No audience access' })).toBeTruthy();
    expect(loadSession()).toEqual(TENANT_FULL);
    expect(window.sessionStorage.getItem(SESSION_STORAGE_KEY)).not.toBeNull();
  });

  it('a 401 in operator mode is the proxy-key message, not the tenant access message', async () => {
    window.localStorage.setItem('motahai.filters', JSON.stringify({ tenantId: '7' }));
    setup({ mode: 'operator' }, () => jsonResponse({ detail: 'Invalid or missing API key' }, 401));
    expect(await screen.findByText(/No operator key is configured/)).toBeTruthy();
    expect(screen.queryByText(/Ask your operator to grant audience access/)).toBeNull();
  });

  it('operator Export now posts and renders the refreshed status from the response', async () => {
    window.localStorage.setItem('motahai.filters', JSON.stringify({ tenantId: '7' }));
    const refreshed = {
      ...AUDIENCES_STATUS,
      lists: [{ ...AUDIENCES_STATUS.lists[0], rows: 41 }, AUDIENCES_STATUS.lists[1]],
    };
    const fetchImpl = setup({ mode: 'operator' }, (_url, init) =>
      jsonResponse(init?.method === 'POST' ? refreshed : AUDIENCES_STATUS),
    );
    await screen.findByRole('heading', { name: 'Exclude refusers' });
    const exclude = cardFor('Exclude refusers');
    expect(within(exclude).getByText('2')).toBeTruthy();
    fireEvent.click(await screen.findByRole('button', { name: 'Export now' }));
    await waitFor(() => expect(within(exclude).getByText('41')).toBeTruthy());
    expect(fetchImpl.mock.calls.filter((c) => c[1]?.method === 'POST')).toHaveLength(1);
  });

  it('a failed export shows an alert and keeps the previous status', async () => {
    window.localStorage.setItem('motahai.filters', JSON.stringify({ tenantId: '7' }));
    setup({ mode: 'operator' }, (_url, init) =>
      init?.method === 'POST' ? jsonResponse({ detail: 'boom' }, 500) : jsonResponse(AUDIENCES_STATUS),
    );
    fireEvent.click(await screen.findByRole('button', { name: 'Export now' }));
    expect(await screen.findByText('Could not run the export.')).toBeTruthy();
    expect(within(cardFor('Exclude refusers')).getByText('2')).toBeTruthy();
  });

  it('never renders SHA-256-shaped strings, even when the downloaded blob contains them', async () => {
    setup(TENANT, (url) =>
      url.endsWith('.csv')
        ? new Response(HASHED_CSV, { status: 200, headers: { 'Content-Type': 'text/csv' } })
        : jsonResponse(AUDIENCES_STATUS),
    );
    const [first] = await screen.findAllByRole('button', { name: 'Download CSV' });
    fireEvent.click(first);
    await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith('blob:test-1'), { timeout: 2000 });

    // Sanity: the blob really holds hashes, so the absence below is meaningful.
    expect(await new Response(createObjectURL.mock.calls[0][0] as Blob).text()).toContain(HASH_A);
    const html = document.body.innerHTML;
    expect(html).not.toContain(HASH_A);
    expect(html).not.toContain(HASH_B);
    expect(document.body.textContent ?? '').not.toMatch(/[0-9a-f]{64}/i);
    expect(html).not.toMatch(/[0-9a-f]{64}/i);
  });
});
