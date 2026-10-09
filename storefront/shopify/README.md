# Motahai attribution capture for Shopify (S1-3)

Joins every order to the ad that drove it, and gives the `DeliveredPurchase` Meta CAPI event better matching
(fbp / fbc here, plus IP and user agent from the order webhook).

## How it works

1. `assets/motahai-capture.js` runs on every storefront page. It reads `utm_source`, `utm_medium`,
   `utm_campaign`, `utm_content`, `fbclid`, `ttclid`, `ScCid` (or `sccid`) and `mt_ad` from the URL, and the
   `_fbp` / `_fbc` cookies.
2. Any visit that carries at least one click parameter is a "click". The LAST click is kept in
   `localStorage` (`mt_attr`) for 7 days. A visit without click parameters never overwrites it.
3. It writes hidden cart attributes with `POST /cart/update.js`:
   `_mt_utm_source, _mt_utm_medium, _mt_utm_campaign, _mt_utm_content, _mt_ad_id, _mt_fbp, _mt_fbc,
   _mt_ttclid, _mt_sccid, _mt_ts` (click time in ms). The underscore prefix makes them private: hidden at
   checkout, visible on the order. It only calls the cart API when the values changed (signature kept in
   `sessionStorage`, combined with the `cart` cookie so a new cart after a purchase is written again).
4. Shopify delivers them in the order webhook's `note_attributes`; the server
   (`webhook_listener.parse_shopify_order`) validates them and stores them on the order. `client_details`
   (`browser_ip`, `user_agent`) is read from the same webhook and used only for the CAPI payload, never stored.

`ad_id` is `mt_ad` when present, otherwise `utm_content` when it is all digits.

## Consent

- If `window.Shopify.customerPrivacy` exists, the script stores and sends nothing until
  `Shopify.customerPrivacy.marketingAllowed()` is true, and it re-runs on the `visitorConsentCollected` event.
  One switch covers every field (click ids and fbp/fbc are marketing identifiers; UTMs alone would pass
  `analyticsProcessingAllowed()`, but splitting would make behaviour harder to reason about and audit).
- If the Customer Privacy API is absent, the script proceeds. Consent is then the merchant's responsibility
  (their own banner must gate the script).
- If the consent API throws, the script does nothing.

## Install

### A. Theme app extension (target state)

`blocks/motahai-capture.liquid` is the app embed block (target `body`, loads the asset with `defer`).
Shipping it needs the Shopify app shell (TASK-011), which is NOT built yet: a theme app extension lives in
an app's `extensions/` folder (`assets/motahai-capture.js` + `blocks/motahai-capture.liquid`), is deployed with
the Shopify CLI, and the merchant switches the embed on under Theme editor > App embeds.

### B. Manual fallback (done-with-you pilots)

1. Theme > Edit code > Assets > add `motahai-capture.js` (paste the file).
2. In `layout/theme.liquid`, just before `</body>`, add:
   `<script src="{{ 'motahai-capture.js' | asset_url }}" defer></script>`
3. Per D-003 Hermes never edits the theme; a human does this and QA's it.

### Verify on a development store (the lookup doc marks this as inferred)

The Shopify docs say private cart attributes are visible on the order details page; they do not state
that they appear in the webhook's `note_attributes`. Before relying on it: add a product via an ad URL
(`?utm_source=meta&utm_content=1234567890`), place a test order, and check the `orders/create` payload for
`note_attributes` entries named `_mt_*`. If private attributes are missing there, drop the underscore prefix
(the server also accepts `mt_`-prefixed names) at the cost of showing them at checkout.

## Ad URL-parameter templates

Meta (Ads Manager > ad > Tracking > URL Parameters, no leading `?`):

    utm_source=meta&utm_medium=paid&utm_campaign={{campaign.id}}&utm_content={{ad.id}}

TikTok (macros confirmed in TikTok's UTM help article; the field where they are entered was not found):

    utm_source=tiktok&utm_medium=paid&utm_campaign=__CAMPAIGN_ID__&utm_content=__CID__&mt_ad=__CID__

`__CID__` is the creative ID and `__AID__` the ad group ID. Smart+ campaigns use `__ADID_V2__` for the ad.
TikTok appends `ttclid` itself.

Snapchat (Ads Manager > Ad details > Build URL parameters; macro names inferred from secondary sources):

    utm_source=snapchat&utm_medium=paid&utm_campaign={{campaign.id}}&utm_content={{ad.id}}

Snapchat appends `ScCid` itself.

Verify in Ads Manager: the Meta macros and ALL Snapchat macros are not confirmed from official pages (see
`docs/research/S1-6_platform_lookups.md`). A typo yields an empty value, and the click shows as unattributed.
Paste a template into one ad, click the preview link, and check that `utm_content` is a number.

## Tests

`node storefront/shopify/test_capture.mjs` (also run by `tests/test_attribution.py`, skipped when node is missing).
