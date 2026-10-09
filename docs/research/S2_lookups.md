# S2 Lookups: Meta CAPI, custom conversions, customer data, order and courier sources

Date: 2026-10-10. Scope: questions Q1 to Q8 from the S2 research brief.

Confidence labels:
- **Confirmed**: official documentation page read in this session.
- **Inferred**: non-official source, a partial read, or a search summary of an official page. Verify before building on it.
- **Not found**: no official source located. Do not design on an assumption here.

Method limits: Meta Business Help Center pages (facebook.com/business/help, en-gb.facebook.com/business/help) returned only the page title to the fetch tool in this session, so every help-center claim below is Inferred unless another official page confirms it. Some developer and Shopify pages were read through a summarizing fetch; quotes are close to literal and marked where paraphrased. Two Salla pages and the Aramex developer pages returned 403 or were unreachable (noted in place).

---

## Summary table

| # | Question | Answer (short) | Confidence |
|---|---|---|---|
| 1 | Are `client_user_agent` and `event_source_url` required for `action_source=website`? | Yes, both are required for website events (official). What happens when missing is not documented. Only a community quote of an Events Manager message says such events "may be discarded". | Confirmed (requirement). Not found (rejection vs. accept). Inferred (discard). |
| 1b | Q1 vs. design: `action_source` and `event_time` | Meta's 7-day `event_time` rule is confirmed. The design handles it: `DeliveredPurchase` is sent only if it can go out before order placed + 6.5 days; later deliveries become terminal `late_delivery` and are never sent; a separate 7-day stale guard also exists. `action_source` stays `website`: Motahai captures checkout client_user_agent and IP (Shopify client_details), stores them encrypted until send (D-006), and sets event_source_url from the store URL. Salla events lacking a user agent are flagged `missing_user_agent` in `capi_events.quality_flags`. | Resolved by design (cutoff + D-006) |
| 2 | Can a custom conversion on a custom server event be the optimization event of a Sales campaign? | Official docs say server events "may be used in measurement, reporting, or optimization". No doc confirms a server-only custom event as an ad-set optimization event in Ads Manager. | Partial (Not found for the exact UI path) |
| 2b | What does the custom conversion category (e.g. Purchase) do? | `custom_event_type` is a field with an enum (includes `PURCHASE` and `OTHER`). Effect on optimization is not documented. A help snippet says pick a standard category similar to the action. | Confirmed (field and enum). Inferred (effect). |
| 2c | Value optimization on custom events | Yes. "Value optimization works for all standard and custom events on the Sales objective", with `value` and `currency` (ISO 4217) sent. | Confirmed |
| 2d | ROAS goal / minimum ROAS / highest value for a custom conversion | Not found. The ROAS-goal help page could not be read and the value-optimization guide does not mention ROAS goals. | Not found |
| 3 | Learning-phase threshold and reset rules | About 50 optimization events per ad set in 7 days. Stuck ad sets get "Learning Limited". Changing optimization event, audience, or creative counts as a significant edit. The official page could not be read. | Inferred |
| 4 | Customer-list identifiers, hashing, phone format | Schema keys: `EMAIL`, `PHONE`, `FN`, `LN`, `CT`, `ST`, `ZIP`, `COUNTRY`, `GEN`, `DOBY`, `DOBM`, `DOBD`, `FI`, `MADID`, `EXTERN_ID`. SHA-256, lowercase hex, required except external IDs, app user IDs, page-scoped IDs, and MADID. Phone: remove symbols and leading zeros, include country code, no "+". Pre-hashed input is expected. | Confirmed |
| 4b | Value column, value-based lookalike, minimum audience size | No value/LTV key in the official schema list. No minimum audience size stated. Lookalike source size: "the more, the better" (help summary). The 100-person figure is third-party only. | Not found (value key, minimum). Inferred (value-based flow). |
| 5 | Meta guidance on `external_id` | "Hashing recommended." Any unique advertiser ID. Use the same format in pixel and CAPI. Same parameter set on browser and server. | Confirmed |
| 5b | Does Shopify's Facebook & Instagram app send `external_id`, and with what value? | Not found. Shopify's help page describes Enhanced/Maximum sharing via Conversions API with name, location, email, phone. It does not mention `external_id`, hashing, or customer ID. | Not found |
| 6 | Shopify: how COD orders are marked confirmed | `Order.tags` (comma-separated string) is the readable marker. COD apps (Confirmify, Confirm Bot, OTP) use tags such as COD-CONFIRMED, Confirmed/Canceled. Webhook topic payload containing tags not verified. | Confirmed (field). Inferred (app tag behavior). |
| 6b | Salla: order statuses, custom statuses, status-change webhook | Endpoints: update order status (`POST /orders/{order_id}/status`, with `slug` or `status_id` for custom sub-status), list statuses, create/update custom statuses. Store event `order.status.updated` appears in the official docs examples. Its payload `status` object has `id`, `name`, `slug`, `customized`. Events-list sample does not show the event. | Confirmed (endpoints, event name, status object example). Inferred (list statuses path). |
| 6c | WhatsApp confirmation tools that write back to Salla | Not found. | Not found |
| 7a | OTO (Saudi aggregator): webhooks, auth, delivered value | Webhook types `orderStatus`, `shipmentError`, `newOrders`. `orderStatus` signed HMAC-SHA256 over `orderId:status:timestamp`, Base64, using the secretKey set at registration. Delivered status value appears as `delivered` in the example. Timestamp is UTC. API auth and endpoint names only from third-party mirrors. | Confirmed (webhook, signature from official help article). Inferred (API auth and endpoints). |
| 7b | Torod (Saudi aggregator) | No public API or webhook docs found. Help article says tracking updates come via API or webhooks, and integration requires contacting Torod. | Not found (details) |
| 7c | Bosta (Egypt): webhooks, auth, delivered state | Auth: header `Authorization: <API_KEY>`, raw key, no prefix. Webhook set in dashboard (Settings > API Integration) or per delivery via `webhookUrl` and `webhookCustomHeaders`. Payload has `state` (number) and `cod` (number, only in Delivered state) and `isConfirmedDelivery` (boolean). Delivered = state **45**. No HMAC signature documented. | Confirmed |
| 7d | Aramex: tracking API and auth | Official manual is SOAP-based. Auth described as email+password plus account number+PIN (from a search summary of the manual). The PDF was unreachable this session. Delivered code not found. | Inferred (manual unreachable) |
| 7e | SMSA Express: tracking API and auth | SOAP "eCommerce Web API" at `http://track.smsaexpress.com/SECOM/SMSAwebService.asmx`. 25 operations including `getTrack`, `getTracking`, `getTrackingByRef`, `getStatus`, `getStatusByRef`, `getShipUpdates`, `getDeliveredShipments`, `getShipCharges`. Auth not read (WSDL not opened). Delivered code not found. Endpoint is plain HTTP. | Confirmed (operation list). Not found (auth, delivered code). |
| 7f | J&T Express in Gulf/Egypt | No public developer API for Egypt or KSA found. Only an Indonesian developer portal (developer.jet.co.id) found. | Not found |
| 7g | COD remittance and settlement reports | Bosta: dashboard describes daily COD cycle tracking, receipts per transfer, bank-transfer cash-out. No file or API format found for any courier. | Inferred (Bosta, marketing text). Not found (format). |
| 8a | Does `current_total_price` reflect edits and refunds? | GraphQL `currentTotalPriceSet`: "The total price of the order, after returns... This includes taxes and discounts." It does not mention refunds or order edits. The REST field description was not found. | Confirmed (text). Not found (explicit edit/refund behavior). |
| 8b | Structure of `refunds[].transactions[]` | REST: refund fields `refund_line_items`, `transactions`, `order_adjustments`, `processed_at`. Transaction example fields `amount`, `kind`, `status`, `gateway`, `processed_at`, `currency`, `order_id`, `parent_id`. GraphQL OrderTransaction: `amountSet`, `kind`, `status`, `gateway`, `processedAt`, `parentTransaction`. Kind enum: AUTHORIZATION, CAPTURE, CHANGE, EMV_AUTHORIZATION, REFUND, SALE, SUGGESTED_REFUND, VOID. | Confirmed |

