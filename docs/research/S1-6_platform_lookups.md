# S1-6 Platform Lookups: URL macros, Meta CAPI, Shopify, Salla, WhatsApp

Research date: 2026-10-09. Sources: official platform documentation where it could be fetched; otherwise search excerpts or third-party guides, labelled as such.

## Confidence key

- **Confirmed**: stated in an official doc that was fetched (or an official search excerpt, noted where used).
- **Inferred**: not stated directly; deduced from examples, code samples, or secondary sources.
- **Not found**: no official source located. Do not rely on an answer here without a test.

## Access caveats

- Several official pages are JavaScript-rendered and returned only a title through WebFetch: the Meta URL-parameters article (facebook.com/business/help/2360940870872492), the Meta learning-phase article (facebook.com/business/help/112167992830700), the Snapchat URL-macro articles (businesshelp.snapchat.com, "Sorry to interrupt / CSS Error"), and the WhatsApp template and read-receipt articles (developers.facebook.com/documentation/...). Answers that depend on these are marked Inferred.
- The Shopify webhook topic reference page is very long and the orders/paid topic text was not reachable there. The WebhookSubscriptionTopic enum page was used instead.

## Summary table

| # | Question | Answer | Confidence | Source |
|---|---|---|---|---|
| 1a | TikTok ad-level macros | `__CAMPAIGN_ID__`, `__CAMPAIGN_NAME__`, `__AID__` (ad group ID), `__AID_NAME__`, `__CID__` (creative ID), `__CID_NAME__`, `__PLACEMENT__`. Smart+ upgraded campaigns: `__ADID_V2__`, `__ADID_V2_NAME__`. The page does not name the UI field where they are set. | Confirmed (names); setting field not found | https://ads.tiktok.com/help/article/track-offsite-web-events-with-utm-parameters (page says "Last updated: February 2026") |
| 1b | Snapchat ad-level macros | Secondary sources list campaign, ad set and ad IDs as `{{campaign.id}}`, `{{adSet.id}}`, `{{ad.id}}`. No ad-squad-specific macro found. Set in Ads Manager, Ad Details, Build URL parameters, with default keys Campaign Source / Campaign ID / Campaign Content. Catalog links take the macros in the link attribute. | Inferred (official page not readable) | https://businesshelp.snapchat.com/s/article/add-url-macros?language=en_US (not readable); search excerpt of the same article |
| 1c | Meta URL macros | `{{ad.id}}`, `{{adset.id}}`, `{{campaign.id}}`; also `{{ad.name}}`, `{{adset.name}}`, `{{campaign.name}}`, `{{placement}}`, `{{site_source_name}}`. Entered in the "URL Parameters" field in the Tracking section, without a leading "?". | Inferred (names consistent across secondary sources; official body not readable) | https://www.facebook.com/business/help/2360940870872492 (title "Specifications For URL Dynamic Parameters" confirmed) |
| 2a | Meta: share a dataset/pixel with a partner | Business Settings / Events Manager: share the data source with a partner business ("Assign partners" by partner business ID). Permission tasks for a pixel: ANALYZE, UPLOAD, ADVERTISE, EDIT (EDIT is restricted, needs Meta allowlisting). Recipient admin accepts the request. The "Manage events dataset" and "Use events dataset" labels from vendor guides were not confirmed. | Confirmed (tasks and API); UI labels Inferred | https://developers.facebook.com/docs/business-management-apis/business-asset-management/guides/business-pixel-sharing/ |
| 2b | Meta: partner token for CAPI | Partner assigns its system user to the shared pixel and generates an access token from it. Token type and required permission are not stated on the CAPI page. | Inferred (from a summary of Meta partner docs) | https://developers.facebook.com/docs/business-management-apis/business-asset-management/guides/business-pixel-sharing/ ; https://developers.facebook.com/documentation/ads-commerce/conversions-api/using-the-api |
| 2c | CAPI auth: Bearer header or access_token param | The documented method is the `access_token` parameter (query string per the text; form field in the cURL example). No Authorization header is documented for `/{id}/events`. Bearer is documented for WhatsApp and general Graph calls, but the Graph auth page returned 404. Do not assume Bearer works for `/events`. | Confirmed (access_token param); Bearer not documented for CAPI | https://developers.facebook.com/documentation/ads-commerce/conversions-api/using-the-api |
| 2d | Browser/server dedup window | 48 hours. Matching is on `event_id` = `eventID` and `event_name` = `event`. Browser-first events can also match on `event_name` plus `fbp` and/or `external_id`. | Confirmed | https://developers.facebook.com/documentation/ads-commerce/conversions-api/deduplicate-pixel-and-server-events |
| 2e | Max event_time age (website) | `event_time` can be up to 7 days in the past. If any event in a request is older than 7 days, the whole request fails. | Confirmed | https://developers.facebook.com/documentation/ads-commerce/conversions-api/parameters/server-event ; https://developers.facebook.com/documentation/ads-commerce/conversions-api/using-the-api |
| 2f | Optimize on a custom conversion built from a server event (e.g. "DeliveredPurchase") | Official custom-conversion docs define conversions by a rule on pixel `url`/`event` (`event_source_id`), with a cap of 100 per ad account. No official text says a CAPI-sent custom event name can drive optimization on web. The offline-conversions doc says custom events cannot be used for optimization (offline context). Minimum volume: about 50 optimization events in 7 days, from third-party sources only. | Not found (server custom event optimization); volume threshold Inferred | https://developers.facebook.com/docs/marketing-api/reference/custom-conversion/ ; https://www.facebook.com/business/help/112167992830700 (not readable) |
| 3a | Shopify COD: when does `financial_status` become `paid` | Manual payment orders (COD included) stay Pending/unpaid until the merchant marks them paid on the order details page. The API route is the `orderMarkAsPaid` GraphQL mutation (needs `write_orders` and `mark_orders_as_paid`). The `orders/paid` topic "Occurs whenever an order is paid." Community reports say some COD orders appear PAID at creation. This is unresolved. | Confirmed (merchant marks paid); community conflict noted | https://help.shopify.com/en/manual/payments/manual-payments ; https://shopify.dev/docs/api/admin-graphql/latest/mutations/orderMarkAsPaid ; https://shopify.dev/docs/api/admin-graphql/latest/enums/WebhookSubscriptionTopic |
| 3b | Shopify delivery status | Topics: `fulfillments/update` ("Occurs whenever a fulfillment is updated") and `fulfillment_events/create` ("Occurs whenever a fulfillment event is created"). `shipment_status` values: `label_printed`, `label_purchased`, `attempted_delivery`, `ready_for_pickup`, `confirmed`, `in_transit`, `out_for_delivery`, `delivered`, `failure`. "fulfilled" is not a shipment status and does not mean delivered. | Confirmed (values and topics); whether `shipment_status` appears in webhook payloads not verified | https://shopify.dev/docs/api/admin-rest/latest/resources/fulfillment ; https://shopify.dev/docs/api/admin-graphql/latest/enums/WebhookSubscriptionTopic |
| 3c | Shopify `client_details` and `note_attributes` in order payload (API 2026-10) | `client_details` is present with `browser_ip`, `user_agent`, `accept_language`, `session_hash`, `browser_height`, `browser_width`. `note_attributes` is an array of `{name, value}` objects in the sample. The field description is not in the visible text. | Confirmed (`client_details`); `note_attributes` shape confirmed from sample only | https://shopify.dev/docs/api/admin-rest/latest/resources/order (api_version 2026-10) |
| 3d | Shopify webhook HMAC | Header `X-Shopify-Hmac-Sha256` (docs spelling `X-Shopify-Hmac-SHA256`; HTTP headers are case-insensitive). Value is base64 HMAC-SHA256 of the raw request body, keyed with the app's client secret. Parse the raw body before any JSON middleware. | Confirmed | https://shopify.dev/docs/apps/build/webhooks/subscribe/https |
| 3e | Theme app embed writes cart attributes that appear as order `note_attributes` | `POST /cart/update.js` takes an `attributes` object, so a theme embed can set cart attributes. Shopify states that "private" cart attributes are hidden at checkout and visible on the Order details page, and are not exposed to Liquid `cart.attributes` or the Ajax API. The page does not state that normal cart attributes become order `note_attributes`. The theme app extension page does not mention cart attributes. | Cart write: Confirmed; carry-over to `note_attributes`: Inferred (not stated in the fetched text) | https://shopify.dev/docs/api/ajax/reference/cart ; https://shopify.dev/docs/apps/build/online-store/theme-app-extensions/configuration (not found) |
| 4a | Salla webhook signature | Headers: `X-Salla-Security-Strategy` and `X-Salla-Signature` (the page also writes `x-salla-signature`). The default strategy is `signature`. Code samples compute HMAC-SHA256 (hex) with the webhook secret as key over the raw body and compare. Prose says plain "SHA256" and gives conflicting lengths (64 vs 32 characters), so the code is the best guide. The Node sample lacks a `return` after `sendStatus(401)`. | Inferred (from code samples; docs ambiguous) | https://docs.salla.dev/421119m0 |
| 4b | Salla order status webhook and slugs | Event `order.status.updated` exists in the order event list. Status slugs: `payment_pending`, `waiting_for_payment_confirmation`, `payment_failed`, `in_progress`, `under_review`, `completed` ("paid and delivered to the customer"), `delivering`, `delivered`, `shipped`, `canceled`, `restored`, `restoring`. Other order events include `order.payment.updated` and `order.shipment.created`. The payload shape for `order.status.updated` was not verified. | Confirmed (event name and slugs); payload shape not verified | https://docs.salla.dev/421119m0 ; https://docs.salla.dev/5394150e0 |
| 4c | Salla COD representation | `payment_method` value `"cod"` appears in sample order and shipment payloads. Other values seen: `bank`, `credit_card`, `customer_wallet`, `pre_paid` (shipments). No full enum found. | Inferred (sample data only) | https://docs.salla.dev/433804m0 ; https://docs.salla.dev/5751605e0 |
| 4d | Salla app snippet / attaching click IDs or UTMs to an order | Confirmed: an App Snippet injects `tracker.js` from a CDN URL set in the Partners Portal (app, then Snippet). Writing custom click/UTM data onto an order: not found. The order object exposes `source_details` (`type`, `value`, `device`, `user-agent`, `ip`). A Create Order sample shows `utm_*` keys under `source_details`, but that came from a search excerpt. Whether apps can write order fields is not stated. | Snippet injection: Confirmed; order attachment: Not found | https://docs.salla.dev/1724504m0 ; https://docs.salla.dev/433804m0 |
| 5a | WhatsApp: business-initiated messages outside the 24-hour window | Outside an open customer service window only pre-approved template messages can be sent. Templates must be `APPROVED` before sending. Categories: Marketing, Utility, Authentication. Utility templates must be tied to the user's order, account or transaction, or be essential (for example, safety), and cannot be promotional. Authentication templates have strict rules and cannot be used for marketing-style content. Review is automatic on create or edit, and WhatsApp Manager says it can take up to 24 hours. Up to 100 templates per WABA per hour. Utility templates inside an open window are free. | Confirmed | https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/overview ; https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-messages ; https://developers.facebook.com/docs/whatsapp/pricing ; https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/template-categorization (search excerpt, not fetched in full) |
| 5b | WhatsApp read receipts via `statuses` webhook | Official webhook docs say each outgoing message can trigger up to three status webhooks: sent, delivered, read. `failed` appears in the `statuses` errors. `delivered` means the message reached at least one device. Whether a `read` webhook is suppressed when the recipient disables read receipts is not stated in the Cloud API docs. The consumer FAQ says users can turn read receipts off, and that they then cannot see others' receipts (group chats are the exception). | `read` webhook: Confirmed; suppression when disabled: Not found (consumer FAQ says receipts can be disabled) | https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/components ; https://faq.whatsapp.com/3307102709559968/?locale=en_US&cms_platform=web&category=5245250 (search excerpt, not fetched) |

