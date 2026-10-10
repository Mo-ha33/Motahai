# Media Buyer Handbook: Motahai signal ladder (pilot, S2)

Date: 2026-10-10. Audience: the media buyer running the pilot merchant's Meta ads. Companion documents: `docs/pilot/S2-7_validation_plan.md` (the test plan, with the pre-flight checklist and decision rules) and `docs/research/S2_lookups.md` (what Meta and the platforms document).

Labels used below:
- **Confirmed**: an official page was read.
- **Inferred**: a third-party source or a partial read. Verify before relying on it.
- **[Test]**: not documented. Check it in the pilot and record the result.
- **Not found**: no official source located. Do not design on it.

---

## 0. What Motahai sends, and what it does not

- Motahai sends two custom server events: `ConfirmedOrder` and `DeliveredPurchase`.
- Motahai **never sends the standard `Purchase`**. The merchant's native Shopify or Salla Meta integration keeps sending `Purchase` at order creation. Leave it on. Do not duplicate it and do not turn it off during the pilot.
- Every event goes to the merchant's own Meta dataset with `action_source = website`.
- During the pilot the tenant runs in **shadow mode**. Shadow mode records each event that would have been sent (`capi_events.status = shadow`) and calls nothing. Nothing reaches Meta until the S2-7 pre-flight is green and the operator switches the tenant to live.

---

## 1. The ladder

