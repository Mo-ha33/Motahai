// Node test for storefront/salla/salla_capture.js: runs the script in a vm with a fake window/document/storage/fetch.
// Usage: node storefront/salla/test_capture.mjs   (exit code 1 on failure)
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const SCRIPT = fs.readFileSync(
  path.join(path.dirname(fileURLToPath(import.meta.url)), 'salla_capture.js'), 'utf8');
const DAY = 864e5;
const CORE = 'https://core.example.com';

function makeStorage(throwing = false) {
  const m = new Map();
  return {
    getItem: (k) => { if (throwing) throw new Error('blocked'); return m.has(k) ? m.get(k) : null; },
    setItem: (k, v) => { if (throwing) throw new Error('blocked'); m.set(k, String(v)); },
    removeItem: (k) => { if (throwing) throw new Error('blocked'); m.delete(k); },
    _m: m,
  };
}

function makeBrowser() {
  return {
    local: makeStorage(),
    session: makeStorage(),
    now: 1_800_000_000_000,
    cookies: {},
    calls: [],
    sallaNotes: [],
    fetchOk: true
  };
}

async function load(b, {
  search = '',
  pathname = '/',
  salla,
  motahaiConfig,
  fetchImpl,
  localStorageThrows = false,
  beacon = true,
  userAgent = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)'
} = {}) {
  const listeners = {};
  const FakeDate = class extends Date { static now() { return b.now; } };
  const document = {
    get cookie() { return Object.entries(b.cookies).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join('; '); },
    addEventListener: (n, fn) => { (listeners[n] = listeners[n] || []).push(fn); },
    querySelector: (sel) => {
      if (pathname.includes('/thank-you') && sel.includes('thank-you')) {
        return { getAttribute: () => null };
      }
      return null;
    },
    currentScript: null,
  };
  const window = {
    location: { search, pathname },
    localStorage: localStorageThrows ? makeStorage(true) : b.local,
    sessionStorage: b.session,
    salla: salla || {
      config: {
        get: (k) => {
          if (k === 'page.slug') return pathname.includes('thank-you') ? 'thank-you' : 'home';
          if (k === 'merchant.id') return '1234567';
          return null;
        }
      }
    },
    MOTAHAI_CONFIG: motahaiConfig === undefined ? { core_host: CORE } : motahaiConfig,
    navigator: {
      userAgent,
      ...(beacon ? { sendBeacon: (url, blob) => {
        b.calls.push({ url, via: 'beacon', type: blob.type, body: JSON.parse(blob.text) });
        return true;
      } } : {}),
    },
  };
  const fetch = fetchImpl || ((url, opts) => {
    b.calls.push({ url, opts, via: 'fetch', body: JSON.parse(opts.body) });
    return Promise.resolve({ ok: b.fetchOk });
  });

  const ctx = vm.createContext({
    window,
    document,
    fetch,
    URLSearchParams,
    Date: FakeDate,
    Blob: class { constructor(parts, o) { this.text = parts.join(''); this.type = (o && o.type) || ''; } },
    JSON,
    Object,
    String,
    Number,
    Promise,
    decodeURIComponent
  });
  vm.runInContext(SCRIPT, ctx);
  await new Promise((r) => setTimeout(r, 0));
  return {
    fire: async (n) => {
      (listeners[n] || []).forEach((f) => f());
      await new Promise((r) => setTimeout(r, 0));
    }
  };
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test('landing page: captures click params and cookies into localStorage', async () => {
  const b = makeBrowser();
  b.cookies._fbp = 'fb.1.1700000000000.1234567890';
  await load(b, {
    search: '?utm_source=meta&utm_medium=paid&utm_campaign=111&utm_content=222&fbclid=ABC_d-1&ttclid=T1&ScCid=S1',
    pathname: '/products/item-1'
  });
  assert.equal(b.calls.length, 0, 'no POST on non-thank-you page');
  assert.ok(b.local._m.has('mt_attr'));
  const stored = JSON.parse(b.local.getItem('mt_attr'));
  assert.equal(stored.a.utm_source, 'meta');
  assert.equal(stored.a.ad_id, '222');
  assert.equal(stored.a.fbc, `fb.1.${b.now}.ABC_d-1`);
  assert.equal(stored.a.ttclid, 'T1');
  assert.equal(stored.a.sccid, 'S1');
});

test('thank-you page: beacon POST to <core>/v1/capture/<merchant> with order_id, total, currency, attribution (no ip/UA)', async () => {
  const b = makeBrowser();
  b.cookies._fbp = 'fb.1.1700000000000.1234567890';
  // Step 1: visit landing page
  await load(b, {
    search: '?utm_source=meta&utm_medium=paid&utm_campaign=111&utm_content=222&fbclid=ABC_d-1',
    pathname: '/products/item-1'
  });
  // Step 2: navigate to thank-you page
  const salla = {
    config: {
      get: (k) => {
        if (k === 'page.slug') return 'thank-you';
        if (k === 'merchant.id') return '1234567';
        if (k === 'order.id') return '998877';
        if (k === 'order.total') return 350.5;
        if (k === 'order.currency') return 'sar';
        return null;
      }
    }
  };
  await load(b, { pathname: '/orders/998877', salla });

  assert.equal(b.calls.length, 1);
  assert.equal(b.calls[0].url, `${CORE}/v1/capture/1234567`);
  assert.equal(b.calls[0].via, 'beacon');
  assert.ok(b.calls[0].type.startsWith('text/plain'), 'simple request: no preflight');
  const payload = b.calls[0].body;
  assert.equal(payload.order_id, '998877');
  assert.equal(payload.order_total, 350.5);
  assert.equal(payload.currency, 'SAR');
  for (const k of ['user_agent', 'client_ip', 'ip', 'merchant', 'platform']) assert.ok(!(k in payload), `no ${k} in body`);
  assert.equal(payload.attribution.utm_source, 'meta');
  assert.equal(payload.attribution.ad_id, '222');
  assert.equal(payload.attribution.fbp, 'fb.1.1700000000000.1234567890');
  assert.equal(payload.attribution.fbc, `fb.1.${b.now}.ABC_d-1`);
});

test('attaches to order note via salla.order.updateNote when available', async () => {
  const b = makeBrowser();
  b.cookies._fbp = 'fb.1.1700000000000.1234567890';
  let noteSent = null;
  const salla = {
    config: {
      get: (k) => {
        if (k === 'page.slug') return 'thank-you';
        if (k === 'merchant.id') return '1234567';
        if (k === 'order.id') return '554433';
        if (k === 'order.total') return '100';
        if (k === 'order.currency') return 'EGP';
        return null;
      }
    },
    order: {
      id: '554433',
      updateNote: async (arg) => { noteSent = arg; return { status: 200 }; }
    }
  };
  await load(b, {
    search: '?utm_source=tiktok&utm_content=777&mt_ad=777',
    pathname: '/orders/554433',
    salla
  });

  assert.equal(b.calls.length, 1);
  assert.ok(noteSent !== null);
  assert.equal(noteSent.id, '554433');
  assert.ok(noteSent.note.includes('_mt_utm_source=tiktok'));
  assert.ok(noteSent.note.includes('_mt_ad_id=777'));
});

test('deduplication: reload of thank-you page does not send second capture POST', async () => {
  const b = makeBrowser();
  const salla = {
    config: {
      get: (k) => {
        if (k === 'page.slug') return 'thank-you';
        if (k === 'merchant.id') return '1234567';
        if (k === 'order.id') return '1001';
        if (k === 'order.total') return '100';
        if (k === 'order.currency') return 'EGP';
        return null;
      }
    }
  };
  await load(b, { search: '?utm_source=meta&utm_content=555', pathname: '/orders/1001', salla });
  assert.equal(b.calls.length, 1);
  // Reload same page in same session
  await load(b, { pathname: '/orders/1001', salla });
  assert.equal(b.calls.length, 1, 'deduplicated via sessionStorage');
});

test('expired click (> 7 days) is cleared and not attached', async () => {
  const b = makeBrowser();
  await load(b, { search: '?utm_source=meta&utm_content=555', pathname: '/products/p1' });
  b.now += 8 * DAY;
  const salla = {
    config: {
      get: (k) => {
        if (k === 'page.slug') return 'thank-you';
        if (k === 'merchant.id') return '1234567';
        if (k === 'order.id') return '2002';
        if (k === 'order.total') return '100';
        if (k === 'order.currency') return 'EGP';
        return null;
      }
    }
  };
  await load(b, { pathname: '/orders/2002', salla });
  assert.equal(b.calls.length, 1);
  assert.deepEqual(b.calls[0].body.attribution, {});
  assert.equal(b.local._m.has('mt_attr'), false, 'expired record removed');
});

test('never throws on storage failure or network failure', async () => {
  const b = makeBrowser();
  const salla = {
    config: {
      get: () => 'thank-you'
    }
  };
  await load(b, {
    search: '?utm_source=meta&utm_content=555',
    pathname: '/orders/3003',
    salla,
    localStorageThrows: true,
    beacon: false,
    fetchImpl: () => Promise.reject(new Error('Network down'))
  });
});

function sallaOrder(id, extra = {}) {
  const vals = { 'page.slug': 'thank-you', 'merchant.id': '1234567', 'order.id': id, ...extra };
  return { config: { get: (k) => (k in vals ? vals[k] : null) } };
}

test('no beacon support: falls back to fetch keepalive with text/plain, no credentials', async () => {
  const b = makeBrowser();
  await load(b, { pathname: '/orders/7001', beacon: false,
    salla: sallaOrder('7001', { 'order.total': '1,250.00', 'order.currency': 'EGP' }) });
  assert.equal(b.calls.length, 1);
  assert.equal(b.calls[0].via, 'fetch');
  assert.equal(b.calls[0].url, `${CORE}/v1/capture/1234567`);
  assert.equal(b.calls[0].opts.keepalive, true);
  assert.equal(b.calls[0].opts.credentials, 'omit');
  assert.ok(b.calls[0].opts.headers['Content-Type'].startsWith('text/plain'));
  assert.equal(b.calls[0].body.order_total, 1250);
});

test('no-op when order total or currency cannot be found', async () => {
  const b = makeBrowser();
  await load(b, { pathname: '/orders/7002', salla: sallaOrder('7002') });
  assert.equal(b.calls.length, 0);
});

test('no-op when core_host is missing or not https', async () => {
  for (const cfg of [{}, { core_host: 'http://core.example.com' }, { core_host: 'javascript:alert(1)' }]) {
    const b = makeBrowser();
    await load(b, { pathname: '/orders/7003', motahaiConfig: cfg,
      salla: sallaOrder('7003', { 'order.total': 10, 'order.currency': 'EGP' }) });
    assert.equal(b.calls.length, 0);
  }
});

test('MOTAHAI_CONFIG overrides supply order_total / currency / merchant', async () => {
  const b = makeBrowser();
  await load(b, { pathname: '/orders/7004', motahaiConfig: { core_host: `${CORE}/`, merchant_id: '42', order_total: 99.9, currency: 'sar' },
    salla: sallaOrder('7004') });
  assert.equal(b.calls.length, 1);
  assert.equal(b.calls[0].url, `${CORE}/v1/capture/42`);
  assert.equal(b.calls[0].body.order_total, 99.9);
  assert.equal(b.calls[0].body.currency, 'SAR');
});

let failed = 0;
for (const [name, fn] of tests) {
  try {
    await fn();
    console.log('ok   -', name);
  } catch (e) {
    failed++;
    console.log('FAIL -', name, '\n', e.message);
  }
}
console.log(`${tests.length - failed}/${tests.length} passed`);
process.exit(failed ? 1 : 0);
