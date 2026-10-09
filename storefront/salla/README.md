# Motahai attribution capture for Salla (S2-8, hardened in FX-2)

Joins every Salla order to the ad that drove it (`fbp`, `fbc`, `utm_*`, `ad_id`, `ttclid`, `sccid`, plus the buyer's IP
and user agent) so `DeliveredPurchase` reaches Meta with full Event Match Quality.

## How it works

1. `storefront/salla/salla_capture.js` runs on every storefront page. It captures URL parameters (`utm_*`, `fbclid`,
   `ttclid`, `sccid`, `mt_ad`) and Meta cookies (`_fbp`, `_fbc`) and keeps the last click for 7 days in
   `localStorage` (`mt_attr`).
2. On the thank-you / order page it finds `order_id`, `order_total` and `currency` and sends
   `POST <CORE_HOST>/v1/capture/<merchant_id>` with body
   `{"order_id": "...", "order_total": 350.5, "currency": "SAR", "attribution": {...}}`
   via `navigator.sendBeacon` (fallback `fetch` with `keepalive`). Content type is `text/plain`, so it is a CORS
   "simple request" and never triggers a preflight. The script sends NO IP and NO user agent: the server derives
   them from the connection and the `User-Agent` header (a `client_ip`/`user_agent` in the body would be ignored).
3. The server stores the capture in a quarantine table (`pending_captures`, IP/UA Fernet-encrypted, 48 h TTL). It
   never creates or edits an order. Every accepted request gets an empty `204`, whether or not the order exists.
4. A scheduled job (`capture.merge_pending_captures`, every 15 min) joins a capture to the order created by the
   signed Salla webhook, only when tenant and order id match AND the captured total matches the order total
   (within max(1%, 1.0 currency unit), same currency). It fills only EMPTY attribution columns and stores IP/UA only if
   the order has no checkout context yet. Mismatches are rejected (`total_mismatch`), extra captures for the same order
   are rejected (`duplicate_capture`), unmatched ones expire after 48 h.

## Setup (per Salla store)

Embed the snippet (Salla theme / custom code) with:

```html
<script>window.MOTAHAI_CONFIG = { core_host: "https://<CORE_HOST>", merchant_id: "<SALLA_MERCHANT_ID>" };</script>
<script src="https://<where-you-host-it>/salla_capture.js" defer></script>
```

`core_host` must be `https://` (without it the script sends nothing). `merchant_id` must equal `tenants.shop_domain`
(the Salla merchant id). Optional overrides if auto-detection fails on the store's theme: `order_id`,
`order_total`, `currency`, `is_thank_you`.

**CORS requirement: the tenant's `storefront_url` MUST be set** (e.g. `https://shop.example.com`). The endpoint answers
only requests whose `Origin` equals the origin of `storefront_url` (scheme + host + non-default port); a different or
missing Origin gets `403`, and a Salla tenant with no `storefront_url` accepts nothing. If the store is reachable on
several hostnames (apex and `www`, custom domain and `*.salla.sa`), set `storefront_url` to the one customers check
out on; others are rejected. Behind a reverse proxy set `MOTAHAI_TRUSTED_PROXY=1` ONLY if the proxy overwrites
`X-Forwarded-For` with the real client address (nginx: `proxy_set_header X-Forwarded-For $remote_addr;`). Without it,
the proxy's address is seen, which is private/non-public and is therefore dropped (no IP stored).

Limits: body 4 KB, 30 requests/min per IP, 600/min per tenant (in-process; multiple workers need a shared limiter).

## Privacy and safety

- Customer email, phone and name are never read or sent. The server stores no PII beyond the encrypted IP/UA, which
  are purged once merged/rejected (on the capture row) and on send or after 14 days (on the order's checkout context).
- The script is wrapped in try/catch everywhere and is a no-op whenever order data is not found.

## MUST be verified on a real Salla store (every selector below is inferred)

- How the thank-you page exposes the order id, total and currency. The script tries `salla.config.get('order.id' |
  'order.total' | 'order.currency')`, `salla.order.id/total/currency`, `[data-order-id|total|currency]` and the
  `/orders/<id>` URL. Confirm one works, or pass the values through `MOTAHAI_CONFIG`.
- That the id shown on the page is the SAME id Salla's webhooks carry in `data.id` (not `reference_id`); otherwise
  captures never match and simply expire.
- That the page total equals the webhook's `amounts.total` (shipping, COD fee, tax, discounts included in the same
  way). A systematic gap above max(1%, 1.0) makes every capture `total_mismatch`; if so, adjust what the script reads.
- That `salla.consent` / theme events (`salla:order:completed`) fire as assumed and that `sendBeacon` cross-origin to
  `<CORE_HOST>` reaches the endpoint (check for 204 and `Access-Control-Allow-Origin` in the browser network panel).
- That `storefront_url`'s origin equals the `Origin` header the browser sends from the thank-you page.

Residual risk: an attacker who knows the exact total of a specific future order could still get a forged capture
merged for it (the total is a second factor, not a secret). See `src/ameen_workforce/capture.py`.

## Testing

```bash
node storefront/salla/test_capture.mjs
python -m pytest tests/test_capture.py
```
