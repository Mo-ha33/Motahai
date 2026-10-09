# S2-7 Pilot Validation Plan (first pilot merchant)

Date: 2026-10-10. Owner: media buyer + Motahai operator. Companion: `docs/research/S2_lookups.md` (open [Test] items are listed there, section "Open [Test] questions"). Items marked [Test] are not confirmed by official docs and must be checked in this pilot.

Design under test:
- Native Meta `Purchase` is left untouched.
- `ConfirmedOrder`: custom server event when the merchant confirms a COD order (phone, WhatsApp, or tag). Candidate ad-set optimization signal.
- `DeliveredPurchase`: custom server event when cash is collected. Truth and ROAS signal. Sent after a settlement window.

Event-time rule: Meta accepts `event_time` up to 7 days in the past (confirmed; a request with any older event is rejected whole). The design keeps `event_time` = order placed time, which stays inside that limit because `DeliveredPurchase` is sent only if it can go out before order placed + 6.5 days. Later deliveries become a terminal `late_delivery` state and are never sent; a separate 7-day stale guard also exists. Placed time is deliberate: Meta's 7-day click window runs from the ad click to `event_time`, so delivery time would push many conversions out of the window.

---

## 1. Pre-flight checks (day 0 to 3)

**1.1 Event payload**
- [ ] `action_source = website`, with `client_user_agent` and `event_source_url` present on both events. Motahai captures the checkout user agent and IP (Shopify client_details), stores them encrypted until send (D-006), and sets `event_source_url` from the store URL. Website is also what keeps these events usable by Website-conversion-location Sales ad sets.
- [ ] Events lacking a user agent (Salla until a capture path exists) carry `missing_user_agent` in `capi_events.quality_flags`. Check in Events Manager diagnostics whether Meta accepts or discards them, and record the result. [Test]
- [ ] `event_name` is exactly `ConfirmedOrder` or `DeliveredPurchase` (case-sensitive; same spelling in the custom conversion rule).
- [ ] `event_time` = order placed time, and the sender enforces the design cutoff (`DeliveredPurchase` sent only before placed + 6.5 days; otherwise `late_delivery`, never sent) plus the 7-day stale guard. Check that no event in a batch is older than 7 days at send time.
- [ ] `event_id` is set and stable per order + event (dedupe window: 48 hours on same `event_name` and `event_id`). [Confirmed]
- [ ] `user_data` has at least one hashed identifier: `ph` (SHA-256 of digits with country code, no `+`, no leading zeros) and/or `em` (trimmed, lowercased, SHA-256). Add `external_id` (same value on every event, same format as in pixel if pixel is used) and `client_ip_address` / `client_user_agent` where the merchant captured them at checkout. [Confirmed]
- [ ] `custom_data` has `currency` (ISO 4217, e.g. `EGP`, `SAR`) and `value` (order total, or delivered net amount for `DeliveredPurchase`; decide one rule and write it down). Needed for value optimization. [Confirmed]
- [ ] Hashing check: a unit test normalizes `+20 100 123 4567`, `0020 100 123 4567`, and `201001234567` to the same digits (`201001234567`), and that the local form `01001234567` is converted to the country-coded form before hashing (a leading-zero strip alone gives `1001234567`, which is wrong). Also check the Meta sample: SHA-256 of `15559876543` is `1ef970831d7963307784fa8688e8fce101a15685d62aa765fed23f3a2c576a4e`.

**1.2 Test events**
- [ ] Send both events with `test_event_code` (value from Events Manager > Test Events).
- [ ] Both events appear in Test Events with the right name, value, currency, and matched identifiers.
- [ ] Note: Meta says test events "may get discarded if they don't match a Facebook or Meta account". Use a real test phone/email on a real account for at least one event per type. [Confirmed]
- [ ] Send one deliberately old event (older than 7 days) in a sandbox request and confirm the whole request is rejected. This proves the time rule before go-live. [Confirmed rule, reproduce]

**1.3 Match and quality**
- [ ] Open the Dataset Quality API (or the Events Manager data-quality view) for the dataset. Record Event Match Quality if shown. Meta documents EMQ for web events only, so expect no EMQ number for these server events. [Confirmed: "EMQ is currently available only for web events"] Record what is shown instead.
- [ ] Record match rate proxy: share of test events that show a matched user in Test Events. Target before live: all test events matched.
- [ ] Record whether events flagged `missing_user_agent` show a warning or are dropped in Events Manager diagnostics. [Test]
- [ ] Attribution check [Test]: send test events with `event_time` 3 to 6 days in the past. Confirm they are accepted and attributed to an ad click inside the 7-day window.

