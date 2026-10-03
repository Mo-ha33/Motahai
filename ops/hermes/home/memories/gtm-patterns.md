# Battle-Tested GTM Patterns & Gotchas

> Read on demand. Each pattern: **Rule → Why → How → Verify.** Items tagged
> `[VERIFY]` depend on platform behaviour that changes; re-confirm against the
> vendor's current docs (via `hermes_web_search`) before citing them in a verdict.
> Status as of 2026-10.

---

## P1. Shopify — Checkout Extensibility (Additional Scripts are gone)

- **Rule:** Never propose code for `checkout.liquid` or *Settings → Checkout → Additional
  scripts* (order status page). Both are retired for Plus and non-Plus stores
  (Shopify deadlines: Plus Aug 2025, non-Plus Aug 2026). `[VERIFY]` per store.
- **How:** Checkout/thank-you tracking runs in **Customer Events → Custom pixel**
  (Web Pixels API) or an app pixel. Storefront (theme) pages can still use GTM in
  `theme.liquid`.
- **Event map (Web Pixels → GA4):**
  | Shopify `analytics.subscribe` | GA4 |
  |---|---|
  | `page_viewed` | `page_view` |
  | `product_viewed` | `view_item` |
  | `collection_viewed` | `view_item_list` |
  | `search_submitted` | `search` |
  | `product_added_to_cart` | `add_to_cart` |
  | `cart_viewed` | `view_cart` |
  | `checkout_started` | `begin_checkout` |
  | `checkout_shipping_info_submitted` | `add_shipping_info` |
  | `payment_info_submitted` | `add_payment_info` |
  | `checkout_completed` | `purchase` |
- **Gotchas:**
  1. The custom pixel runs in a **sandboxed iframe**. GTM loaded inside it sees the
     sandbox URL → always set `page_location` from `event.context.document.location.href`
     and `page_referrer` from `event.context.document.referrer`.
  2. GTM Preview / Tag Assistant **cannot attach** to the sandbox. Debug with GA4
     DebugView (`debug_mode: true` on a test pixel) and `console.log` in the pixel.
  3. The pixel's `window.dataLayer` is **not** the storefront's dataLayer.
  4. Money in Web Pixels is `MoneyV2 { amount: number, currencyCode }` — already decimal.
     Liquid money objects (`{{ product.price }}`, `{{ line_item.final_price }}`) are in
     **subunits** (÷100). `| money` filters return *formatted strings* — never feed them to GA4.
  5. Customer-privacy: in *Customer events → pixel → Customer privacy*, set the permission
     (marketing / analytics) so Shopify withholds events until consent. Do not also gate
     with a second, conflicting consent check.
  6. **Double counting with Shopify sales-channel apps.** The *Google & YouTube* app can send
     GA4/Ads purchases; the *Facebook & Instagram* app sends Pixel + CAPI. GTM pixels on top
     ⇒ duplicates. Decide ONE owner per destination, record it in the client ledger.

### Reference custom pixel (purchase only — extend per event map)
```js
// Shopify Admin → Settings → Customer events → Add custom pixel
// Replace <<GTM-ID>> — never guess it.
const GTM_ID = "<<GTM-ID>>";
window.dataLayer = window.dataLayer || [];
function gtag(){ dataLayer.push(arguments); }
(function(w,d,s,l,i){w[l]=w[l]||[];w[l].push({'gtm.start':new Date().getTime(),event:'gtm.js'});
var f=d.getElementsByTagName(s)[0],j=d.createElement(s),dl=l!='dataLayer'?'&l='+l:'';
j.async=true;j.src='https://www.googletagmanager.com/gtm.js?id='+i+dl;f.parentNode.insertBefore(j,f);
})(window,document,'script','dataLayer',GTM_ID);

analytics.subscribe("checkout_completed", (event) => {
  const c = event.data.checkout;
  // Normalise: GID ("gid://shopify/Order/123") → "123", so the webhook path (numeric id)
  // produces the identical event_id / transaction_id.
  const orderId = String(c.order?.id ?? "").split("/").pop();
  if (!orderId) return;                                   // no business key → no purchase
  dataLayer.push({ ecommerce: null });
  dataLayer.push({
    event: "purchase",
    event_id: "purchase_" + orderId,                      // deterministic → server can reproduce
    page_location: event.context.document.location.href,
    ecommerce: {
      transaction_id: orderId,
      currency: c.totalPrice.currencyCode,               // e.g. "EGP"
      value: Number(c.totalPrice.amount),
      tax: Number(c.totalTax?.amount ?? 0),
      shipping: Number(c.shippingLine?.price?.amount ?? 0),
      coupon: (c.discountApplications || []).map(d => d.title).filter(Boolean).join(",") || undefined,
      items: (c.lineItems || []).map((li, idx) => ({
        item_id: String(li.variant?.sku || li.variant?.product?.id || ""),
        item_name: li.title,
        item_variant: li.variant?.title,
        price: Number(li.variant?.price?.amount ?? 0),
        quantity: li.quantity,
        index: idx
      }))
    }
  });
});
```
`[VERIFY]` field paths against the current Web Pixels `Checkout` object before shipping.

