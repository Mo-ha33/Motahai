import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { SessionProvider } from '../auth/SessionContext';
import { ApiProvider } from '../dashboard/ApiContext';
import { fakeFetch, jsonResponse } from '../test/fakeFetch';
import { OnboardingPage } from './OnboardingPage';
import { TENANT_STORAGE_KEY } from './storage';

const SECRET = 'courier-secret-XYZ-123';
const API_KEY = 'mtk_full-key-ABCDEF';
const TOKEN = 'EAAB-super-secret-token';

function checklist(overrides: Record<string, unknown> = {}) {
  return {
    tenant_id: 7,
    mode: 'shadow',
    steps: [
      { key: 'tenant_created', label: 'Tenant created', required: true, manual: false, status: 'done', detail: '' },
      { key: 'api_key', label: 'Tenant API key issued', required: true, manual: false, status: 'missing', detail: '' },
      {
        key: 'platform_webhooks', label: 'Register shopify webhooks (MANUAL)', required: false, manual: true,
        status: 'manual', detail: 'https://x/webhooks/shopify/orders/create',
      },
    ],
    platform_webhook_urls: ['https://x/webhooks/shopify/orders/create'],
    ready_for_live: false,
    note: 'n',
    ...overrides,
  };
}

function backend(extra?: (url: string, init?: RequestInit) => Response | undefined) {
  return fakeFetch((url, init) => {
    const custom = extra?.(url, init);
    if (custom) return custom;
    const method = init?.method ?? 'GET';
    if (url.endsWith('/tenants') && method === 'POST') return jsonResponse({ tenant_id: 7 }, 201);
    if (url.endsWith('/meta')) return jsonResponse({ tenant_id: 7, meta_capi_token: 'configured (Fernet-encrypted)' });
    if (url.endsWith('/courier-secrets')) {
      return jsonResponse(
        { tenant_id: 7, secrets: { bosta: { url: 'https://x/webhooks/bosta/s', secret: SECRET } }, note: '' },
        201,
      );
    }
    if (url.endsWith('/api-key')) {
      return jsonResponse({ tenant_id: 7, key_id: 1, key_prefix: 'mtk_full', scopes: 'stats', api_key: API_KEY }, 201);
    }
    if (url.endsWith('/checklist')) return jsonResponse(checklist());
    return jsonResponse({ detail: 'nope' }, 404);
  });
}

function renderWizard(f: ReturnType<typeof fakeFetch>, mode: 'operator' | 'tenant' = 'operator') {
  const initial =
    mode === 'operator'
      ? ({ mode: 'operator' } as const)
      : ({ mode: 'tenant', key: 'mtk_x', profile: { tenant_id: 1, name: 'S', currency: 'EGP', country: 'EG', mode: 'shadow', platform: 'shopify' } } as const);
  return render(
    <SessionProvider initial={initial as never}>
      <ApiProvider fetchImpl={f}>
        <OnboardingPage lang="en" />
      </ApiProvider>
    </SessionProvider>,
  );
}