**1.4 Custom conversions**
- [ ] Create custom conversion `ConfirmedOrder` (source: dataset; rule: event name equals `ConfirmedOrder`; category: pick one, record it). [Test: category effect]
- [ ] Create custom conversion `DeliveredPurchase` the same way.
- [ ] Both show status Active after test events (and archive is not triggered; inactive conversions archive after 2 years per help summary).
- [ ] Record the category chosen for each. Do not create a second copy under a different category without writing it down, since rules cannot be edited later (help summary). [Inferred]

**1.5 Optimization options (screenshot everything)**
- [ ] Create a paused Sales / Website campaign draft. Open ad set > Conversion > Performance goal.
- [ ] Record whether `ConfirmedOrder` and `DeliveredPurchase` appear as conversion events. [Test Q2]
- [ ] Record whether "Maximize value of conversions" is offered on each. [Confirmed for custom events on Sales objective, check the UI]
- [ ] Record whether any minimum ROAS / ROAS goal option appears. [Not found in official docs, Test]
- [ ] If an event is not selectable: stop the design for that event. Do not edit the design to hide it. Go to the fallback in section 4.

**1.6 Pixel parity (only if pixel also sends events)**
- [ ] Same `external_id` value on pixel and CAPI for the same person.
- [ ] `event_name` and `event_id` consistent across browser and server. [Confirmed dedup rule]

Exit criterion for pre-flight: all of 1.1 to 1.5 are green or have a recorded fallback. Do not start the experiment with an untested event.

---

## 2. Experiment design

**2.1 Arms**
- Arm A (control): ad set optimizes for native `Purchase`.
- Arm B (test): ad set optimizes for custom conversion `ConfirmedOrder`.
- Same creatives, same audience, same placements, same schedule, same daily budget per ad set (or same total budget split 50/50).

**2.2 Setup method**
- Preferred: Meta A/B test (Experiments) with the optimization event as the tested variable. [Inferred: UI availability to confirm]
- Fallback: two duplicated ad sets under one campaign (or two campaigns with identical structure if the account blocks shared budgets). Name them `EXP-S2-A-Purchase` and `EXP-S2-B-ConfirmedOrder`.
- Do not add a third arm (DeliveredPurchase) in the same test. Keep arm count at two.