---

## Design conflicts (read before building)

1. **Q1 / event_time (resolved by design).** Meta rule (Confirmed): "The `event_time` can be up to 7 days before you send an event to Facebook." If any event in a request is more than 7 days old, "we return an error for the entire request and process no events." URL: https://developers.facebook.com/docs/marketing-api/conversions-api/parameters/server-event

   The design sets `DeliveredPurchase.event_time` = order placed time, which stays within the 7-day limit because the design enforces a cutoff: `DeliveredPurchase` is sent only if it can be sent before order placed + 6.5 days. Later deliveries become a terminal `late_delivery` state and are never sent. The code also has a separate 7-day stale guard. Placed time is used on purpose for attribution: Meta's 7-day click window runs from the ad click to `event_time`, so using delivery time would push many conversions outside the window.

   [Test] Confirm that events with `event_time` 3 to 6 days in the past are accepted and attributed (see pilot plan).

2. **Q1 / action_source (resolved by design).** Website events require `client_user_agent`, `action_source`, and `event_source_url` (Confirmed). The design keeps `action_source = website`. Motahai has browser context for these orders: it captures the checkout client_user_agent and IP from the platform (Shopify client_details), stores them encrypted until send (decision D-006), and sets event_source_url from the store URL. Website is also what makes these events usable by Website-conversion-location Sales ad sets. Events without a user agent (for example Salla until a capture path exists) are flagged `missing_user_agent` in `capi_events.quality_flags`. Whether Meta accepts or discards them is a [Test] item for Events Manager diagnostics. Non-website values (`other`, `phone_call`, `chat`) are only a fallback experiment, with the risk that they are not selectable for website ad sets. URL: https://developers.facebook.com/docs/marketing-api/conversions-api/parameters/server-event

3. **Q1 / match gating.** Meta: "You cannot use unmatched events for attribution or ad delivery optimization, but you can still use them for basic measurement." So ConfirmedOrder and DeliveredPurchase only drive optimization if they match. Phone (hashed, normalized, with country code) is the main identifier for COD, so it must be sent on both events. Add `client_ip_address` and `client_user_agent` if available, and an `external_id` (see Q5). Confirmed. URL: https://developers.facebook.com/documentation/ads-commerce/conversions-api/best-practices

4. **Q1 / match quality reporting.** The Dataset Quality API page says: "EMQ is currently available only for web events." So an Event Match Quality score may not appear for ConfirmedOrder/DeliveredPurchase. The pilot must read match via Dataset Quality API and Events Manager diagnostics, not an EMQ number per event. Confirmed. URL: https://developers.facebook.com/docs/marketing-api/conversions-api/dataset-quality-api/