const type = (label: string | RegExp, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
const click = (name: string | RegExp) => fireEvent.click(screen.getByRole('button', { name }));

async function createTenant() {
  type('Shop domain', 'demo.myshopify.com');
  type(/Country/, 'eg');
  type(/Currency/, 'egp');
  click('Create tenant');
  await screen.findByRole('heading', { name: 'Connect Meta' });
}

describe('onboarding wizard', () => {
  beforeEach(() => window.sessionStorage.clear());
  afterEach(cleanup);

  it('tenant mode shows the operator-only card and makes no calls', () => {
    const f = backend();
    renderWizard(f, 'tenant');
    expect(screen.getByText('Operator only')).toBeTruthy();
    expect(f).not.toHaveBeenCalled();
  });

  it('happy path through every step; secrets shown once and gone after leaving', async () => {
    const f = backend();
    renderWizard(f);
    await createTenant();
    const createCall = f.mock.calls.find(([u]) => String(u).endsWith('/tenants'))!;
    expect(JSON.parse(String(createCall[1]?.body))).toEqual({
      shop_domain: 'demo.myshopify.com', platform: 'shopify', country: 'EG', currency: 'EGP',
    });
    expect(window.sessionStorage.getItem(TENANT_STORAGE_KEY)).toBe('7');

    // Meta: token input is a password field and is cleared after submit.
    const tokenInput = screen.getByLabelText('Conversions API token') as HTMLInputElement;
    expect(tokenInput.type).toBe('password');
    expect(tokenInput.autocomplete).toBe('off');
    type('Dataset ID', '123456');
    type('Conversions API token', TOKEN);
    click('Save Meta settings');
    expect(tokenInput.value).toBe('');
    await screen.findByRole('heading', { name: 'Courier webhook secrets' });
    const metaCall = f.mock.calls.find(([u]) => String(u).endsWith('/meta'))!;
    expect(JSON.parse(String(metaCall[1]?.body))).toEqual({ meta_dataset_id: '123456', meta_capi_token: TOKEN });

    // Courier secrets shown once.
    click('Generate secrets');
    await screen.findByText('Copy now');
    expect((screen.getByLabelText(/^Bosta . Secret$/) as HTMLInputElement).value).toBe(SECRET);
    expect(screen.getByText(/will not be shown again/)).toBeTruthy();
    click('Continue');
    await screen.findByRole('heading', { name: 'First API key' });
    expect(screen.queryByDisplayValue(SECRET)).toBeNull();

    // API key shown once; gone after navigating away and back.
    click('Issue key');
    await screen.findByDisplayValue(API_KEY);
    click('Continue');
    await screen.findByRole('heading', { name: 'Checklist' });
    expect(screen.queryByDisplayValue(API_KEY)).toBeNull();
    expect(screen.getByText(/separate operator action/)).toBeTruthy();
    expect(screen.getByDisplayValue('https://x/webhooks/shopify/orders/create')).toBeTruthy();
    click(/^4/);
    await screen.findByRole('heading', { name: 'First API key' });
    expect(screen.queryByDisplayValue(API_KEY)).toBeNull();

    // Nothing sensitive reached storage.
    const stored = JSON.stringify([window.sessionStorage, window.localStorage]) + JSON.stringify(Object.entries(window.sessionStorage));
    for (const secret of [SECRET, API_KEY, TOKEN]) expect(stored.includes(secret)).toBe(false);
    expect(window.sessionStorage.length).toBe(1);
  });

  it('copy button uses the clipboard', async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    window.sessionStorage.setItem(TENANT_STORAGE_KEY, '7');
    renderWizard(backend());
    await screen.findByDisplayValue('https://x/webhooks/shopify/orders/create');
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Copy:/ }));
    });
    expect(writeText).toHaveBeenCalledWith('https://x/webhooks/shopify/orders/create');
    await screen.findByText('Copied');
  });

  it('resumes at the checklist from the stored tenant id', async () => {
    window.sessionStorage.setItem(TENANT_STORAGE_KEY, '7');
    const f = backend();
    renderWizard(f);
    await screen.findByText('Tenant API key issued');
    expect(String(f.mock.calls[0]![0])).toBe('/v1/operator/onboarding/tenants/7/checklist');
  });

  it('continues an existing tenant by id', async () => {
    const f = backend();
    renderWizard(f);
    type('Tenant id', '7');
    click('Load checklist');
    await screen.findByText('Tenant API key issued');
    expect(window.sessionStorage.getItem(TENANT_STORAGE_KEY)).toBe('7');
  });

  it('renders a 409 inline with the server detail', async () => {
    const f = backend((url, init) =>
      url.endsWith('/tenants') && init?.method === 'POST'
        ? jsonResponse({ detail: 'A tenant with this shop_domain already exists' }, 409)
        : undefined,
    );
    renderWizard(f);
    type('Shop domain', 'dup.myshopify.com');
    type(/Country/, 'EG');
    type(/Currency/, 'EGP');
    click('Create tenant');
    const alert = await screen.findByRole('alert');
    await waitFor(() => expect(alert.textContent).toContain('Conflict'));
    expect(alert.textContent).toContain('A tenant with this shop_domain already exists');
    expect(window.sessionStorage.getItem(TENANT_STORAGE_KEY)).toBeNull();
  });
});
