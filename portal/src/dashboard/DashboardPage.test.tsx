import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createStatsClient } from '../api/client';
import { defaultDashboard } from '../config/dashboard';
import { FiltersProvider } from '../filters/FiltersContext';
import { fakeFetch, jsonResponse } from '../test/fakeFetch';
import { summaryEnvelope } from '../test/fixtures/summary';
import { ApiContext } from './ApiContext';
import { DashboardPage } from './DashboardPage';

const NOW = new Date('2026-10-10T15:00:00Z');

function renderPage(f: ReturnType<typeof fakeFetch>, page: 'roas' | 'signal' = 'roas') {
  return render(
    <ApiContext.Provider value={createStatsClient({ fetchImpl: f })}>
      <FiltersProvider now={() => NOW}>
        <DashboardPage config={defaultDashboard.pages[page]!} lang="en" />
      </FiltersProvider>
    </ApiContext.Provider>,
  );
}

function seedTenant(id: string) {
  window.localStorage.setItem('motahai.filters', JSON.stringify({ tenantId: id }));
}

describe('DashboardPage', () => {
  beforeEach(() => window.localStorage.clear());
  afterEach(cleanup);

  it('asks for a tenant and does not call the API when none is chosen', () => {
    const f = fakeFetch(() => jsonResponse(summaryEnvelope));
    renderPage(f);
    expect(screen.getByText('Choose a tenant')).toBeTruthy();
    expect(f).not.toHaveBeenCalled();
  });

  it('fetches the 7-day default window and renders real fixture numbers', async () => {
    seedTenant('7');
    const f = fakeFetch(() => jsonResponse(summaryEnvelope));
    renderPage(f);

    expect(await screen.findByText('Orders placed')).toBeTruthy();
    expect(screen.getByText('200')).toBeTruthy();
    expect(screen.getByText('×1.42')).toBeTruthy();
    expect(screen.getByText('ad-111')).toBeTruthy();
    expect(f).toHaveBeenCalledTimes(1);
    expect(f.mock.calls[0][0]).toBe('/v1/tenants/7/stats/summary?start=2026-10-03&end=2026-10-10');
  });

  it('refetches with a new window when the range changes', async () => {
    seedTenant('7');
    const f = fakeFetch(() => jsonResponse(summaryEnvelope));
    renderPage(f);
    await screen.findByText('Orders placed');

    fireEvent.click(screen.getByRole('button', { name: 'Last 30 days' }));

    await waitFor(() => expect(f).toHaveBeenCalledTimes(2));
    expect(f.mock.calls[1][0]).toBe('/v1/tenants/7/stats/summary?start=2026-09-10&end=2026-10-10');
  });

  it('validates a custom range inline without calling the API', async () => {
    seedTenant('7');
    const f = fakeFetch(() => jsonResponse(summaryEnvelope));
    renderPage(f);
    await screen.findByText('Orders placed');

    fireEvent.click(screen.getByRole('button', { name: 'Custom' }));
    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-10-10' } });

    expect(await screen.findByText('The end date must be after the start date.')).toBeTruthy();
    expect(screen.getByText('Invalid date range')).toBeTruthy();
    expect(f).toHaveBeenCalledTimes(1);
  });

  it('shows the 401 copy with a retry that refetches', async () => {
    seedTenant('7');
    const f = fakeFetch(() => jsonResponse({ detail: 'nope' }, 401));
    renderPage(f);

    expect(await screen.findByText(/no operator key configured/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(f).toHaveBeenCalledTimes(2));
  });

  it('shows the 404 and 422 copy', async () => {
    seedTenant('99');
    renderPage(fakeFetch(() => jsonResponse({ detail: 'Unknown tenant' }, 404)));
    expect(await screen.findByText('Unknown tenant. Check the tenant id.')).toBeTruthy();
    cleanup();

    renderPage(fakeFetch(() => jsonResponse({ detail: 'window must not exceed 92 days' }, 422)));
    expect(await screen.findByText('window must not exceed 92 days')).toBeTruthy();
  });

  it('shows the unreachable copy on network failure', async () => {
    seedTenant('7');
    renderPage(
      fakeFetch(() => {
        throw new TypeError('Failed to fetch');
      }),
    );
    expect(await screen.findByText('Cannot reach the API.')).toBeTruthy();
  });

  it('renders an error card for an unknown widget type instead of crashing', async () => {
    seedTenant('7');
    const f = fakeFetch(() => jsonResponse(summaryEnvelope));
    render(
      <ApiContext.Provider value={createStatsClient({ fetchImpl: f })}>
        <FiltersProvider now={() => NOW}>
          <DashboardPage
            lang="en"
            config={{
              route: 'roas',
              filters: ['tenant'],
              widgets: [{ id: 'x', type: 'does-not-exist', options: {} }],
            }}
          />
        </FiltersProvider>
      </ApiContext.Provider>,
    );
    expect(await screen.findByText('Unknown widget type: does-not-exist')).toBeTruthy();
  });
});
