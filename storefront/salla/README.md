# Salla attribution capture (S1-3): not built, known gap

There is no confirmed way yet for a Salla app to attach attribution data (UTMs, ad id, fbp/fbc, click ids) to an
order, so `webhook_listener.parse_salla_order` returns `attribution`, `client_ip` and `user_agent` as `None`.
Salla orders therefore have empty attribution columns and `DeliveredPurchase` for Salla goes out with email/phone
hashes only (lower match quality), until this is solved. See `docs/research/S1-6_platform_lookups.md`.

## Options to investigate

1. App Snippets: Salla apps can inject a JavaScript snippet into the storefront. A snippet could reuse the logic of
   `storefront/shopify/assets/motahai-capture.js` (same click rules, same 7-day window, same consent gating).
2. Getting the captured values onto the order. Candidates, none verified:
   - Salla order notes or custom fields set through the Merchant API, from the snippet (storefront-side API
     availability and permissions unknown) or from the app backend keyed by a session/cart id.
   - The snippet posts the values to a Motahai endpoint together with a Salla cart/session identifier, and the
     backend joins them to the order on `order.created` (needs an identifier present in both).
3. Check what the Salla order webhook already returns (for example referrer or UTM fields); the payload was not
   inspected for this.

## Constraints to keep

- Do not read or send email/phone from the storefront.
- Respect the consent state of the merchant's banner.
- IP and user agent stay pass-through only (never stored).