---

## P2. Dual-layer deduplication

- **Layer 1 (no double fire):** `transaction_id` on GA4 + Ads; thank-you reload guard:
  ```js
  var k = "tx_" + orderId;
  try { if (localStorage.getItem(k)) return; localStorage.setItem(k, "1"); } catch (e) {}
  ```
  One purchase tag per destination; linter rules `tag.exact_duplicate`,
  `google.duplicate_google_tag` must be zero.
- **Layer 2 (browser ↔ server):** same `event_name` + same `event_id` on both paths.
  - Purchase: `event_id = "purchase_" + order_id` — **deterministic**, so a server path
    driven by Shopify webhooks (which never sees the browser) produces the identical ID.
    Use the **numeric** order ID on both paths (strip any `gid://shopify/...` prefix).
  - Other events: UUID generated once in the dataLayer push, read by both the Pixel tag
    (`eventID`) and the sGTM/CAPI tag (`event_id`).
  - Meta dedups within **48 h**; the browser and server event names must match exactly
    (`Purchase` vs `purchase` do not dedup).
- **Verify:** Meta Events Manager → event → "Deduplicated" counts; GA4 purchases vs
  Shopify orders for the same day (expect ≤ 2–3% gap, explainable by consent/adblock).

---

## P3. Meta Conversions API

- `event_time` = Unix **seconds**, not older than 7 days. `action_source: "website"`
  requires `event_source_url` and `client_user_agent`.
- **Hash** (SHA-256, lowercase hex) after normalising: `em` (trim, lowercase), `ph`
  (digits only, with country code, **no `+`, no leading 0**: Egypt `010 1234 5678` →
  `201012345678`), `fn`/`ln` (lowercase, no punctuation), `ct`, `st`, `zp`, `country`
  (`eg`), `external_id`.
- **Do not hash** `fbp`, `fbc`, `client_ip_address`, `client_user_agent`.
- `fbc` = `fb.1.<creation_ms>.<fbclid>`; build it server-side from the landing-page
  `fbclid` if the `_fbc` cookie is missing.
- Token lives only on the server (sGTM / Stape / backend). A token in the browser is a
  `critical` incident (linter: `security.meta_token_in_html`).
- Use Test Events code only on a non-production dataset or remove it after testing —
  a forgotten `test_event_code` silently diverts production events.

## P4. Google Ads Enhanced Conversions

- Normalise: email trimmed + lowercase (for `gmail.com`/`googlemail.com` remove dots in
  the local part); phone in **E.164 with `+`** (`+201012345678`) — note the contrast with
  Meta's no-`+` rule. Hash SHA-256 or let the Google tag hash `user_provided_data`.
- Turn on "Include user-provided data" in the Google tag / Ads settings; verify the
  conversion's diagnostics show EC "Recording" `[VERIFY]` naming in the current UI.
- Purchase conversion: `transaction_id` = Shopify order ID (dedup + enables adjustments).

---

## P5. GA4 ecommerce schema (Egypt/Gulf specifics)

- `currency`: `EGP` (2 decimals), `SAR`, `AED`, `KWD` (**3 decimals**), `USD`. Always
  ISO uppercase, never `LE`, `ج.م`, `EGY`.
- Parse prices defensively when scraping the DOM:
  Arabic-Indic digits `٠١٢٣٤٥٦٧٨٩` → `0-9`; Arabic decimal `٫` (U+066B) → `.`;
  strip thousands separators `,` and `٬` (U+066C) and all currency text.
