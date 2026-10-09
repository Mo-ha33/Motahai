/*! motahai-capture.js - ad attribution capture for Shopify (no dependencies, ES2017).
 * Reads click params (utm_*, fbclid, ttclid, ScCid/sccid, mt_ad) + Meta cookies (_fbp/_fbc), keeps the LAST click
 * for 7 days in localStorage (mt_attr), and writes hidden cart attributes (_mt_*) that Shopify copies to the
 * order's note_attributes. Never reads or sends email/phone. Every path is try/catch: it must never break a store.
 * Consent: if Shopify.customerPrivacy exists, nothing is stored or sent until marketingAllowed() is true
 * (one switch for all fields: click ids + fbp/fbc are marketing identifiers); re-runs on visitorConsentCollected.
 * If the API is absent the script proceeds (the merchant owns the consent banner). */
(function () {
  'use strict';
  var W = window, D = document, TTL = 7 * 864e5, KEY = 'mt_attr', HKEY = 'mt_attr_h', busy = false;
  var UTM = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_content'];

  function ls(k, v) { try { if (v === undefined) return W.localStorage.getItem(k); if (v === null) W.localStorage.removeItem(k); else W.localStorage.setItem(k, v); } catch (e) {} return null; }
  function ss(k, v) { try { if (v === undefined) return W.sessionStorage.getItem(k); W.sessionStorage.setItem(k, v); } catch (e) {} return null; }
  function cookie(n) {
    try {
      var m = ('; ' + D.cookie).split('; ' + n + '=');
      return m.length > 1 ? decodeURIComponent(m.pop().split(';')[0]) : '';
    } catch (e) { return ''; }
  }
  function hash(s) { var h = 5381, i = s.length; while (i) h = (h * 33) ^ s.charCodeAt(--i); return String(h >>> 0); }
  function clean(v, n) { return typeof v === 'string' ? v.replace(/[\u0000-\u001f\u007f]/g, '').trim().slice(0, n || 255) : ''; }

  function allowed() {
    try {
      var p = W.Shopify && W.Shopify.customerPrivacy;
      return !p || typeof p.marketingAllowed !== 'function' ? true : !!p.marketingAllowed();
    } catch (e) { return false; } // fail closed when the consent API misbehaves
  }

  // Click params of the CURRENT url, or null when the visit carries none (so it never overwrites a stored click).
  function readClick() {
    var q = {}, any = false, a = {}, k;
    new URLSearchParams(W.location.search).forEach(function (v, key) { q[key.toLowerCase()] = v; });
    UTM.forEach(function (n) { a[n] = clean(q[n]); });
    a.ttclid = clean(q.ttclid);
    a.sccid = clean(q.sccid || q.scclid); // ScCid is matched case-insensitively
    var fbclid = clean(q.fbclid), ad = clean(q.mt_ad, 64), uc = a.utm_content;
    a.ad_id = /^\d+$/.test(ad) ? ad : (/^\d+$/.test(uc) ? uc : '');
    for (k in a) if (a[k]) any = true;
    if (fbclid) {
      any = true;
      var c = cookie('_fbc'); // reuse the pixel's cookie only if it belongs to this click
      a.fbc = c && c.split('.').pop() === fbclid ? c : 'fb.1.' + Date.now() + '.' + fbclid;
    }
    return any ? a : null;
  }

  function load() {
    try {
      var r = JSON.parse(ls(KEY) || 'null');
      if (r && typeof r.t === 'number' && r.a && Date.now() - r.t < TTL && Date.now() >= r.t) return r;
      if (r) ls(KEY, null); // expired or malformed
    } catch (e) { ls(KEY, null); }
    return null;
  }

  function attributes(rec) {
    var a = (rec && rec.a) || {}, o = {};
    function put(k, v) { if (v) o['_mt_' + k] = v; }
    UTM.forEach(function (n) { put(n, a[n]); });
    put('ad_id', a.ad_id);
    put('fbp', clean(cookie('_fbp')));
    put('fbc', a.fbc || clean(cookie('_fbc')));
    put('ttclid', a.ttclid);
    put('sccid', a.sccid);
    if (rec && Object.keys(o).length) o._mt_ts = String(rec.t);
    return o;
  }

  function sync() {
    if (busy || !allowed()) return;
    var click = readClick(), rec = load();
    if (click) { rec = { t: Date.now(), a: click }; ls(KEY, JSON.stringify(rec)); } // last non-empty click wins
    var attrs = attributes(rec);
    if (!Object.keys(attrs).length) return;
    var body = JSON.stringify({ attributes: attrs }), sig = hash(body);
    // The cart cookie changes after an order completes (new cart), so it is part of the "unchanged" signature.
    if (ss(HKEY) === hash(cookie('cart') + '|' + sig)) return;
    busy = true;
    fetch('/cart/update.js', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body, keepalive: true })
      .then(function (res) { if (res && res.ok) ss(HKEY, hash(cookie('cart') + '|' + sig)); })
      .catch(function () {})
      .then(function () { busy = false; });
  }

  function safe() { try { sync(); } catch (e) { busy = false; } }
  safe();
  try { D.addEventListener('visitorConsentCollected', safe); } catch (e) {}
})();