5. **Q2 / category Purchase.** The design uses custom conversions with category Purchase. The only official statement on category is the enum (`PURCHASE` and `OTHER` are both valid `custom_event_type` values). Whether a Purchase-category custom conversion changes optimization, reporting, or dedup with native Purchase is not documented. Test it in the pilot. Confirmed (enum), Not found (effect). URL: https://developers.facebook.com/docs/marketing-api/reference/custom-conversion/

6. **Q2 / ROAS goals.** Value optimization is confirmed for custom events. ROAS-specific goals (minimum ROAS, etc.) are not confirmed for custom events. Do not promise a minimum-ROAS campaign on ConfirmedOrder/DeliveredPurchase until the pilot shows it in the UI. Confirmed (value optimization), Not found (ROAS goal). URL: https://developers.facebook.com/documentation/ads-commerce/conversions-api/guides/value-optimization

7. **Q4 / headers.** The design says "phone, email, value" column headers. The official schema keys are uppercase `EMAIL`, `PHONE`, and so on. No official value/LTV key was found. Confirmed for keys; Not found for value. URL: https://developers.facebook.com/docs/marketing-api/audiences/guides/custom-audiences

8. **Q5 / external_id.** Do not assume Shopify sends `external_id`. Motahai should send its own `external_id` with the same format on pixel and CAPI, if used. Confirmed (Meta guidance); Not found (Shopify behavior).

9. **Q7 / Bosta state.** The design uses a generic "delivered" concept. Bosta's delivered state is numeric `45`. `isConfirmedDelivery` (boolean) also exists. Map on state and this flag, not on text. Confirmed.

10. **Q6 / Salla event.** The official events-list sample (`/webhooks/events`) shows only `order.created` and `order.updated`. Other official docs show `order.status.updated`. Confirm with a live GET of the events list before building the handler. Conflicting, Inferred for the live list.

---

## Q1. Meta CAPI: client_user_agent and event_source_url for action_source=website

**Answer.** Both are required for website events. The official table lists `event_source_url` and `client_user_agent` as required for "All website events". The parameters page says: "Website events shared using the Conversions API require the client_user_agent, action_source, and event_source_url."

**Consequence when missing: not found.** The official docs state no rejection or diagnostic behavior for these two fields. They say the parameters "contribute to improving the quality of events used for ad delivery and may improve campaign performance." The only consequence text found is a quote in a Meta developer community thread, which quotes an Events Manager message: "Any events received through Conversions API that do not have these parameters may be discarded." The thread answer is not identified as Meta staff. Inferred.

**Related exact fields (official):**
- `action_source` (required for all events; allowed values: `email`, `website`, `app`, `phone_call`, `chat`, `physical_store`, `system_generated`, `business_messaging`, `other`).
- `event_time` (Unix seconds, GMT, max 7 days old; whole request rejected otherwise).
- `event_id` (dedupe with pixel within 48 hours on matching `event_name` and `event_id`).
- `user_data.client_user_agent` (not hashed), `user_data.client_ip_address` (not hashed).

**URLs:**
- https://developers.facebook.com/docs/marketing-api/conversions-api/parameters/server-event (Confirmed)
- https://developers.facebook.com/documentation/ads-commerce/conversions-api/parameters (Confirmed)
- https://developers.facebook.com/documentation/ads-commerce/conversions-api/best-practices (Confirmed)
- https://developers.facebook.com/documentation/ads-commerce/conversions-api/deduplicate-pixel-and-server-events (Confirmed)
- https://developers.facebook.com/community/threads/453778469208591/ (Inferred; community)

**Confidence:** requirement Confirmed. Rejection vs. accept Not found. Discard quote Inferred.

---

## Q2. Custom conversions as optimization events

**2a. Custom conversion on a custom server event as optimization event.** Not found as an explicit official statement. What is confirmed:
- The Conversions API overview says: "server events may be used in measurement, reporting, or optimization in a similar way as other connection channels." Server events are linked to a dataset ID. Confirmed. https://developers.facebook.com/documentation/ads-commerce/conversions-api
- The custom conversion object (Marketing API) has fields `id`, `name`, `description`, `custom_event_type`, `event_source_type`, `pixel`, `rule`, `default_conversion_value`, `retention_days`, `is_archived`, `last_fired_time`, `first_fired_time`. Its description says the conversion is used to "Optimize ads delivery based on online and offline conversion events that you define." Confirmed. https://developers.facebook.com/docs/marketing-api/reference/custom-conversion/
- Allowed `custom_event_type`: `ADD_PAYMENT_INFO, ADD_TO_CART, ADD_TO_WISHLIST, COMPLETE_REGISTRATION, CONTENT_VIEW, INITIATED_CHECKOUT, LEAD, PURCHASE, SEARCH, CONTACT, CUSTOMIZE_PRODUCT, DONATE, FIND_LOCATION, SCHEDULE, START_TRIAL, SUBMIT_APPLICATION, SUBSCRIBE, LISTING_INTERACTION, FACEBOOK_SELECTED, OTHER`. Confirmed.
- The custom-conversion reference page makes no statement about server/CAPI sources.
- Marketing API: "ads optimized for custom conversions" via `promoted_object` could not be read (page returned 404). Not found for the exact `promoted_object` fields in this session.
- The promoted_object pattern for custom event optimization in app ads uses `custom_event_type` `OTHER` and `custom_event_str` (from a search summary; mobile-app specific). Not found for server-only events. Inferred.

**2b. What the category does.** From a Meta help summary (URL-based custom conversions): "you can create a custom conversion and choose a standard event category that's similar to the action you want to optimize." The steps are "Set the conversion event to All URL Traffic" then "Under Choose a Standard Event for Optimization, pick Purchase." This is the URL-rule flow, not a server-event flow. Inferred. https://www.facebook.com/business/help/434245993430255

