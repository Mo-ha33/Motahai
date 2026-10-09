/*! salla_capture.js - ad attribution & thank-you page capture for Salla storefronts (no dependencies, ES2017).
 * Reads click params (utm_*, fbclid, ttclid, ScCid/sccid, mt_ad) + Meta cookies (_fbp/_fbc) on storefront visits,
 * stores the LAST click for 7 days in localStorage (mt_attr).
 * On the thank-you / order confirmation page, extracts the order_id, captures user_agent, fbp, fbc, utm_*, ad_id,
 * and transmits them to the Motahai capture endpoint and/or attaches them to Salla order notes.
 * Never reads or sends customer email/phone. Safe try/catch throughout to guarantee store stability.
 * Config override via window.MOTAHAI_CONFIG = { endpoint: '...', merchant_id: '...' }.
 */
(function () {
  'use strict';
  var W = window, D = document, TTL = 7 * 864e5, KEY = 'mt_attr', busy = false;
  var UTM = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_content'];

  function ls(k, v) { try { if (v === undefined) return W.localStorage.getItem(k); if (v === null) W.localStorage.removeItem(k); else W.localStorage.setItem(k, v); } catch (e) {} return null; }
  function ss(k, v) { try { if (v === undefined) return W.sessionStorage.getItem(k); if (v === null) W.sessionStorage.removeItem(k); else W.sessionStorage.setItem(k, v); } catch (e) {} return null; }
  function cookie(n) {
    try {
      var m = ('; ' + D.cookie).split('; ' + n + '=');
      return m.length > 1 ? decodeURIComponent(m.pop().split(';')[0]) : '';
    } catch (e) { return ''; }
  }
  function clean(v, n) { return typeof v === 'string' ? v.replace(/[\u0000-\u001f\u007f]/g, '').trim().slice(0, n || 255) : ''; }

  function allowed() {
    try {
      // If Salla or a merchant consent API exists
      var s = W.salla && W.salla.consent;
      if (s && typeof s.isAllowed === 'function') return !!s.isAllowed('marketing');
      var p = W.Shopify && W.Shopify.customerPrivacy;
      if (p && typeof p.marketingAllowed === 'function') return !!p.marketingAllowed();
      return true;
    } catch (e) { return false; }
  }

  function readClick() {
    var q = {}, any = false, a = {}, k;
    try {
      new URLSearchParams(W.location.search).forEach(function (v, key) { q[key.toLowerCase()] = v; });
      UTM.forEach(function (n) { a[n] = clean(q[n]); });
      a.ttclid = clean(q.ttclid);
      a.sccid = clean(q.sccid || q.scclid);
      var fbclid = clean(q.fbclid), ad = clean(q.mt_ad, 64), uc = a.utm_content;
      a.ad_id = /^\d+$/.test(ad) ? ad : (/^\d+$/.test(uc) ? uc : '');
      for (k in a) if (a[k]) any = true;
      if (fbclid) {
        any = true;
        var c = cookie('_fbc');
        a.fbc = c && c.split('.').pop() === fbclid ? c : 'fb.1.' + Date.now() + '.' + fbclid;
      }
    } catch (e) {}
    return any ? a : null;
  }

  function load() {
    try {
      var r = JSON.parse(ls(KEY) || 'null');
      if (r && typeof r.t === 'number' && r.a && Date.now() - r.t < TTL && Date.now() >= r.t) return r;
      if (r) ls(KEY, null);
    } catch (e) { ls(KEY, null); }
    return null;
  }

  function getAttribution(rec) {
    var a = (rec && rec.a) || {}, o = {};
    UTM.forEach(function (n) { if (a[n]) o[n] = a[n]; });
    if (a.ad_id) o.ad_id = a.ad_id;
    var fbp = clean(cookie('_fbp'));
    if (fbp) o.fbp = fbp;
    var fbc = a.fbc || clean(cookie('_fbc'));
    if (fbc) o.fbc = fbc;
    if (a.ttclid) o.ttclid = a.ttclid;
    if (a.sccid) o.sccid = a.sccid;
    return o;
  }

  function findMerchantId() {
    try {
      var cfg = W.MOTAHAI_CONFIG || {};
      if (cfg.merchant_id || cfg.merchant) return String(cfg.merchant_id || cfg.merchant);
      if (W.salla && W.salla.config) {
        var m = W.salla.config.get('merchant.id') || W.salla.config.get('store.id') || W.salla.config.get('merchant_id');
        if (m) return String(m);
      }
      if (W.salla && W.salla.merchant_id) return String(W.salla.merchant_id);
      if (D.currentScript && D.currentScript.getAttribute('data-merchant-id')) {
        return D.currentScript.getAttribute('data-merchant-id');
      }
    } catch (e) {}
    return '';
  }

  function findOrderId() {
    try {
      var cfg = W.MOTAHAI_CONFIG || {};
      if (cfg.order_id) return String(cfg.order_id);
      if (W.salla && W.salla.config) {
        var o = W.salla.config.get('order.id') || W.salla.config.get('order_id') || W.salla.config.get('order.reference_id');
        if (o) return String(o);
      }
      if (W.salla && W.salla.order && W.salla.order.id) return String(W.salla.order.id);
      if (W.sallaOrder && W.sallaOrder.id) return String(W.sallaOrder.id);
      var m = (W.location.pathname || '').match(/\/orders\/([0-9a-zA-Z_-]+)/i);
      if (m && m[1]) return m[1];
      var q = new URLSearchParams(W.location.search);
      var qId = q.get('order_id') || q.get('order') || q.get('id');
      if (qId && /^\d+$/.test(qId)) return qId;
      var el = D.querySelector('[data-order-id]');
      if (el && el.getAttribute('data-order-id')) return el.getAttribute('data-order-id');
    } catch (e) {}
    return '';
  }

  function isThankYouPage() {
    try {
      if (W.MOTAHAI_CONFIG && W.MOTAHAI_CONFIG.is_thank_you) return true;
      if (W.salla && W.salla.config) {
        var slug = W.salla.config.get('page.slug') || W.salla.config.get('page.name');
        if (slug === 'thank-you' || slug === 'order-completed' || slug === 'order-received') return true;
      }
      var p = (W.location.pathname || '').toLowerCase();
      if (p.indexOf('/thank-you') !== -1 || p.indexOf('/checkout/success') !== -1) return true;
      if (/\/orders\/[0-9a-zA-Z_-]+/i.test(p)) return true;
      if (D.querySelector('.order-details, .thank-you, [data-order-id]')) return true;
    } catch (e) {}
    return false;
  }

  function attachToSallaOrderNotes(orderId, attrs) {
    try {
      var noteLines = [];
      for (var k in attrs) {
        if (attrs[k]) noteLines.push('_mt_' + k + '=' + attrs[k]);
      }
      var noteText = noteLines.join('\n');
      if (!noteText) return;
      if (W.salla && W.salla.order && typeof W.salla.order.updateNote === 'function') {
        W.salla.order.updateNote({ id: orderId, note: noteText }).catch(function () {});
      } else if (W.salla && W.salla.cart && typeof W.salla.cart.comment === 'function') {
        W.salla.cart.comment(noteText).catch(function () {});
      }
    } catch (e) {}
  }

  function postCapture(merchantId, orderId, attrs) {
    try {
      var cfg = W.MOTAHAI_CONFIG || {};
      var endpoint = cfg.endpoint || '/storefront/capture';
      var ua = (W.navigator && W.navigator.userAgent) ? clean(W.navigator.userAgent, 512) : '';
      var body = JSON.stringify({
        platform: 'salla',
        merchant: merchantId,
        order_id: String(orderId),
        attribution: attrs,
        user_agent: ua,
        timestamp: Date.now()
      });

      if (typeof fetch === 'function') {
        fetch(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: body,
          keepalive: true
        }).catch(function () {});
      } else if (W.navigator && typeof W.navigator.sendBeacon === 'function') {
        var blob = new Blob([body], { type: 'application/json' });
        W.navigator.sendBeacon(endpoint, blob);
      }
    } catch (e) {}
  }

  function sync() {
    if (busy || !allowed()) return;
    busy = true;
    try {
      var click = readClick(), rec = load();
      if (click) {
        rec = { t: Date.now(), a: click };
        ls(KEY, JSON.stringify(rec));
      }

      if (isThankYouPage()) {
        var orderId = findOrderId();
        var merchantId = findMerchantId();
        if (orderId) {
          var lockKey = 'mt_salla_captured_' + orderId;
          if (!ss(lockKey)) {
            var attrs = getAttribution(rec);
            attachToSallaOrderNotes(orderId, attrs);
            postCapture(merchantId, orderId, attrs);
            ss(lockKey, '1');
          }
        }
      }
    } catch (e) {
    } finally {
      busy = false;
    }
  }

  function safe() { try { sync(); } catch (e) { busy = false; } }

  // Execute immediately
  safe();

  // Listen for consent or Salla events if available
  try {
    D.addEventListener('visitorConsentCollected', safe);
    D.addEventListener('salla:order:completed', safe);
    if (W.salla && W.salla.event && typeof W.salla.event.orderCompleted === 'function') {
      W.salla.event.orderCompleted(safe);
    }
  } catch (e) {}
})();