- `value` = what you optimise on; define per client (incl./excl. shipping & tax) in the
  ledger and keep it identical across GA4, Ads, Meta.
- `items` ≤ 200 per event; each needs `item_id` or `item_name`.
- `ecommerce: null` push before every ecommerce event (stale items otherwise merge).

## P6. Egyptian payment gateways & COD

- **Paymob / Tabby / valU / card 3-DS redirects:** user leaves the domain, comes back →
  GA4 self-referral steals attribution. Add the gateway domains seen in the client's
  referral report to GA4 *Unwanted referrals* (`[VERIFY]` exact hostnames from data,
  e.g. `accept.paymob.com`, `checkout.tabby.ai`).
- **Fawry reference-code & COD:** order is created **before** money is collected.
  - **Option A (Recommended when unpaid/RTO rate > ~15%):** browser fires
    `order_placed` (custom) at thank-you; the real `purchase` is sent **server-side** on
    the Shopify `orders/paid` webhook (GA4 Measurement Protocol / sGTM, Meta CAPI,
    Ads offline/EC for leads) with `event_id = purchase_<order_id>`.
  - **Option B:** fire `purchase` at thank-you for everything; later send GA4 `refund`
    and Google Ads conversion **retractions/restatements** for unpaid/RTO orders.
    Simpler, but ad platforms optimise on orders that never pay.
- Record per-client gateway behaviour in the ledger — it drives the purchase timing decision.

## P7. Consent Mode v2

- Default (before any tag): all four signals — `ad_storage`, `analytics_storage`,
  `ad_user_data`, `ad_personalization` — plus `wait_for_update` (≈500 ms) when the CMP
  loads async. Region-scoped defaults are allowed (`region: ['EG']` granted vs EEA denied)
  but must be a documented client decision.
- CMP template fires on **Consent Initialization – All Pages**; Google tag on
  **Initialization – All Pages**; nothing else on Consent Init.
- Advanced mode (tags load, send cookieless pings) vs Basic (tags blocked until consent):
  a client decision — record it.
- Non-Google tags need *Additional consent checks* (linter:
  `consent.non_google_tag_unchecked`).
- Verify with Tag Assistant → Consent tab: default state present before first tag;
  update event after CMP choice; `gcs`/`gcd` params change on GA4 hits.
- EEA/UK: TCF v2.2 + Google-certified CMP. Egypt: PDPL (Law 151/2020) — flag legal
  questions to the human; Hermes certifies technical behaviour only.

## P8. Server-side GTM (Stape / Cloud Run)

- Host on a first-party subdomain (`sst.client.com`) or same-origin path.
  Safari/ITP may cap cookies set by a server whose IP does not match the main site
  (`[VERIFY]` current WebKit rules); Stape's "Own CDN"/"Cookie Keeper"-type features or a
  same-origin path mitigate. On Shopify, a same-origin path needs a proxy in front of
  Shopify — check Shopify's current support stance first; default to subdomain.
- One **client** claims each request (GA4 client for `/g/collect`, Data client for custom).
  Check `Request → Client` in sGTM preview when events vanish.
- Keep the web GA4 tag's `server_container_url` and transport consistent; don't send the
  same hit both direct-to-Google and via sGTM.
- Forward `event_id`, `user_data`, `fbp/fbc`, IP and UA to the server; strip PII not needed.

## P9. GTM authoring gotchas

- Built-in trigger IDs: All Pages `2147479553`; Initialization & Consent Initialization use
  the reserved `21474795xx` range — not present in the export's `trigger` list.
- **Custom JavaScript variables must be pure** — GTM evaluates them many times per event.
  No `dataLayer.push`, no network calls, no DOM mutation inside them.
- Lookup tables: exact, case-sensitive. Regex tables: first match wins; "full matches only"
  is on by default; capture groups `$1` need "Enable capture groups".
- `{{Event}}` / `{{_event}}` inside custom-event triggers is internal — not a missing var.
- "Once per page" on a tag ≠ dedup across reloads — use the Layer-1 guard (P2).
- UA (`ua` tags) stopped processing July 2024 — delete, don't migrate in place.
- Prefer templates (Community Gallery) over Custom HTML: sandboxed, permissioned, reviewable.
- Web container hard limit ~200 KB; linter warns above 150 KB export size.
- Publishing: every publish gets a **version name + description** citing the verdict ID;
  rollback = re-publish previous version number recorded in the ledger.
