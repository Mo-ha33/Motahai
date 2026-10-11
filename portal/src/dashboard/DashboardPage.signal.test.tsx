import { beforeEach, afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createStatsClient } from '../api/client';
import { defaultDashboard } from '../config/dashboard';
import { FiltersProvider } from '../filters/FiltersContext';
import { fakeFetch, jsonResponse } from '../test/fakeFetch';
import { healthScoreEnvelope } from '../test/fixtures/healthScore';
import { summaryEnvelope } from '../test/fixtures/summary';
import { ApiContext } from './ApiContext';
import { DashboardPage } from './DashboardPage';

const NOW = new Date('2026-10-10T15:00:00Z');
const QUERY_7D = 'start=2026-10-03&end=2026-10-10';
const QUERY_30D = 'start=2026-09-10&end=2026-10-10';

type Reply = Response | (() => Response | Promise<Response>);

/** Serves each stats source from its own reply; anything else is a 404. */
function router(replies: { summary?: Reply; health?: Reply; roas?: Reply } = {}) {
  const resolve = (r: Reply | undefined, fallback: () => Response): Response | Promise<Response> =>
    r === undefined ? fallback() : typeof r === 'function' ? r() : r;
  return fakeFetch((url) => {
    if (url.includes('/stats/summary')) {
      return resolve(replies.summary, () => jsonResponse(summaryEnvelope));
    }
    if (url.includes('/stats/health-score')) {
      return resolve(replies.health, () => jsonResponse(healthScoreEnvelope));
    }
    return jsonResponse({ detail: 'not found' }, 404);
  });
}

function renderPage(f: ReturnType<typeof fakeFetch>, page: 'roas' | 'signal' = 'signal') {
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

function urls(f: ReturnType<typeof fakeFetch>): string[] {
  return f.mock.calls.map(([url]) => String(url));
}

function errorCards(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll<HTMLElement>('.widget-source-error')).map(
    (el) => el.dataset.widget ?? '',
  );
}

describe('DashboardPage on the signal route (summary + health-score)', () => {
  beforeEach(() => window.localStorage.clear());
  afterEach(cleanup);

  it('requests both sources with the same window and renders both', async () => {
    seedTenant('7');
    const f = router();
    renderPage(f);

    expect(await screen.findByText('Tracking health score')).toBeTruthy();
    expect(await screen.findByText('72')).toBeTruthy();
    expect(screen.getByText('Degraded')).toBeTruthy();
    expect(screen.getByText('400')).toBeTruthy();

    const all = urls(f);
    expect(all).toHaveLength(2);
    expect(all).toContain(`/v1/tenants/7/stats/summary?${QUERY_7D}`);
    expect(all).toContain(`/v1/tenants/7/stats/health-score?${QUERY_7D}`);
    expect(new Set(all.map((u) => u.split('?')[1])).size).toBe(1);
  });

  it('a 500 from health-score shows an error card for that widget and summary numbers still render', async () => {
    seedTenant('7');
    const f = router({ health: () => jsonResponse({ detail: 'boom' }, 500) });
    const { container } = renderPage(f);

    expect(await screen.findByText('400')).toBeTruthy();
    expect(screen.getByText('Signal overview')).toBeTruthy();
    expect(screen.getByText('5%')).toBeTruthy();

    const card = await waitFor(() => {
      const el = container.querySelector<HTMLElement>('.widget-source-error[data-widget="health-score"]');
      expect(el).not.toBeNull();
      return el!;
    });
    expect(card.getAttribute('role')).toBe('alert');
    expect(card.textContent).toContain('Tracking health score');
    expect(card.textContent).toContain('Could not load the data');
    expect(card.textContent).toContain('Unexpected error (500).');
    expect(card.querySelector('button')?.textContent).toBe('Retry');
    expect(screen.queryByText('Degraded')).toBeNull();
    expect(errorCards(container)).toEqual(['health-score']);
  });

  it('a 500 from summary keeps the health widget and shows error cards for the summary widgets', async () => {
    seedTenant('7');
    const f = router({ summary: () => jsonResponse({ detail: 'boom' }, 500) });
    const { container } = renderPage(f);

    expect(await screen.findByText('72')).toBeTruthy();
    expect(screen.getByText('Degraded')).toBeTruthy();
    expect(screen.getByText('1 open incident')).toBeTruthy();

    await waitFor(() => expect(errorCards(container)).toHaveLength(4));
    expect(errorCards(container).sort()).toEqual(['compare-bars', 'compare-bars', 'kpi-group', 'window-note']);
    expect(screen.queryByText('400')).toBeNull();
    // The error cards keep their widget titles; the numbers do not render.
    expect(screen.getByText('Signal overview')).toBeTruthy();
    expect(container.querySelector('.widget-source-error[data-widget="health-score"]')).toBeNull();
  });

  it('Retry on a failed health-score card refetches it and recovers', async () => {
    seedTenant('7');
    let healthCalls = 0;
    const f = router({
      health: () => {
        healthCalls += 1;
        return healthCalls === 1 ? jsonResponse({ detail: 'boom' }, 500) : jsonResponse(healthScoreEnvelope);
      },
    });
    const { container } = renderPage(f);

    const retry = await waitFor(() => {
      const button = container.querySelector<HTMLButtonElement>('.widget-source-error button');
      expect(button).not.toBeNull();
      return button!;
    });
    fireEvent.click(retry);

    // Retry reloads every source of the page, so both come back.
    expect(await screen.findByText('72')).toBeTruthy();
    expect(screen.getByText('400')).toBeTruthy();
    expect(healthCalls).toBe(2);
    expect(errorCards(container)).toEqual([]);
  });

  it('changing the date range refetches both sources with the new window', async () => {
    seedTenant('7');
    const f = router();
    renderPage(f);
    await screen.findByText('72');

    fireEvent.click(screen.getByRole('button', { name: 'Last 30 days' }));

    await waitFor(() => expect(f).toHaveBeenCalledTimes(4));
    const after = urls(f).slice(2);
    expect(after).toContain(`/v1/tenants/7/stats/summary?${QUERY_30D}`);
    expect(after).toContain(`/v1/tenants/7/stats/health-score?${QUERY_30D}`);
    expect(await screen.findByText('72')).toBeTruthy();
  });

  it('shows the page loading state while both sources are in flight', () => {
    seedTenant('7');
    const f = router({
      summary: () => new Promise<Response>(() => undefined),
      health: () => new Promise<Response>(() => undefined),
    });
    renderPage(f);
    expect(document.querySelector('[aria-busy="true"]')).not.toBeNull();
    expect(screen.queryByText('72')).toBeNull();
  });

  it('shows one page-level error card when every source fails', async () => {
    seedTenant('7');
    const f = router({
      summary: () => jsonResponse({ detail: 'boom' }, 500),
      health: () => jsonResponse({ detail: 'boom' }, 500),
    });
    renderPage(f);
    expect(await screen.findByText('Could not load the data')).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toContain('Unexpected error (500).');
  });
});

describe('DashboardPage on the ROAS route (summary only)', () => {
  beforeEach(() => window.localStorage.clear());
  afterEach(cleanup);

  it('makes no health-score request', async () => {
    seedTenant('7');
    const f = router();
    renderPage(f, 'roas');

    expect(await screen.findByText('Orders placed')).toBeTruthy();
    expect(f).toHaveBeenCalledTimes(1);
    expect(urls(f)).toEqual([`/v1/tenants/7/stats/summary?${QUERY_7D}`]);
    expect(urls(f).some((u) => u.includes('health-score'))).toBe(false);
  });
});
