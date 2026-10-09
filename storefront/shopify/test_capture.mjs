// Node test for assets/motahai-capture.js: runs the script in a vm with a fake window/document/storage/fetch.
// Usage: node storefront/shopify/test_capture.mjs   (exit code 1 on failure)
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const SCRIPT = fs.readFileSync(
  path.join(path.dirname(fileURLToPath(import.meta.url)), 'assets', 'motahai-capture.js'), 'utf8');
const DAY = 864e5;

function makeStorage(throwing = false) {
  const m = new Map();
  return {
    getItem: (k) => { if (throwing) throw new Error('blocked'); return m.has(k) ? m.get(k) : null; },
    setItem: (k, v) => { if (throwing) throw new Error('blocked'); m.set(k, String(v)); },
    removeItem: (k) => { if (throwing) throw new Error('blocked'); m.delete(k); },
    _m: m,
  };
}

// One "browser": persistent localStorage/clock across page loads; sessionStorage is reset per session.
function makeBrowser() {
  return { local: makeStorage(), session: makeStorage(), now: 1_800_000_000_000, cookies: {}, calls: [], fetchOk: true };
}

async function load(b, { search = '', shopify, fetchImpl, localStorageThrows = false } = {}) {
  const listeners = {};
  const FakeDate = class extends Date { static now() { return b.now; } };
  const document = {
    get cookie() { return Object.entries(b.cookies).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join('; '); },
    addEventListener: (n, fn) => { (listeners[n] = listeners[n] || []).push(fn); },
  };
  const window = {
    location: { search }, localStorage: localStorageThrows ? makeStorage(true) : b.local, sessionStorage: b.session,
    Shopify: shopify,
  };
  const fetch = fetchImpl || ((url, opts) => {
    b.calls.push({ url, opts, attrs: JSON.parse(opts.body).attributes });
    return Promise.resolve({ ok: b.fetchOk });
  });
  const ctx = vm.createContext({ window, document, fetch, URLSearchParams, Date: FakeDate, JSON, Object, String, Number, Promise, decodeURIComponent });
  vm.runInContext(SCRIPT, ctx);
  await new Promise((r) => setTimeout(r, 0));
  return { fire: async (n) => { (listeners[n] || []).forEach((f) => f()); await new Promise((r) => setTimeout(r, 0)); } };
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test('captures click params, cookies and posts hidden cart attributes', async () => {
  const b = makeBrowser();
  b.cookies._fbp = 'fb.1.1700000000000.1234567890';
  await load(b, { search: '?utm_source=meta&utm_medium=paid&utm_campaign=111&utm_content=222&fbclid=ABC_d-1&ttclid=T1&ScCid=S1' });
  assert.equal(b.calls.length, 1);
  assert.equal(b.calls[0].url, '/cart/update.js');
  assert.equal(b.calls[0].opts.method, 'POST');
  assert.equal(b.calls[0].opts.headers['Content-Type'], 'application/json');
  assert.deepEqual(b.calls[0].attrs, {
    _mt_utm_source: 'meta', _mt_utm_medium: 'paid', _mt_utm_campaign: '111', _mt_utm_content: '222',
    _mt_ad_id: '222', _mt_fbp: 'fb.1.1700000000000.1234567890', _mt_fbc: `fb.1.${b.now}.ABC_d-1`,
    _mt_ttclid: 'T1', _mt_sccid: 'S1', _mt_ts: String(b.now),
  });
  assert.ok(!/email|phone/i.test(b.calls[0].opts.body), 'never sends email/phone');
});

test('mt_ad wins over utm_content; non-digit utm_content gives no ad_id; sccid lowercase accepted', async () => {
  let b = makeBrowser();
  await load(b, { search: '?utm_content=222&mt_ad=999' });
  assert.equal(b.calls[0].attrs._mt_ad_id, '999');
  b = makeBrowser();
  await load(b, { search: '?utm_content=summer-sale&sccid=SX' });
  assert.equal(b.calls[0].attrs._mt_ad_id, undefined);
  assert.equal(b.calls[0].attrs._mt_utm_content, 'summer-sale');
  assert.equal(b.calls[0].attrs._mt_sccid, 'SX');
});

test('reuses the _fbc cookie when it belongs to the same fbclid, else builds a new one', async () => {
  let b = makeBrowser();
  b.cookies._fbc = 'fb.1.1700000000000.ABC';
  await load(b, { search: '?fbclid=ABC' });
  assert.equal(b.calls[0].attrs._mt_fbc, 'fb.1.1700000000000.ABC');
  b = makeBrowser();
  b.cookies._fbc = 'fb.1.1700000000000.OLD';
  await load(b, { search: '?fbclid=NEW' });
  assert.equal(b.calls[0].attrs._mt_fbc, `fb.1.${b.now}.NEW`);
});

test('last click within 7 days wins; a visit without params does not overwrite; expires after 7 days', async () => {
  const b = makeBrowser();
  await load(b, { search: '?utm_source=meta&utm_content=111' });
  const t1 = b.now;
  b.now += 2 * DAY;
  b.session = makeStorage(); // new session, no click params: stored click is reused
  await load(b, { search: '' });
  assert.equal(b.calls.length, 2);
  assert.equal(b.calls[1].attrs._mt_ad_id, '111');
  assert.equal(b.calls[1].attrs._mt_ts, String(t1));
  b.now += 1 * DAY;
  b.session = makeStorage();
  await load(b, { search: '?utm_source=tiktok&utm_content=333&ttclid=TT' });
  assert.equal(b.calls[2].attrs._mt_ad_id, '333');
  assert.equal(b.calls[2].attrs._mt_utm_source, 'tiktok');
  const t3 = b.now;
  b.now += 6 * DAY;
  b.session = makeStorage();
  await load(b, { search: '' });
  assert.equal(b.calls[3].attrs._mt_ad_id, '333');
  assert.equal(b.calls[3].attrs._mt_ts, String(t3));
  b.now += 2 * DAY; // 8 days after the last click
  b.session = makeStorage();
  await load(b, { search: '' });
  assert.equal(b.calls.length, 4, 'expired click: nothing to send');
  assert.equal(b.local._m.has('mt_attr'), false, 'expired record is removed');
});

test('consent denied writes nothing (storage or cart); granting later writes', async () => {
  const b = makeBrowser();
  b.cookies._fbp = 'fb.1.1700000000000.1234567890';
  let allowed = false;
  const shopify = { customerPrivacy: { marketingAllowed: () => allowed } };
  const page = await load(b, { search: '?utm_source=meta&utm_content=111', shopify });
  assert.equal(b.calls.length, 0);
  assert.equal(b.local._m.size, 0);
  assert.equal(b.session._m.size, 0);
  allowed = true;
  await page.fire('visitorConsentCollected');
  assert.equal(b.calls.length, 1);
  assert.equal(b.calls[0].attrs._mt_ad_id, '111');
  assert.ok(b.local._m.has('mt_attr'));
});

test('consent API that throws fails closed; absent API proceeds', async () => {
  let b = makeBrowser();
  await load(b, { search: '?utm_content=111', shopify: { customerPrivacy: { marketingAllowed() { throw new Error('x'); } } } });
  assert.equal(b.calls.length, 0);
  b = makeBrowser();
  await load(b, { search: '?utm_content=111', shopify: {} });
  assert.equal(b.calls.length, 1);
});

test('no second write when nothing changed in the same session; a new cart cookie triggers a rewrite', async () => {
  const b = makeBrowser();
  b.cookies.cart = 'cart-1';
  await load(b, { search: '?utm_content=111' });
  await load(b, { search: '' });
  await load(b, { search: '' });
  assert.equal(b.calls.length, 1);
  b.cookies.cart = 'cart-2'; // order completed, fresh cart
  await load(b, { search: '' });
  assert.equal(b.calls.length, 2);
});

test('a failed cart write is retried on the next page', async () => {
  const b = makeBrowser();
  b.fetchOk = false;
  await load(b, { search: '?utm_content=111' });
  b.fetchOk = true;
  await load(b, { search: '' });
  assert.equal(b.calls.length, 2);
  await load(b, { search: '' });
  assert.equal(b.calls.length, 2);
});

test('never throws: blocked storage, rejecting or missing fetch, nothing to send', async () => {
  let b = makeBrowser();
  await load(b, { search: '?utm_content=111', localStorageThrows: true });
  assert.equal(b.calls.length, 1, 'still writes the cart attributes for this visit');
  b = makeBrowser();
  await load(b, { search: '?utm_content=111', fetchImpl: () => Promise.reject(new Error('net')) });
  b = makeBrowser();
  await load(b, { search: '?utm_content=111', fetchImpl: () => { throw new Error('sync'); } });
  b = makeBrowser();
  await load(b, { search: '' });
  assert.equal(b.calls.length, 0, 'no click, no cookies: nothing to send');
});

let failed = 0;
for (const [name, fn] of tests) {
  try { await fn(); console.log('ok   -', name); }
  catch (e) { failed++; console.log('FAIL -', name, '\n', e.message); }
}
console.log(`${tests.length - failed}/${tests.length} passed`);
process.exit(failed ? 1 : 0);