Other help-summary claims (Inferred): custom conversions cannot be edited for rules after creation; ad sets optimized for deleted conversions still deliver but may perform poorly; inactive conversions archive after 2 years; account cap 100 (developer doc summary; an older doc says 40).

**2c. Value optimization on custom events.** Confirmed. From the value-optimization guide:
- "Value optimization works for all standard and custom events on the Sales objective."
- "Value and currency should be added to existing events you want to use value optimization for."
- Pixel: `value` and `currency`. CAPI: include `value` and `currency` in `custom_data`. Currency must be ISO 4217 three-letter code.
- "Our system values conversions proportional to the value that is passed back."
- Minimum data volume: not stated.
- URL: https://developers.facebook.com/documentation/ads-commerce/conversions-api/guides/value-optimization

**2d. ROAS goal / minimum ROAS / highest value for custom conversion.** Not found. The value-optimization guide mentions neither ROAS goals nor "maximize value" goals by name. The ROAS-goal help page (https://www.facebook.com/business/help/1113453135474912) returned title only. Treat "Maximize value of conversions" as Inferred for custom events and "minimum ROAS" as Not found.

---

## Q3. Learning phase

**Threshold.** About 50 optimization events per ad set within 7 days. Inferred. Sources: third-party guides consistently state it; the Meta help page "About the learning phase" (https://www.facebook.com/business/help/112167992830700/) returned title only.

**Official-page summary (search snippet, Inferred).** Meta's "Learning limited" help article (https://en-gb.facebook.com/business/help/269269737396981) reportedly says an ad set is flagged when it is unlikely to reach around 50 optimization events in the week after its last significant edit. If it reaches 50 events since the last significant edit, it moves from learning limited to active. The page body was not readable, so quote-level verification is still required.

**Stuck ad sets (Inferred, same summary).** Combine ad sets or campaigns, expand audience, raise budget or bid/cost control, choose a more frequent optimization event.

**Significant edits (Inferred).** Last Significant Edit help article (https://www.facebook.com/business/help/942374239243867) reportedly lists pausing, changing the optimization event, audience, or creative as significant. Bid strategy or budget changes "may" be significant depending on magnitude. Budget thresholds (10% vs 20%) conflict across third-party sources. Not found in official text. Adding an ad may or may not reset (a third-party test contradicts the help text). Treat all as Inferred.

**Answer to "does changing the optimization event reset learning?"** Inferred yes: it is a named significant edit in the summary. Do not change the optimization event mid-test. This matches the pilot design.

---

## Q4. Customer list custom audiences

**Identifier schema keys (official, Confirmed).** From the Marketing API customer-file guide, the `schema` accepts a single key or multi-key list from: `EMAIL`, `PHONE`, `GEN`, `DOBY`, `DOBM`, `DOBD`, `LN`, `FN`, `FI`, `CT`, `ST`, `ZIP`, `COUNTRY`, `MADID`. `EXTERN_ID` and `PAGEUID` are documented in separate sections. Example payloads also use `EMAIL_SHA256` and `DOBYM` (not defined on the page).

Note: the design says "phone, email, value". The official key names are `PHONE` and `EMAIL` in the API schema. CSV header names in Ads Manager were not found officially. Third-party guides say headers like `em`, `ph`, `fn`, `ln`, `ct`, `st`, `zip`, `country`, `dob`, `ge`, `value`, `external_id`. Inferred.

URL: https://developers.facebook.com/docs/marketing-api/audiences/guides/custom-audiences

**Hashing (Confirmed).**
- "You must hash data as `SHA256`; we don't support other hashing mechanisms."
- Required for all data except External Identifiers, App User IDs, Page Scoped User IDs, and Mobile Advertiser IDs. MADID: "Do not hash."
- Lowercase hex: "Provide SHA256 values for normalized keys and HEX representations of this value, using lowercase for A through F."
- "you must share your data in a hashed format to maintain privacy." So pre-hashed input is the expected path.

**Phone normalization (Confirmed).**
- "Remove symbols, letters, and any leading zeroes."
- "You should prefix the country code if the `COUNTRY` field is not specified."
- Example: `hash("sha256", "15559876543")` = `1ef970831d7963307784fa8688e8fce101a15685d62aa765fed23f3a2c576a4e`.
- So "digits with country code, no +" is correct. The page does not specify E.164.

**Email normalization (Confirmed, from the customer information parameters page).** Trim leading and trailing spaces, then lowercase. https://developers.facebook.com/docs/marketing-api/conversions-api/parameters/customer-information-parameters

**Name and location normalization (Confirmed, same page).** `fn` and `ln`: lowercase, no punctuation (Roman a-z recommended). `ct`: lowercase, no spaces or punctuation. `zp`: lowercase, no spaces or dashes. `country`: lowercase ISO 3166-1 alpha-2. `db`: YYYYMMDD.

**Value column and value-based lookalike.**
- Value key in the official schema: Not found.
- "You cannot replace a value-based customer file custom audience." Confirmed (value-based audiences exist).
- Value-based lookalike flow: create a lifetime-value custom audience first, then a lookalike from it. Inferred (search summary of official help; title-only fetch).
- Value definitions (AOV, CLV, membership flag): Inferred (third-party); Meta's own page not read.

**Minimum audience size.**
- Official customer-file page: no minimum. Unlimited records, max 10,000 per request. Replace requires existing audience smaller than 100 million. Account limit: "Standard Data File Custom Audiences: 500." Confirmed.
- Lookalike source size: help summary says no hard minimum, "the more, the better." Inferred.
- "100 customers minimum" for value-based audiences: third-party only. Not found officially.

**Other (Confirmed).** `EXTERN_ID` data retention is 90 days. Customer list changes can take up to 24 hours (third-party; not verified).

**Confidence:** schema keys and hashing Confirmed. Value key Not found. Minimum size Not found.

---

## Q5. external_id

**Meta guidance (Confirmed).** https://developers.facebook.com/docs/marketing-api/conversions-api/parameters/customer-information-parameters
- `external_id`: "Hashing recommended."
- "Any unique ID from the advertiser, such as loyalty membership IDs, user IDs, and external cookie IDs."
- "You can send one or more external IDs for a given event."
- "If an External ID is being sent via other channels, it should be in the same format as when sent via the Conversions API."
- Pixel: `fbq('init', 'PIXEL_ID', {'external_id': 12345});`
- Parity: "make sure to apply the same set of customer information parameters your system is currently sharing to the browser side to the server side."
- Dedup note: "you must use `event_name`, `fbp` and/or `external_id` consistently across browser and server events." https://developers.facebook.com/documentation/ads-commerce/conversions-api/deduplicate-pixel-and-server-events
- Best-practice page: "Also include the `external_id` and `event_id` event parameters for all events." https://developers.facebook.com/documentation/ads-commerce/conversions-api/best-practices

**Shopify Facebook & Instagram (Not found).** Shopify's help page (https://help.shopify.com/en/manual/promoting-marketing/analyze-marketing/meta-data-sharing) says Standard uses the pixel only. Enhanced adds the Conversions API and shares "your customer's name, location, email address, and phone number". Maximum adds the same plus "Facebook's latest advertising technology." The page does not mention `external_id`, customer ID, or hashing. No other Shopify source found. Confirmed (text), Not found (external_id).

**Implication.** Motahai should set its own `external_id` (for example a hashed internal customer or order-customer ID) and use the same value and format on every event it sends. Do not assume Shopify's app sends one.

---

## Q6. Order confirmation signals

### Shopify
- **Field.** `Order.tags`: "A comma separated list of tags associated with the order." Confirmed. https://shopify.dev/docs/api/admin-graphql/latest/objects/Order (REST field `tags` naming Inferred).
- `Order.note` and `Order.customAttributes` also exist (Confirmed) and can carry a status, but tags are the usual marker.
- **Webhook payload containing tags.** Not verified in this session. Inferred that the `orders/updated` payload contains `tags` (REST order shape). Verify with a test order.
- **COD confirmation apps (third-party listings, Inferred).**
  - Confirmify (Ecom Smartify, https://apps.shopify.com/confirmify): auto-tags `COD-CONFIRMED`, `COD-CANCELLED`, `COD-PENDING`. The pricing section says `COD-CONFIRMED, CANCELED` (inconsistent). WhatsApp only in the listing. Tag write-back to Shopify is implied by "Their reply updates the order status using tags". Confirmed for listing text; write-back Inferred.
  - Confirm Bot (Momo Digitals, https://apps.shopify.com/confirm-bot): tags Confirmed, Canceled, Contact on reply. Listing URL returned 404 in this session; from a search summary only. Inferred.
  - OTP - CoD Order Verification: verifies with an OTP over SMS or WhatsApp. Free tier SMS only to Egyptian networks. Inferred (search summary).
  - COD Order Confirmation for Saudi Arabia (Softpulse Infotech): calls and SMS, not WhatsApp. Inferred.
- Market note: COD is popular in Saudi, Egypt, and UAE (DelightChat summary). Inferred.

### Salla
- **Update an order's status (Confirmed).** `POST https://api.salla.dev/admin/v2/orders/{order_id}/status`. Body: `slug` (predefined status), `status_id` (custom order sub-status), `note`, `restore_items`, `send_status_sms`. Scope `orders.read_write`. Auth: OAuth 2.1 authorization code with `offline_access`. Responses 201, 401, 404, 422. https://docs.salla.dev/5394148e0
- **Statuses list (Inferred path).** `GET https://api.salla.dev/admin/v2/orders/statuses` returns statuses and sub-statuses. Page title "List Order Statuses" exists (https://docs.salla.dev/project-451700/api-5394150?nav=1 returned 403 in this session); path from a third-party summary.
- **Custom status create/update (Inferred).** `POST /orders/statuses` with required `parent_id`, `name`, `message`. `PUT /orders/statuses/{status_id}`. Scope for create: `orders.read_write` (third-party). Title pages "Create Custom Order Status" and "Update Custom Order Status" exist on docs.salla.dev.
- **Status object in order payloads (Confirmed example).** Example: `"status": { "id": 566146469, "name": "...", "slug": "under_review", "customized": { "id": ... } }`. Use `slug` and `customized.id` to identify merchant custom statuses. https://docs.salla.dev/433804m0
- **Status-change webhook (Confirmed name; payload partial).** `order.status.updated` appears in the Order webhook model examples: `"event": "order.status.updated"`. The docs say 10 order webhook events share one model. The Webhooks reference (https://docs.salla.dev/421119m0) reportedly lists 17 order events including `order.status.updated`, `order.created`, `order.updated`, `order.cancelled`, `order.refunded`, `order.shipment.creating`. Events list endpoint sample (https://docs.salla.dev/5394136e0) shows only `order.created` and `order.updated`. Conflict. Verify with a live `GET /webhooks/events` (requires `webhooks.read`).
- **Webhook security (Confirmed).** Partner apps default to `signature`. Header `X-Salla-Security-Strategy: Signature` and the signature in `X-Salla-Signature`. Verification: compute HMAC-SHA256 of the raw body with the secret (per Node.js and PHP examples) and compare with timing-safe equality. Note: the page says "64 character SHA256 hash" for signature and "32 character" for token, which conflicts. Token strategy uses an `Authorization` header. Secret is set when registering the webhook. https://docs.salla.dev/421119m0
- **Retries (Confirmed).** Three attempts about five minutes apart; response wait about 30 seconds; stops after a success.
- **Communication webhook of the same name.** A notification webhook `order.status.updated` also exists for WhatsApp, SMS, or email messages (Communication Webhooks). Do not confuse with the store event. Confirmed (search summary of https://docs.salla.dev/1380572m0).
- **WhatsApp tools writing back to Salla (Not found).** Not searched exhaustively. Salla exposes status-update endpoints, so a tool could write statuses. Which tools do so: not found.

---

## Q7. Couriers and delivery status

### OTO (Saudi aggregator)
- **Official help article (Confirmed).** https://help.tryoto.com/en/support/solutions/articles/150000213819-webhook
  - "There are 3 types of webhook for now, `newOrders,` `orderStatus` and `shipmentError`."
  - `orderStatus` payload fields include `orderId`, `status`, `dcStatus`, `note`, `pickupLocationCode`, `printAWBURL`, `trackingNumber`, `dcTrackingNumber`, `trackingUrl`, `deliveryCompany`, `timestamp`, `signature`.
  - Example status value is `shipmentProcessing|delivered|returned|...` (casing inconsistent in text). Delivered = `delivered` in the example. Confirmed (example), exact list Not found.
  - Signature: "Signed `orderId:status:timestamp` with `HmacSHA256` method and Base64 Encoded string." `shipmentError`: `orderId:errorCode:timestamp`.
  - `timestamp` is UTC.
  - `authorizationKey`: "Secure token used to authenticate and validate incoming webhook requests." Header name and format not specified (Partial).
- **API auth and endpoints (Inferred, third-party mirrors only).** Bearer access token from a refresh token in the OTO dashboard (Sales Channel > OTO API). Webhook registration `POST https://api.tryoto.com/rest/v2/webhook` (`method`, `url`). Status update `POST https://api.tryoto.com/rest/v2/updateOrder` (Picked Up, Delivered, Returned) or `POST /rest/v2/updateOrderStatus` (conflicting names). Verify against OTO's current API docs.
- **COD amount in webhook.** Not found.

### Torod (Saudi aggregator)
- Only official source found: help article https://help.torod.co/en/?p=53. Integration is not self-serve (contact Torod). Tracking updates come through API or webhooks. Covers Saudi carriers (SPL, SMSA, Aramex, Zajil) per a third-party listing. Inferred.
- Webhook payload, status codes (is "delivered" one), COD fields, auth: Not found. Ask Torod for the API reference and sandbox.

### Bosta (Egypt) (Confirmed)
- **Auth.** Header `Authorization: <YOUR_API_KEY>`, raw value, no prefix. Base URL in docs: `http://app.bosta.co/api/v2/<endpoint>` (plain HTTP as documented; use HTTPS in production after checking). https://docs.bosta.co/docs/how-to/get-your-api-key (fetched via http redirect)
- **Webhook setup.** Dashboard: Settings > API Integration > Request OTP > Set Up Your Webhook. "Webhook URL" mandatory. Optional Authorization Key and custom name ("both the key name and its corresponding value must be entered together"). Per-delivery: `webhookUrl` and `webhookCustomHeaders` (object) on create delivery `/api/v2/deliveries?apiVersion=1`. Example custom header `"Authorization" : "Basic abc123"`. https://docs.bosta.co/docs/how-to/get-delivery-status-via-webhook
- **Trigger.** "Your webhook is triggered only when an order status changes, not on order creation."
- **Payload fields.** `_id`, `trackingNumber`, `state` (Number), `type`, `cod` (Number, "only in Deliverd state" [sic]), `timeStamp` (Number), `isConfirmedDelivery` (Boolean), `deliveryPromiseDate`, `exceptionReason`, `exceptionCode`, `businessReference`, `numberOfAttempts`.
- **Signature.** No HMAC or signature documented on the webhook page. Auth is the custom header you set. Not found for signing.
- **State codes (relevant).** 10 Pickup requested; 20 Route assigned; 21 Picked up from business; 24 Received at warehouse; 25 Fulfilled; 30 In transit between hubs; 40 Picking up (Only for Cash Collection); 41 Picked up; **45 Delivered**; 46 Returned to business; 47 Exception; 48 Terminated; 49 Canceled; 60 Returned to stock; 100 Lost; 101 Damaged; 102 Investigation; 103 Awaiting your action; 104 Archived; 105 On hold.
- **Exceptions.** `exceptionReason` and `exceptionCode` with state 47. Forward codes 1–8, 12–14, 100, 101; return codes 20–30.
- **Remittance.** Bosta business dashboard says merchants can track COD cycles daily, get receipts per transfer, and cash out by bank transfer. Inferred (marketing page via search). Export format: Not found.

### Aramex
- Official manual at https://dr.aramex.com/docs/default-source/resourses/resourcesdata/shipments-tracking-api-manual.pdf. Not reachable in this session (connection refused). Details below come from a search summary: Tracking API is SOAP over HTTPS; authentication has email and password (registered on aramex.com) plus account number and PIN; request by AWB number(s); option for latest update only. Inferred.
- ClientInfo field names (`UserName`, `Password`, `Version`, `AccountNumber`, `AccountPin`, `AccountEntity`, `AccountCountryCode`, `Source`): Not found in a read official source. The manual's wording differs.
- Credentials: issued after contacting an Aramex rep; separate sandbox and production sets (third-party AfterShip guide). Inferred.
- Delivered status code and webhook: Not found.
- Developer center page https://www.aramex.com/developers-solution-center returned 403 in this session.

### SMSA Express (Confirmed for operation list)
- SOAP "eCommerce Web API": `http://track.smsaexpress.com/SECOM/SMSAwebService.asmx`. Plain HTTP as published. 25 operations: addShip, addShipMPS, addShipPDF, addShipment, addShipmentDelv, cancelShipment, getAllRetails, getAllRetailsTiming, getDeliveredShipments, getPDF, getPDFBr, getPDFSino, getRTLCities, getRTLRetails, getRTLRetailsTiming, getShipCharges, getShipUpdates, getShipmentUpdates, getStatus, getStatusByRef, getTrack, getTracking, getTrackingByRef, getTrackingwithRef, stoShipment.
- Tracking calls for status: `getTrack`, `getTracking`, `getTrackingByRef`, `getStatus`, `getStatusByRef`, `getShipUpdates`, `getShipmentUpdates`, `getDeliveredShipments` (list of delivered shipments; likely the cleanest delivery feed). Inferred on purpose.
- Auth: Not found (WSDL/service description not read). REST option mentioned on a portal update note. Not found in detail.
- Delivered status code: Not found. Webhook: Not found (ClickPost third-party says webhook/polling). Inferred.
- Business sign-up: via track.smsaexpress.com eCommerce portal or smsaexpress.com. Inferred.

### J&T Express (Gulf and Egypt)
- Not found. The only official developer portal found is for Indonesia: https://developer.jet.co.id/documentation/index. Confirmed existence; does not cover Egypt or KSA per the search result.

### COD remittance and settlement reports
- Bosta: dashboard-level COD cycle reports and transfer receipts (Inferred). Formats (CSV, email, portal): Not found.
- OTO, Torod, Aramex, SMSA, J&T: Not found.

---

## Q8. Shopify order totals and refunds

**`current_total_price` / `currentTotalPriceSet`.**
- GraphQL `Order.currentTotalPriceSet`: "The total price of the order, after returns, in shop and presentment currencies. This includes taxes and discounts." Confirmed.
- It does NOT say refunds or order edits are included. The sibling field `currentCartDiscountAmountSet` explicitly says "after returns, refunds, order edits, and cancellations", so the omission is informative. Inferred: the total does not reflect refunds directly; it reflects returns. Behavior after edits: Not found.
- `totalPriceSet`: "before returns." `totalRefundedSet`: "The total amount that was refunded." `netPaymentSet`: "based on the total amount received minus the total amount refunded." `totalReceivedSet`: "The total amount received from the customer before returns." Confirmed (text).
- REST `current_total_price`: field exists in the sample, description not found in the fetch.
- Source: https://shopify.dev/docs/api/admin-graphql/latest/objects/Order and https://shopify.dev/docs/api/admin-rest/latest/resources/order

**Refund and transactions structure.**
- REST refund object: `id`, `order_id`, `created_at`, `note`, `user_id`, `processed_at`, `refund_line_items`, `transactions`, `order_adjustments`. "Each refund is a record of money being returned to the customer." Use `calculate` endpoint to check amounts. Confirmed. https://shopify.dev/docs/api/admin-rest/latest/resources/refund
- REST transaction example fields: `amount` ("209.00"), `kind` ("refund" in REST examples), `status` ("success"), `gateway`, `processed_at`, `currency`, `order_id`, `parent_id`. Confirmed (examples); REST definitions not read.
- GraphQL `Refund`: `transactions` (OrderTransactionConnection), `refundLineItems`, `totalRefundedSet`, `orderAdjustments`, `processedAt`. "The existence of a Refund object doesn't guarantee that the money has been returned to the customer." Confirmed.
- GraphQL `OrderTransaction`: `amountSet` (MoneyBag), `kind` (OrderTransactionKind), `status` (OrderTransactionStatus), `gateway`, `processedAt`, `parentTransaction`. Confirmed. https://shopify.dev/docs/api/admin-graphql/latest/objects/OrderTransaction
- `OrderTransactionKind` values: AUTHORIZATION, CAPTURE, CHANGE, EMV_AUTHORIZATION, REFUND, SALE, SUGGESTED_REFUND, VOID. Confirmed. https://shopify.dev/docs/api/admin-graphql/latest/enums/OrderTransactionKind
- `OrderTransactionStatus` values: not read directly. Refund page summary says transactions can be pending, processing, success, or failure. Partial.
- Note the case mismatch: REST sample uses `kind: "refund"` and GraphQL enum uses `REFUND`. Map both.

**Suggested net-collected formula (Inferred, to verify on a test store).**
`net_collected = sum(amountSet of transactions where kind in (SALE, CAPTURE) and status = SUCCESS) - sum(amountSet of transactions where kind = REFUND and status = SUCCESS)` across the order's transactions and refunds. For COD orders marked paid manually, check how the manual payment appears as a transaction. Not verified.

For COD, the courier's COD-collected amount (Bosta `cod`, OTO if present) is the true cash source. Use Shopify transactions only as a cross-check.

---

## Open [Test] questions for the pilot

1. Does a custom conversion built on the server custom event `ConfirmedOrder` appear in the ad-set optimization dropdown for a Sales/Website campaign? (Q2)
2. Does choosing category `PURCHASE` vs `OTHER` change optimization, dedup, or reporting? (Q2)
3. Are `value` and `currency` in `custom_data` enough to show "Maximize value of conversions" for `DeliveredPurchase`? Is a ROAS minimum available? (Q2)
4. What does Events Manager show for match quality on server-only custom events, given EMQ is documented for web events only? (Q1, Dataset Quality API)
5. With `action_source = website` and a captured client_user_agent / event_source_url, are events lacking a user agent (Salla until a capture path exists, flagged `missing_user_agent`) accepted or discarded, per Events Manager diagnostics? (Q1)
5b. Fallback experiment only: do non-website action sources (`other`, `phone_call`, `chat`) match phone-only identifiers and stay selectable for website-conversion-location Sales ad sets? Risk: they may not be selectable for website ad sets. (Q1, Q4)
5c. Confirm events with `event_time` 3 to 6 days in the past are accepted and attributed (design cutoff is placed + 6.5 days; attribution uses the 7-day click window from ad click to event_time). (Q1)
6. Exact learning threshold and significant-edit list in the current Help Center text. (Q3)
7. Does the uploaded phone customer file match at the expected rate with normalized, SHA-256, country-coded phones? (Q4)
8. Does the `external_id` we send match on pixel and CAPI, and does the dedup count stay correct? (Q5)
9. Does the `orders/updated` webhook include `tags` after a confirmation app writes its tag? (Q6)
10. Salla: does `GET /webhooks/events` list `order.status.updated`, and what is the exact `data` schema? (Q6)
11. OTO: exact auth header, endpoint names, and status strings for delivered; is there a COD amount field? (Q7)
12. Torod and J&T: is there a public API for Egypt/KSA? Who issues credentials? (Q7)
13. Aramex: ClientInfo fields and delivered code from the official WSDL. SMSA: auth model and delivered code. (Q7)
14. Bosta: confirm HTTPS base URL and the signature/no-signature behavior, and COD settlement report format. (Q7)
15. Shopify: `current_total_price` after an order edit (not only a refund). Does a manual COD "paid" action create a SALE transaction? (Q8)

---

## Source list (official unless marked)

Meta:
- https://developers.facebook.com/docs/marketing-api/conversions-api/parameters/server-event
- https://developers.facebook.com/docs/marketing-api/conversions-api/parameters/customer-information-parameters
- https://developers.facebook.com/documentation/ads-commerce/conversions-api/best-practices
- https://developers.facebook.com/documentation/ads-commerce/conversions-api/parameters
- https://developers.facebook.com/documentation/ads-commerce/conversions-api/deduplicate-pixel-and-server-events
- https://developers.facebook.com/docs/marketing-api/conversions-api/dataset-quality-api/
- https://developers.facebook.com/documentation/ads-commerce/conversions-api/guides/value-optimization
- https://developers.facebook.com/docs/marketing-api/reference/custom-conversion/
- https://developers.facebook.com/docs/marketing-api/audiences/guides/custom-audiences
- https://developers.facebook.com/documentation/ads-commerce/conversions-api
- https://www.facebook.com/business/help/112167992830700/ (title only)
- https://en-gb.facebook.com/business/help/269269737396981 (title only; summary Inferred)
- https://www.facebook.com/business/help/942374239243867 (title only; summary Inferred)
- https://www.facebook.com/business/help/434245993430255 (summary Inferred)
- https://www.facebook.com/business/help/185705781836755 (title only)
- https://www.facebook.com/business/help/1113453135474912 (title only)
- https://developers.facebook.com/community/threads/453778469208591/ (community, Inferred)

Shopify:
- https://shopify.dev/docs/api/admin-graphql/latest/objects/Order
- https://shopify.dev/docs/api/admin-graphql/latest/objects/Refund
- https://shopify.dev/docs/api/admin-graphql/latest/objects/OrderTransaction
- https://shopify.dev/docs/api/admin-graphql/latest/enums/OrderTransactionKind
- https://shopify.dev/docs/api/admin-rest/latest/resources/order
- https://shopify.dev/docs/api/admin-rest/latest/resources/refund
- https://help.shopify.com/en/manual/promoting-marketing/analyze-marketing/meta-data-sharing
- https://apps.shopify.com/confirmify (listing text)
- https://apps.shopify.com/confirm-bot (404 in this session; summary Inferred)

Salla:
- https://docs.salla.dev/5394148e0 (Update Order Status)
- https://docs.salla.dev/433804m0 (order webhook models)
- https://docs.salla.dev/421119m0 (webhook security and retries)
- https://docs.salla.dev/5394136e0 (List Events sample)
- https://docs.salla.dev/1380572m0 (communication webhooks; summary)
- https://docs.salla.dev/project-451700/api-5394150?nav=1 (403 in this session)

Couriers:
- https://help.tryoto.com/en/support/solutions/articles/150000213819-webhook (OTO, Confirmed)
- https://docs.bosta.co/docs/how-to/get-your-api-key (fetched via http://docs.bosta.co/docs/how-to/get-your-api-key/)
- http://docs.bosta.co/docs/how-to/get-delivery-status-via-webhook/
- http://track.smsaexpress.com/SECOM/SMSAwebService.asmx
- https://dr.aramex.com/docs/default-source/resourses/resourcesdata/shipments-tracking-api-manual.pdf (unreachable in this session)
- https://help.torod.co/en/?p=53 (help article)
- https://developer.jet.co.id/documentation/index (Indonesia only)