**2.3 Duration and sample**
- Minimum 14 days of spend. Reason: COD confirmation and delivery take days, and `DeliveredPurchase` lags behind spend.
- Minimum sample: at least 50 optimization events per arm per week, or the arm sits in Learning Limited. This is the learning-threshold figure from third-party sources and the Learning Limited summary (Inferred). If an arm is Learning Limited for more than 7 days, record it and stop the arm (see 2.6).
- Judge outcomes on orders placed in days 1 to 14, with a maturity window of 21 days after the last order date before final read (assumption: COD delivery plus settlement; confirm with the merchant's real cycle).

**2.4 Success metric (computed by Motahai, not Meta)**
- Primary: Delivered ROAS = (net collected cash from delivered orders attributed to the arm) / (ad spend for the arm, same window). Net collected = courier COD collected minus returns and refunds. Use the courier cash record (Bosta `cod` on Delivered state 45 where available) and Shopify transactions as a cross-check.
- Secondary: delivery rate = delivered orders / shipped orders (by arm). Confirmation rate = ConfirmedOrder / orders placed (by arm, from Motahai DB).
- Cost per delivered order = spend / delivered orders (by arm).
- Attribution: order carries the `utm_*` or ad id captured at checkout. Attribution rule must be fixed before the test and applied the same way to both arms.
- Meta-reported ROAS and Meta-reported purchases are shown for context only. Do not decide on them.

**2.5 Decision rule**
- Arm B wins if Delivered ROAS is higher by at least 15% and delivery rate is not more than 3 percentage points lower than arm A, with at least 30 delivered orders per arm. These thresholds are proposals; confirm with the merchant before start.
- If neither wins, keep arm A (native Purchase). Do not iterate mid-test.

**2.6 Guardrails**
- Do not edit the test mid-run. Significant edits (optimization event, audience, creative, pausing) reset learning (third-party and help summary, Inferred). Budget changes: keep under the agreed step (for example 10% per day) and log every change.
- Do not add or remove creatives during the test.
- Stop conditions (any one):
  - Arm is Learning Limited for more than 7 days.
  - Confirmation rate below 20% of orders for 3 consecutive days (signals a broken confirmation flow, not an ad problem).
  - Event send failures above 1% (check error logs and `event_time` age).
  - Match not visible in Test Events or Dataset Quality for more than 48 hours after a deploy change.
  - Merchant reports a COD fraud spike or a courier outage.
- If stopped early, record the reason and do not declare a winner.

**2.7 Logging**
- Keep a dated change log in the pilot folder: every budget, schedule, creative, or event-payload change, with who made it.
- Export daily: spend, impressions, Meta purchases, Meta ConfirmedOrder, orders placed, confirmed, shipped, delivered, net collected, by arm.

---

## 3. Rules: when to move an ad set to optimize on DeliveredPurchase

Move an ad set from `ConfirmedOrder` (or Purchase) to `DeliveredPurchase` only when ALL are true:
1. `DeliveredPurchase` has at least 50 events for that ad set in each of the last 2 full weeks. (The 50/week figure is the learning-threshold guide, Inferred.)
2. The ad set is not in Learning Limited, and it has been stable for 7 days with no significant edit.
3. Delivered-event volume is consistent with orders placed: the last 14 days of delivered data have matured (21-day window closed for the earliest cohort in the window).
4. Motahai's delivered ROAS for that ad set is at or above the arm A baseline in the pilot read-out (section 2.5).
5. The operator confirms the courier feed is current (no backlog of unposted deliveries more than 48 hours).

If `DeliveredPurchase` volume is below 50/week per ad set:
- Consolidate ad sets (fewer, larger ad sets) rather than optimizing on a thin event.
- Keep `ConfirmedOrder` as the optimization event.
- Re-check weekly. Do not switch on a single good week.

Do not switch the optimization event in the same week as a budget or creative change.

---

## 4. Fallbacks

- If `ConfirmedOrder` is not selectable as an optimization event: optimize on native `Purchase` with value (`value` in `custom_data` and Purchase), and use `ConfirmedOrder` only for reporting and offline-style tracking. Record this as a finding.
- If value optimization is missing on custom events: the test becomes a volume test (count optimization), and the ROAS comparison is done in Motahai only.
- If match quality is too low (no matched users in Test Events): stop sending the custom events and fix identifiers (hashed phone, external_id) before any spend decision.

---

## 5. Open [Test] questions the pilot must answer

1. Can `ConfirmedOrder` (server custom event, `action_source = website`) be selected as an ad-set optimization event for a Sales/Website campaign? Screenshot the dropdown.
2. What does the custom conversion category (`PURCHASE` vs `OTHER`) change, if anything, in optimization, dedup, and reporting?
3. Is "Maximize value of conversions" available on `ConfirmedOrder` and `DeliveredPurchase`? Is a minimum ROAS goal available?
4. Does Meta match server-only events on phone-only identifiers at a usable rate? What do Dataset Quality and Test Events show?
5. Are events lacking a user agent (flagged `missing_user_agent`) accepted or discarded by Meta? Are they used for optimization?
5b. Fallback experiment only: do non-website action sources (`other`, `phone_call`, `chat`) match phone-only identifiers and stay selectable for Website-conversion-location Sales ad sets? Risk: they may not be selectable for website ad sets, so run only if the website path fails.
5c. Are events with `event_time` 3 to 6 days in the past accepted and attributed? (Design cutoff is placed + 6.5 days.)
6. Is the learning threshold (about 50 optimization events per ad set per week) and the significant-edit list confirmed by the current Meta Help Center text?
7. What is the real match rate of the hashed, normalized, country-coded phone customer list (if the merchant uses one)?
8. Does the `orders/updated` webhook carry `tags` after the confirmation app adds its tag? Does the confirmation app's tag write back for every confirmation?
9. Salla (if pilot is on Salla): does `GET /webhooks/events` list `order.status.updated`? What is the `data` schema?
10. Courier: exact delivered state value and COD amount field for the pilot courier (Bosta state 45 and `cod`; OTO `delivered`; others not yet found).
11. Shopify: does `current_total_price` change after an order edit, not just a refund? Does marking a COD order paid create a SALE transaction?
12. Courier settlement report format and timing (for the net-collected figure in section 2.4).

Record the answer, the screenshot or API response, and the date for each. Update `docs/research/S2_lookups.md` with the result (do not overwrite the Confirmed/Inferred labels; add a new "Pilot result" line).
