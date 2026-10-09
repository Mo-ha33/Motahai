# Motahai attribution capture for Salla (S2-8)

Joins every Salla order to the ad that drove it, achieving parity with Shopify in user matching (`fbp`, `fbc`, `user_agent`, `utm_*`, `ad_id`, `client_ip`) for Meta Conversions API (`DeliveredPurchase`).

## How it works

1. `storefront/salla_capture.js` runs on every storefront page and the Thank-You / Order Confirmation page.
2. It captures URL parameters (`utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `fbclid`, `ttclid`, `sccid`, `mt_ad`) and Meta cookies (`_fbp`, `_fbc`).
3. Click data is saved with a 7-day TTL in `localStorage` under `mt_attr`.
4. On the Salla thank-you / order completion page:
   - Detects completion via Salla Twilight engine (`salla.config.get('page.slug') === 'thank-you'`), order URL path `/orders/<id>`, or event `salla:order:completed`.
   - Extracts `order_id` and `merchant` ID.
   - Gathers attribution (`utm_*`, `ad_id`, `fbp`, `fbc`, `ttclid`, `sccid`) and `user_agent`.
   - Attaches `_mt_*` key-values to Salla order notes (`salla.order.updateNote` or cart comment).
   - Posts directly to the Motahai capture endpoint `POST /storefront/capture` (or `/webhooks/salla/capture`).
   - Deduplicates within the session via `sessionStorage` (`mt_salla_captured_<order_id>`).
5. On the Motahai backend:
   - Client-side capture updates the order's attribution fields and encrypts `client_ip` and `user_agent` into `checkout_context` (D-006).
   - If Salla webhooks arrive with `notes`, `custom_fields`, or `source_details`, `parse_salla_order` also extracts and merges attribution.
   - When the order reaches `delivered` (COD) or `paid` (prepaid), Meta CAPI receives `fbp`, `fbc`, `client_user_agent`, `client_ip_address`, and `external_id`/customer hashes with full Event Match Quality parity.

## Privacy & Safety

- Customer email and phone are NEVER read from or transmitted by the storefront script.
- Client IP and User Agent are Fernet-encrypted at rest (D-006) and purged after send or TTL.
- Script is completely wrapped in try/catch to guarantee zero disruption to customer checkout.

## Testing

```bash
node storefront/salla/test_capture.mjs
```