## Section 1: URL macros for ad-level tracking

### 1a. TikTok Ads

- Official article: "Track offsite web events with UTM parameters" (last updated February 2026, no version number). https://ads.tiktok.com/help/article/track-offsite-web-events-with-utm-parameters
- Macros listed in the official table:
  - `__CAMPAIGN_NAME__` (campaign name), `__CAMPAIGN_ID__` (campaign ID)
  - `__AID_NAME__` (ad group name), `__AID__` (ad group ID)
  - `__CID_NAME__` (creative name), `__CID__` (creative ID)
  - `__PLACEMENT__` (TikTok, TikTok Pangle)
  - Smart+ (upgraded) campaigns: `__ADID_V2__` (ad ID), `__ADID_V2_NAME__` (ad name, the bundle of creative assets)
- Where set: the article points to the TikTok URL builder tool and to an option that adds UTM parameters to the ad URL automatically. It does not name the ad-level field. Setting location: Not found.
- Reading: the `__AID__` and `__CID__` names in the task brief are correct, with the caveat that for Smart+ the ad-level macro is `__ADID_V2__`.

### 1b. Snapchat Ads

- Official article IDs: "Add URL Macros to Your Ads" (https://businesshelp.snapchat.com/s/article/add-url-macros?language=en_US), "About URL Parameters and Macros" (https://businesshelp.snapchat.com/s/article/url-parameters?language=en_US), and "Appending URL Parameters in the Snapchat Catalog". These could not be fetched.
- From search excerpts of the official material (not verified in full):
  - Catalog article lists five macros: campaign name, ad set name, campaign ID, ad set ID, ad ID, written in double curly braces, for example `{{campaign.id}}`, `{{adSet.id}}`, `{{ad.id}}`. These go in each product's link attribute.
  - Builder default parameter keys: Campaign Source, Campaign ID, Campaign Content. Dynamic macros must be typed inside curly braces. Special characters are encoded.
  - Auto-UTMs (dated October 1, 2025) can add Ad ID and Campaign ID to ads when they are missing.
- Ad squad macro: Not found. Snapchat's API calls the object an ad squad, while the help centre says "ad set". Confirm which name the builder uses.
- Server-side tag macros, for example `SERVER_AD_SQUAD_ID`, `SERVER_CAMPAIGN_ID`, `SERVER_AD_ID`, come from a search excerpt of the Third Party Tracking FAQ. They are separate from the curly-brace URL macros.
- Story ads cannot take URL-level macros. Existing ones must be edited in the creative library.

### 1c. Meta

- Official page: "Specifications For URL Dynamic Parameters", Meta Business Help Center, article 2360940870872492. Title confirmed; body not readable. https://www.facebook.com/business/help/2360940870872492
- Macros (from secondary sources, consistent with each other): `{{campaign.id}}`, `{{adset.id}}`, `{{ad.id}}`, `{{campaign.name}}`, `{{adset.name}}`, `{{ad.name}}`, `{{placement}}`, `{{site_source_name}}` (values such as fb, ig, msg, an).
- Setting location: the "URL Parameters" field in the Tracking section during ad setup. Secondary sources disagree on whether the field is at campaign, ad set, or ad level. Confirm in Ads Manager.
- Format: enter the string without a leading "?"; separate pairs with "&". Macros are filled at click time. A typo produces an empty value and the click may fall under "Unknown".

## Section 2: Meta Conversions API and dataset sharing

### 2a. Sharing a pixel or dataset with a partner

- Official permission tasks for pixel sharing (Business Management API, `POST <ads_pixel>/agencies?business=<agency_business_id>&permitted_tasks=[...]`):
  - ANALYZE: "View, analyze and advertise."
  - UPLOAD: "Upload website conversion data to this dataset."
  - ADVERTISE: "Connect ad accounts to this Facebook dataset."
  - EDIT: "Manage dataset, edit settings, analyze and advertise." Restricted; requires Meta allowlisting.
- The request uses a system user token that owns the pixel. If there is no existing relationship, the request is pending, and the recipient business admin accepts it in Business Manager (or via API with the recipient admin's user token).
- The official page does not explicitly say that UPLOAD covers CAPI server events. Inferred: UPLOAD is the task that allows sending conversion data, so a partner needs UPLOAD (or EDIT) to send server events. Verify with a test.
- Events Manager UI: vendor guides describe Data Sources, then Settings, then Share with a business, then Partners, then Assign partners (enter partner business ID). The exact "Manage events dataset" label was not confirmed in an official source.

### 2b. Partner access token

- Pixel sharing docs: the partner assigns its system user to the shared pixel and generates an access token from it to send server events. Inferred from a summary of the official partner docs.
- Not found: a Meta statement that the token must be a system-user token for `/events`.

### 2c. Authentication for `/{id}/events`

- Official CAPI usage: "Attach your generated secure access token using the `access_token` query parameter to the request." The cURL example uses a form field instead. Endpoint: `POST https://graph.facebook.com/{API_VERSION}/{PIXEL_ID}/events`. Example version v25.0. The page calls the dataset ID `PIXEL_ID`.
- Authorization header: not documented for CAPI. The Bearer examples found in search are from WhatsApp docs; Graph API auth page returned 404. Recommendation: send `access_token` as a parameter, and test any Bearer approach before using it.

### 2d. Dedup window

- 48 hours. Rule from the official page: "events are only deduplicated if they are received within 48 hours of when we receive the first event." Match on `event_id` (browser `eventID`) and `event_name` (browser `event`). Alternative for browser-first flows: `event_name` plus `fbp` and/or `external_id`.

### 2e. Maximum event age (website events)

- "The `event_time` can be up to 7 days before you send an event to Meta." If any event in a request is older than 7 days, the whole request fails. For `physical_store` offline events the limit is 62 days.
- The page does not state a future-timestamp rule.

### 2f. Optimizing on a custom conversion built from a server event

- Official custom-conversion reference: custom conversions filter events by `rule` (fields `url`, `event`, and custom parameters), are tied to `event_source_id` (pixel or offline event set), and are set with `custom_event_type`. Maximum 100 per ad account. The examples use browser events (`fbq('trackCustom', ...)`); the page does not discuss CAPI-sent events.
- Offline conversions doc (official): "You can't use custom events for optimization or Custom Audiences." This is stated for offline uploads, so it is not proof for web CAPI events.
- Not found: an official statement that a CAPI server custom event (for example "DeliveredPurchase") can be used as an optimization event. Test it in a dev account before committing to the plan.
- Minimum volume: Third-party sources cite about 50 optimization events within 7 days to leave the learning phase. The official learning-phase article could not be read. Inferred.

## Section 3: Shopify

### 3a. COD: when `financial_status` becomes paid

- Official help: orders placed with manual payment methods (COD included) show as pending/unpaid. The merchant "mark[s] the order as paid on the order details page" after receiving payment, then fulfills as usual. https://help.shopify.com/en/manual/payments/manual-payments (page had no date)
- API: `orderMarkAsPaid` (GraphQL Admin, API 2026-10) "Marks an order as paid by recording a payment transaction for the outstanding amount." Scopes: `write_orders` plus the `mark_orders_as_paid` user permission. Only valid when the outstanding balance is positive and the order is not already PAID. https://shopify.dev/docs/api/admin-graphql/latest/mutations/orderMarkAsPaid
- Webhook: ORDERS_PAID, which is the `orders/paid` topic: "The webhook topic for `orders/paid` events. Occurs whenever an order is paid." https://shopify.dev/docs/api/admin-graphql/latest/enums/WebhookSubscriptionTopic (API 2026-10)
- Conflict: community threads report that some manually created COD orders showed PAID without fulfillment, and that `orders/updated` did not always fire on the Pending to Paid change. These are unresolved and not official. Test on a development store with a COD order marked paid, and verify `financial_status` in the payload.
- `financial_status` enum (API 2026-10): pending, authorized, partially_paid, paid, partially_refunded, refunded, voided.

### 3b. Delivery status

- Topics (official enum descriptions): `fulfillments/update` ("Occurs whenever a fulfillment is updated"), `fulfillments/create`, and `fulfillment_events/create` ("Occurs whenever a fulfillment event is created"). Each requires `read_fulfillments` (or `read_marketplace_orders`).
- `shipment_status` values on the Fulfillment resource (API 2026-10): `label_printed`, `label_purchased`, `attempted_delivery`, `ready_for_pickup`, `confirmed`, `in_transit`, `out_for_delivery`, `delivered`, `failure`. https://shopify.dev/docs/api/admin-rest/latest/resources/fulfillment
- Fulfillment `status` (not delivery): `pending`, `open`, `success`, `cancelled`, `error`, `failure`.
- Fulfilled is not delivered. Order-level `fulfillment_status` values `fulfilled`, `partial`, `null`, `restocked` describe whether line items were fulfilled. Delivery is `shipment_status` = `delivered`. Treat `fulfilled` as "handed to carrier or shop", not delivered.
- Not verified: whether `shipment_status` appears in the `fulfillments/update` payload, and the exact `FulfillmentEvent` status enum (its REST page returned 404; the same values appear in the fulfillment payload sample).

### 3c. `client_details` and `note_attributes`

- Order resource (API 2026-10) `client_details`, read-only: `accept_language`, `browser_height`, `browser_ip` (also top-level), `browser_width`, `session_hash`, `user_agent`. Still present in the current version. https://shopify.dev/docs/api/admin-rest/latest/resources/order
- `note_attributes`: present as an array of `{name, value}` objects in the sample payload. The field description did not appear in the visible text.

### 3d. Webhook HMAC

- Header: `X-Shopify-Hmac-Sha256` (docs spelling `X-Shopify-Hmac-SHA256`). Algorithm: HMAC-SHA256, base64 encoded. Key: app client secret. Message: raw request body. https://shopify.dev/docs/apps/build/webhooks/subscribe/https
- Parse the raw body before JSON middleware, as the page warns. After secret rotation it can take up to an hour for the new digest.

### 3e. Theme app embed writing cart attributes

- `POST /cart/update.js` with an `attributes` object (or form fields `attributes[Name]`) sets cart attributes. https://shopify.dev/docs/api/ajax/reference/cart
- Official text: "private cart attributes are visually hidden at checkout, but are visible on the Order details page." Private attributes are not in Liquid `cart.attributes` or the Ajax API. This means a storefront script cannot read back private attributes.
- The official page does not state that normal cart attributes are copied to order `note_attributes`. Inferred: they are, and the order's `note_attributes` carries them. Verify on a development store before relying on it.
- Theme app extension configuration page (https://shopify.dev/docs/apps/build/online-store/theme-app-extensions/configuration): no mention of cart attributes. Not found there.

## Section 4: Salla

### 4a. Webhook signature

- Headers: `X-Salla-Security-Strategy` (strategy used) and `X-Salla-Signature` (the page also writes `x-salla-signature`). The default strategy is `signature`. Other strategies: `token` (sends a value in `Authorization`, though the page's table has a typo) and `none`. https://docs.salla.dev/421119m0
- Algorithm: the code samples (Node and PHP) compute HMAC-SHA256 with the webhook secret as the key over the raw body, hex output, and compare the result to the header. The prose says "SHA256" and gives 64-character and 32-character lengths, which conflict. Inferred: HMAC-SHA256 hex. Test against a live webhook.
- Caution: the Node sample sends `sendStatus(401)` without `return`, so processing can continue into `sendStatus(200)`. Do not copy it as-is.

### 4b. Order status event and slugs

- Event name: `order.status.updated`. Other order events: `order.created`, `order.updated`, `order.cancelled`, `order.refunded`, `order.deleted`, `order.products.updated`, `order.payment.updated`, `order.coupon.updated`, `order.total.price.updated`, `order.shipment.creating`, `order.shipment.created`, `order.shipment.cancelled`, `order.shipment.return.*`, `order.shipping.address.updated`. https://docs.salla.dev/421119m0
- A search excerpt suggested `order.status.updated` is deprecated in one changelog entry, but the webhook page lists it as current and no order event on that page is marked deprecated. Confirm in the Partners Portal.
- Status slugs (List Order Statuses): `payment_pending`, `waiting_for_payment_confirmation`, `payment_failed`, `in_progress`, `under_review`, `completed`, `delivering`, `delivered`, `shipped`, `canceled`, `restored`, `restoring`. https://docs.salla.dev/5394150e0
- Important: Salla's `completed` is described as "paid and delivered to the customer". `delivered` is a separate slug. Shipment events (`order.shipment.created`) exist too.
- Payload shape of `order.status.updated`: not verified.

### 4c. COD representation

- `payment_method` value `"cod"` appears on order and shipment samples, and `payment_methods` arrays carry entries with `payment_method`. Other values seen: `bank`, `credit_card`, `customer_wallet`, `pre_paid` (shipment request). A cash-on-delivery amount appears under `amounts.cash_on_delivery` and a `cash_on_delivery` object. No complete enum found. https://docs.salla.dev/433804m0 ; https://docs.salla.dev/5751605e0
- Inferred: COD orders have `payment_method = "cod"`. Confirm against a real order.

### 4d. Snippets and attaching custom data to orders

- Confirmed: App Snippet injects a hosted `tracker.js`. Steps: Partners Portal, your app, Snippet, add the CDN URL of `tracker.js`. The snippet injects the tracker into merchant stores. Device Mode tracker events include cart updated and product added, plus a checkout-started listener. https://docs.salla.dev/1724504m0
- Not found: any way for an app or snippet to write click IDs, UTMs or custom fields to an order. The order object has `source_details` (`type`, `value`, `device`, `user-agent`, `ip`) and `tags` (writability not stated). A Create Order sample shows `utm_*` under `source_details` (search excerpt; the fetched order page did not show UTM fields). https://docs.salla.dev/433804m0
- Practical path for now: capture click IDs in the snippet and store them through the cart or your own backend, then match on the order webhook. Not documented by Salla; treat as a design choice to verify.
- The Device Mode page did not mention Cloud Mode, though a search excerpt did. Inconsistent; check the Partners Portal.

## Section 5: WhatsApp Cloud API

### 5a. Business-initiated messages outside the 24-hour window

- A user message opens a 24-hour customer service window. Outside it, only pre-approved template messages may be sent. Official: "Template messages are the only type of message that can be sent to WhatsApp users outside of a customer service window." Templates must have status `APPROVED` before sending. https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/overview
- Categories (official, from the template-categorization page via search excerpt): Marketing (awareness, sales, retargeting); Utility (follow-up on user actions or requests); Authentication (identity verification, with a one-time password button, no URLs, media or emojis, from the Template Library).
- Utility rule: must be non-promotional and either specific to the user's order, account or transaction, or essential to the user (for example, safety). Generic feedback surveys are rejected.
- Review: automatic on create or edit. WhatsApp Manager says review can take up to 24 hours. Up to 100 templates per WABA per hour. A template can be recategorized (utility to marketing) after approval; Meta gives advance notice except for misuse cases. Misuse can cap or recategorize utility templates.
- Pricing (official pricing page): utility templates delivered inside an open customer service window are free. Service conversations are free. Rate card cited as effective July 1, 2026; check the current rate card before costing.
- Template default TTL is 30 days, except authentication (10 minutes).
- Service messages (non-template) do not need pre-approval but are only sendable inside the open window (the send-messages page says service messages "do not require pre-approval").

### 5b. Read receipts via `statuses` webhook

- Official webhook components page: each outgoing message can trigger up to three status webhooks: "one for a status of sent, one for delivered, and one for read." The `statuses` object includes `id`, `status`, `timestamp`, `recipient_id`, `conversation` and `pricing`. https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/components
- `failed`: outgoing errors appear in `statuses[].errors`. The page does not list `failed` as a status value.
- `delivered` means the message reached at least one of the user's devices (official, from search excerpt of the messages/mark-as-read doc).
- Read receipts and the user's privacy setting: the consumer FAQ says users can turn off read receipts, and then they cannot see others' read receipts (group chats are the exception). The Cloud API docs do not say whether a `read` webhook is suppressed when the recipient has receipts off. Not found. Treat `delivered` as the primary success signal and `read` as optional. Verify with a test account.
- Mark-as-read: incoming messages should be marked read within 30 days (official, from search excerpt). Marking one message also marks earlier messages in the conversation.
- Duplicates: webhooks can be retried for up to 7 days, so deduplicate on message ID and status.

## Points that contradict or qualify the working assumptions

1. Shopify "fulfilled" is not "delivered": confirmed. The delivered signal is `shipment_status = delivered` (fulfillments and fulfillment events), not order-level `fulfilled`.
2. COD orders become paid when the merchant marks payment: confirmed by Shopify's official help and `orderMarkAsPaid`. But community reports say COD orders can show PAID at creation. Test before relying on it.
3. Salla uses `order.status.updated`: the event exists and is listed. Two qualifications: (a) Salla's `completed` slug already means paid and delivered, and `delivered` is a separate slug, so map both; (b) shipment events (`order.shipment.*`) and `order.payment.updated` may be better signals. Payload shape and possible deprecation still need checking.
4. Meta CAPI `Authorization: Bearer` is not documented for `/events`. The documented method is the `access_token` parameter.
5. Meta macro names and Snapchat macro names are only confirmed through secondary sources, because the official pages could not be read. TikTok macro names are confirmed.
6. Server-side custom events for optimization: no official confirmation found. The offline-conversions statement that custom events cannot be used for optimization may apply in some form. Test first.

## Source list

- TikTok: https://ads.tiktok.com/help/article/track-offsite-web-events-with-utm-parameters
- Snapchat: https://businesshelp.snapchat.com/s/article/add-url-macros?language=en_US ; https://businesshelp.snapchat.com/s/article/url-parameters?language=en_US (both not readable; search excerpts used)
- Meta URL parameters: https://www.facebook.com/business/help/2360940870872492 (title only)
- Meta pixel sharing: https://developers.facebook.com/docs/business-management-apis/business-asset-management/guides/business-pixel-sharing/
- Meta CAPI usage: https://developers.facebook.com/documentation/ads-commerce/conversions-api/using-the-api
- Meta CAPI parameters: https://developers.facebook.com/documentation/ads-commerce/conversions-api/parameters/server-event
- Meta dedup: https://developers.facebook.com/documentation/ads-commerce/conversions-api/deduplicate-pixel-and-server-events
- Meta custom conversion: https://developers.facebook.com/docs/marketing-api/reference/custom-conversion/
- Meta learning phase: https://www.facebook.com/business/help/112167992830700 (title only)
- Shopify manual payments: https://help.shopify.com/en/manual/payments/manual-payments
- Shopify orderMarkAsPaid: https://shopify.dev/docs/api/admin-graphql/latest/mutations/orderMarkAsPaid
- Shopify webhook topics enum: https://shopify.dev/docs/api/admin-graphql/latest/enums/WebhookSubscriptionTopic
- Shopify fulfillment: https://shopify.dev/docs/api/admin-rest/latest/resources/fulfillment
- Shopify order: https://shopify.dev/docs/api/admin-rest/latest/resources/order
- Shopify webhook HMAC: https://shopify.dev/docs/apps/build/webhooks/subscribe/https
- Shopify cart AJAX: https://shopify.dev/docs/api/ajax/reference/cart
- Shopify theme app extension config: https://shopify.dev/docs/apps/build/online-store/theme-app-extensions/configuration
- Salla webhooks: https://docs.salla.dev/421119m0
- Salla order statuses: https://docs.salla.dev/5394150e0
- Salla order object: https://docs.salla.dev/433804m0
- Salla update order: https://docs.salla.dev/5751605e0
- Salla device-mode snippet: https://docs.salla.dev/1724504m0
- WhatsApp templates overview: https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/overview
- WhatsApp template categorization: https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/template-categorization (search excerpt)
- WhatsApp send messages: https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-messages
- WhatsApp pricing: https://developers.facebook.com/docs/whatsapp/pricing
- WhatsApp webhook components: https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/components
- WhatsApp consumer read receipts FAQ: https://faq.whatsapp.com/3307102709559968/?locale=en_US&cms_platform=web&category=5245250 (search excerpt)