| Event | When it fires | event_id | event_time | value | Use it for |
|---|---|---|---|---|---|
| `Purchase` (native) | Order created, sent by the merchant's own Shopify/Salla Meta integration | Set by that integration | Order creation | Order total | Baseline only. Motahai does not touch it. For COD it includes orders later refused. |
| `ConfirmedOrder` | The order is confirmed: a merchant tag (`confirmed`, `cod-confirmed`, `order-confirmed`, or Arabic `تم التأكيد` / `مؤكد`), a Salla status the merchant configured, the order shipped (implicit, Shopify and Salla), a prepaid order paid (implicit), or a manual confirmation from the call centre or WhatsApp bot. Never for orders still awaiting review (Salla `in_review`, `under_review`) or for cancelled orders. | `confirmed_<order_id>` | Confirmation time (the webhook's `updated_at`, or the time of the manual trigger). Sent immediately, no settlement wait. | Full order total (gross) | **Optimization signal.** Use it when `DeliveredPurchase` volume is too thin (section 4). |
| `DeliveredPurchase` | Cash collected and order delivered. Shopify COD: `paid` and `fulfilled` (the merchant marks the order paid when the cash is collected). Prepaid: `paid`. Salla: delivered status. Never for cancelled, refunded or voided orders. | `delivered_<order_id>` | **Order placed time** (`created_at_platform`). Not the delivery time. | **Net collected** = total minus successful refunds. A net value of 0 or less is not sent. | **Truth and ROAS.** Optimize on it only when volume allows (section 4). |

Events that are never sent:

| Action | Meaning |
|---|---|
| `late_delivery` | The delivery was recorded at or after placed + 6.5 days. Terminal. Never sent. |
| `stale` (`no_longer_eligible`) | At send time the order was cancelled, refunded or reduced to 0 value. Correct behavior. |
| `stale` (`event_time_out_of_window`) | The event is older than Meta's 7-day window. Never sent. |
| `SUPPRESSED` | Cancelled, refunded or voided at the webhook. No row is written for `DeliveredPurchase`. |

Timing rules:
- **Settlement window.** A `DeliveredPurchase` waits the tenant's `settlement_hours` (default **12 h**) after the order first shows delivered or paid, then the scheduler sends it. Its due time is never later than placed + 6.5 days minus 1 h.
- **Why placed time.** Meta rejects a whole request if any event in it is older than 7 days (**Confirmed**). Meta's 7-day click window runs from the ad click to `event_time`, so a delivery-time stamp would push many conversions out of the window. The 6.5-day cutoff keeps every send inside the 7-day limit.
- **Dedup.** Meta dedupes on the same `event_name` and `event_id` within 48 hours (**Confirmed**). Motahai's ids are deterministic, so a re-send does not double count.

---

## 2. Example CAPI payloads

These are the payloads the code builds (`MetaCAPISender.process_cod_order_event`, the same structure `build_event_payload` produces), with fake values. `<sha256>` stands for a 64-character lowercase hex SHA-256 digest. The sender adds `access_token` to the request body, never to the URL. It is not shown here.

**ConfirmedOrder** (`event_time` is the confirmation time, Unix seconds):

```json
{
  "data": [
    {
      "event_name": "ConfirmedOrder",
      "event_time": 1759312800,
      "event_id": "confirmed_SIM-1001",
      "action_source": "website",
      "user_data": {
        "em": ["<sha256>"],
        "ph": ["<sha256>"],
        "external_id": ["<sha256>"],
        "fn": ["<sha256>"],
        "ln": ["<sha256>"],
        "ct": ["<sha256>"],
        "country": ["<sha256>"],
        "client_ip_address": "203.0.113.10",
        "client_user_agent": "Mozilla/5.0 (SIMULATION)"
      },
      "custom_data": {
        "currency": "EGP",
        "value": 1250.0,
        "order_id": "SIM-1001"
      },
      "event_source_url": "https://sim-pilot.myshopify.com/"
    }
  ]
}
```

**DeliveredPurchase** (`event_time` is the placed time; `value` is the net collected amount):

```json
{
  "data": [
    {
      "event_name": "DeliveredPurchase",
      "event_time": 1759309200,
      "event_id": "delivered_SIM-1001",
      "action_source": "website",
      "user_data": {
        "em": ["<sha256>"],
        "ph": ["<sha256>"],
        "external_id": ["<sha256>"],
        "fn": ["<sha256>"],
        "ln": ["<sha256>"],
        "ct": ["<sha256>"],
        "country": ["<sha256>"],
        "client_ip_address": "203.0.113.10",
        "client_user_agent": "Mozilla/5.0 (SIMULATION)"
      },
      "custom_data": {
        "currency": "EGP",
        "value": 1250.0,
        "order_id": "SIM-1001"
      },
      "event_source_url": "https://sim-pilot.myshopify.com/"
    }
  ]
}
```

Field notes:
- `em` and `ph` are SHA-256 of the normalized value. Email is trimmed and lowercased. Phone is digits only, country code first, no `+`, no leading zero. A local Egyptian number `01xxxxxxxxx` becomes `201xxxxxxxxx` before hashing. Confirmed by Meta's sample: SHA-256 of `15559876543` is `1ef970831d7963307784fa8688e8fce101a15685d62aa765fed23f3a2c576a4e`.
- `external_id` is Motahai's own hashed customer key. It is the same on every event for that customer.
- `client_ip_address` and `client_user_agent` come from the checkout (D-006). They are encrypted at rest and purged after a successful send or 14 days.
- `custom_data.value` is the order total for `ConfirmedOrder` and the net collected amount for `DeliveredPurchase`. The two numbers are not comparable. See section 5.
- `test_event_code` is **not** set by the pipeline today. See the known gaps in section 9.

---

## 3. Custom conversion setup (Ads Manager)

Before you start: the pilot dataset must already show test events. Complete S2-7 section 1.2 first. Menu paths below are **[Test]**: confirm each one in the account and screenshot it into the change log.

Do this twice, once for each event. The steps are the same.

**A. `ConfirmedOrder`**
1. Open **Events Manager** and select the dataset that receives the merchant's Motahai events. **[Test]**
2. Open **Custom conversions** and choose **Create**. **[Test]**
3. Source: the dataset (not a URL rule).
4. Rule: **event name equals `ConfirmedOrder`**. The name is case-sensitive and must match the payload exactly.
5. Category: **Purchase** (the pilot standard). Write the choice in the change log. Rules cannot be edited after creation (Inferred, from the Meta help summary), so do not create a second copy under another category. The category's effect on optimization, deduplication and reporting is **[Test]** (S2-7 Q2). Record what you see.
6. Value: the events carry `custom_data.value` and `currency`. If the form offers a value column or "use event value", select it. **[Test]**: confirm the option name.
7. Save. Confirm the status shows **Active** after the test events arrive. Inactive conversions archive after two years (Inferred).

**B. `DeliveredPurchase`**: the same steps, with the rule **event name equals `DeliveredPurchase`**. Use the same category (Purchase) so the two are comparable. Record the category.

Ad set check (screenshot it): open the ad set's **Conversion / Performance goal** field and record:
- Whether `ConfirmedOrder` and `DeliveredPurchase` appear as conversion events (S2-7 Q1). **[Test]**
- Whether **Maximize value of conversions** is offered on each. Value optimization works on custom events on the Sales objective (**Confirmed**).
- Whether any minimum-ROAS goal appears. **Not found** in official docs. Do not promise a ROAS goal until the screenshot shows it.

If an event cannot be selected as an optimization event, stop and use the fallback in S2-7 section 4. Do not edit the design to hide the problem.

---

## 4. Optimization rule

**Optimize on the deepest event that gives each ad set about 50 optimization events per week.** The ladder runs from deepest to shallowest: `DeliveredPurchase`, then `ConfirmedOrder`, then native `Purchase`.

The 50-per-week figure is the learning-phase guidance from third-party sources (**Inferred**; Meta's help page could not be read). The rule is the same as S2-7 section 3. Move an ad set to `DeliveredPurchase` only when **all** are true:

1. `DeliveredPurchase` has at least 50 events for that ad set in each of the last two full weeks.
2. The ad set is not in **Learning Limited** and has been stable for 7 days with no significant edit.
3. The last 14 days of delivered data have matured (the 21-day window has closed for the earliest cohort).
4. Motahai's Delivered ROAS for the ad set is at or above the arm A baseline.
5. The courier feed is current: no unposted deliveries older than 48 hours.

If `DeliveredPurchase` volume stays below 50 a week per ad set, consolidate the ad sets (fewer, larger ones) and keep `ConfirmedOrder` as the optimization event. Re-check weekly. Do not switch on one good week. Do not switch in the same week as a budget or creative change.

**How to switch: duplicate, do not edit.** Changing the optimization event on a live ad set is a significant edit that resets learning (Inferred, from the Meta help summary). Instead:
1. Duplicate the ad set.
2. In the copy only, set the new optimization event. Keep the audience and creatives identical.
3. Name the copy with the date and the event (for example `2026-10-20 DeliveredPurchase`).
4. Pause the original only after the copy is reviewed. Log both steps.

Do not run three arms. The pilot test has two arms (section 7).

---

## 5. Custom metrics formulas (Ads Manager)

Build these as custom metrics, or export the columns and calculate them in a sheet. Whether custom metrics are available in the account is **[Test]**.

| Metric | Formula | Notes |
|---|---|---|
| Delivered ROAS | `DeliveredPurchase` conversion value ÷ amount spent | Value is the net collected amount. |
| Delivery rate | `DeliveredPurchase` count ÷ `Purchase` count | `Purchase` includes COD orders later refused, so this is a floor, not a true delivery rate. |
| Confirmation rate | `ConfirmedOrder` count ÷ `Purchase` count | Measures how much of the native signal the confirmation step keeps. |
| Vanity inflation | `Purchase` value ÷ `DeliveredPurchase` value | How much larger the native value is than the cash-backed value. Track the trend. |

**These are Meta-attributed numbers.** They will differ from Motahai's database. Reasons: Meta's attribution windows and modelled conversions, Meta's deduplication, Motahai's placed-time `event_time`, refunds after the event, and the net-collected rule. For any decision, use Motahai's database numbers (S2-7 section 2.4). Meta's ROAS and purchases are for context only.

---

## 6. URL parameter templates

Ads with URL parameters let the checkout capture attribution. Motahai stores `utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `ad_id`, `fbp`, `fbc`, `ttclid` and `sccid`. The first non-empty value for each field wins. Use the same template on every creative so the attribution lines up.

**Meta** (set in the ad's **URL parameters** field, without a leading `?`). Confirmed: the Meta page title "Specifications For URL Dynamic Parameters". The macro names are **Inferred** from secondary sources. Verify in Ads Manager before launch.

```
utm_source=facebook&utm_medium=paid&utm_campaign={{campaign.id}}&utm_content={{ad.id}}
```

Macros: `{{campaign.id}}`, `{{adset.id}}`, `{{ad.id}}`, `{{ad.name}}`, `{{placement}}`.

**TikTok.** Macro names are **Confirmed** from TikTok's UTM article. The UI field that takes them is **Not found**. Use the article's URL builder and check the result in a test ad.

```
utm_source=tiktok&utm_medium=paid&utm_campaign=__CAMPAIGN_ID__&utm_content=__CID__&utm_term=__AID__
```

- `__CID__` is the creative ID and `__AID__` is the ad group ID.
- For Smart+ upgraded campaigns, use `__ADID_V2__` (ad ID) in place of `__CID__`.
- `ttclid` is captured from the TikTok click automatically. Do not add it to the template.

---

## 7. A/B test template (S2-7)

**Question:** does optimizing on `ConfirmedOrder` produce more cash-backed revenue per unit of spend than optimizing on native `Purchase`?

| Item | Setting |
|---|---|
| Arm A (control) | Ad set optimizes on native `Purchase` |
| Arm B (test) | Ad set optimizes on custom conversion `ConfirmedOrder` |
| Held equal | Creatives, audience, placements, schedule, daily budget (or a 50/50 split of one total budget) |
| Setup | Preferred: Meta A/B test with the optimization event as the tested variable (**[Test]**: the UI option). Fallback: two duplicated ad sets under one campaign, named `EXP-S2-A-Purchase` and `EXP-S2-B-ConfirmedOrder`. |
| Arm count | Two. Do not add `DeliveredPurchase` as a third arm. |
| Duration | At least 14 days of spend. |
| Reading window | Orders placed in days 1 to 14, read after a 21-day maturity window from the last order date. |
| Minimum sample | At least 50 optimization events per arm per week. An arm in Learning Limited for more than 7 days is stopped and recorded. |
| **Success metric** | **Delivered ROAS from Motahai's database**: net collected cash from delivered orders attributed to the arm, divided by that arm's spend over the same window. |
| Secondary | Delivery rate (delivered ÷ shipped), confirmation rate (`ConfirmedOrder` ÷ orders placed), cost per delivered order. All from Motahai's database, by arm. |
| Attribution | Fixed before the test and applied the same way to both arms. |
| **Decision rule** (proposal; confirm with the merchant before start) | B wins if Delivered ROAS is at least 15% higher than A **and** delivery rate is at most 3 percentage points lower than A, with at least 30 delivered orders per arm. Otherwise keep A. Do not iterate mid-test. |

**Guardrails**
- No edits mid-run. Changing the optimization event, audience, creative or pausing resets learning. Budget changes stay under the agreed step (for example 10% a day). Log every change with who made it.
- No creatives added or removed during the test.
- Stop conditions (any one): an arm is Learning Limited for more than 7 days; confirmation rate is below 20% of orders for 3 consecutive days (a broken confirmation flow, not an ad problem); event send failures are above 1%; no match is visible in Test Events or Dataset Quality for more than 48 hours after a deploy change; the merchant reports a COD fraud spike or a courier outage.
- If stopped early, record the reason and do not declare a winner.

**Logging.** Keep a dated change log in the pilot folder. Export daily, by arm: spend, impressions, Meta `Purchase`, Meta `ConfirmedOrder`, orders placed, confirmed, shipped, delivered, net collected.

---

## 8. Refuser exclusion and delivered-buyer lookalike (per merchant only)

Motahai builds two hashed customer lists per tenant from `src/ameen_workforce/audiences.py`:

```python
from src.ameen_workforce.audiences import export_tenant_audiences
export_tenant_audiences(session, tenant_id, "out/audiences")
```

This writes two files into the output folder:

| File | Contents | Rule |
|---|---|---|
| `<tenant_id>_exclude_refusers.csv` | Columns `phone,email`. Customers with at least 2 refused COD orders in the last 180 days. | A refused order is a COD order that was shipped and then cancelled or refunded. |
| `<tenant_id>_seed_delivered_buyers.csv` | Columns `phone,email,value`. Customers with a delivered order in the last 365 days, excluding anyone with any refusal in the last 180 days. | Delivered means the order is `delivered`, or a `DeliveredPurchase` was sent or shadowed. |

Both files hold SHA-256 hashes only (lowercase hex). No names, addresses or raw phone numbers. Meta expects hashed input for customer lists (**Confirmed**).

**Rules**
- **One merchant, one file.** Never merge two merchants' lists, and never upload a merchant's customers into another merchant's ad account.
- **Value column.** Meta's official customer-file schema has **no value key** (**Not found**). Do not rely on the `value` column in the seed file. Upload it as a phone and email list. Treat any value-based audience as **[Test]** until it is verified.
- **Minimum size.** The export warns below 100 rows. That is Motahai's threshold, not a Meta rule. Meta's minimum for customer lists is **Not found**. Check the audience size Meta reports before you use the list.
- **Match rate** of the normalized, hashed, country-coded phone list is pilot question 7 (S2-7 section 5). Record it.

**Upload (Ads Manager)** **[Test]**: confirm the menu path. Audiences, then Create audience, then Custom audience, then Customer list. Choose the merchant's ad account, upload the CSV, and map `phone` to phone and `email` to email. Confirm Ads Manager accepts pre-hashed input without hashing it again.

**Use**
- **Exclusion:** add the refuser audience as an exclusion on prospecting ad sets, so the budget does not go to customers who have refused twice.
- **Lookalike seed:** build a lookalike from the delivered-buyers audience in the same ad account. Meta's help summary says a larger source is better and gives no hard minimum (**Inferred**).
- Re-export when the list is reviewed. Allow time for the upload to process, then check the audience size before you rely on it.

---

## 9. Troubleshooting

**Events missing in Test Events**
1. **Check the status first.** Open `capi_events` for the order. `shadow` is expected during the pilot. Nothing reaches Meta, so Test Events shows nothing for a shadow tenant. This is by design.
2. `failed` with an `error_type` tells you why:
   - `missing_dataset_id`: the tenant has no Meta dataset ID.
   - `missing_credential`: the Meta token is missing or cannot be read.
   - `meta_api_error`: check `http_code`. Read the Meta response in Events Manager. The error text is not logged, on purpose.
   - Any other value: an exception type name. Check the logs.
   `retry_failed_events` resends failed rows up to `max_attempts` (default 5). Credential and dataset errors do not use up attempts, so they resume once the tenant is fixed.
3. **No matched user.** Check that `user_data` has a hashed `em` or `ph`. Re-check phone normalization (section 2). A phone without the country code will not match.
4. **Not sent by design.** `late_delivery`, `stale` and `SUPPRESSED` are correct behavior, not bugs (section 1).
5. **Known gap: `test_event_code`.** The pipeline never sets `test_event_code`. See section 10.

**`missing_user_agent` quality flag** (on `capi_events.quality_flags`)
- Means the event has no `client_user_agent`. Causes: the platform payload has no `client_details` (Shopify), Salla has no capture path yet, or the checkout context was purged or expired (14 days, D-006).
- Meta documents `client_user_agent` as required for website events (**Confirmed**). What Meta does with an event that lacks it is **[Test]**: S2-7 section 1.3 checks the Events Manager diagnostics. Record the result.
- Do not fill the gap with a made-up value. If one platform shows a high share of this flag, fix the capture path. That is not a campaign problem.

**High `late_delivery` share**
- Means `DeliveredPurchase` was not sent because delivery was recorded at or after placed + 6.5 days.
- Likely causes: courier lag (the webhook is late or backlogged), the merchant marking cash collected late, or a feed backlog.
- Check: the gap between `created_at_platform` and `delivered_at` for the late orders, and the courier feed backlog. S2-7 rule 5 blocks switching above 48 hours of backlog.
- Effect: those orders never reach Meta. Delivered ROAS is understated and `DeliveredPurchase` volume is thin. Do not read it as a campaign result.

**Low confirmation rate** (below 20% for 3 days is a stop condition)
- Check that the confirmation tag reaches Motahai. Whether the Shopify `orders/updated` payload carries `tags` after the confirmation app writes its tag is **[Test]** (S2-7 Q8). Check with a test order.
- For Salla, check that the merchant's confirmed status is configured. No Salla status confirms by default.

---

## 10. Known gaps (as of 2026-10-10)

- **`test_event_code` is not wired into the pipeline.** `order_pipeline` and `capi_service` accept the parameter, but the pipeline never passes it. Only the connection test in `scripts/onboard_store.py` sets one. Events sent by the pipeline will not show in Test Events and will count in the live dataset. Until this is fixed, do not run a Test Events check on a dataset that feeds live campaigns. Use a dedicated test dataset for that check. This needs an engineering fix. It is not a media-buyer setting.
- **Customer-list value key** is not in Meta's official schema (see section 8).
- **Shopify `tags` in `orders/updated`** is unverified (S2-7 Q8).
- **Category effect** of a Purchase-category custom conversion is unknown (S2-7 Q2).
- **Maximize value and ROAS goals** for custom events are only partly confirmed (S2-7 Q3).
- **Learning threshold and significant-edit list** come from third-party sources and need Meta's current help text (S2-7 Q6).

Record every answer, with the screenshot or API response and the date, in `docs/research/S2_lookups.md` as a new "Pilot result" line. Do not overwrite the original labels.
