# Tariq client playbook (Arabic)

Source of truth: `scripts/build_tariq_payload.py` (`client_messages`). This file mirrors it;
`tests/test_tariq_payload.py` fails if the texts drift. Edit the script, then update this file.

Placeholders in `{braces}` are filled by Tariq before sending. The service account email is
`tariq-gtm-agent@agentic-ai-494313.iam.gserviceaccount.com`.

## Flow at a glance

| Step | Trigger | Message |
|---|---|---|
| Intake | New client onboarding starts | M1 |
| Handshake OK | `hermes_gtm_verify_access` returns `CONNECTED` | M2 |
| Client-side fix | client code: `NOT_INVITED`, `PERMISSION_MISSING`, `CONTAINER_NOT_FOUND`, `INVALID_ID` (also publish-time `PERMISSION_MISSING`) | M3 (then re-verify on any client reply, max 3 automatic tries) |
| Workspace limit | client code `WORKSPACE_LIMIT` | M3-ws (then re-run on any client reply) |
| Our-side issue | any operator code (`SCOPE_INSUFFICIENT`, `API_DISABLED`, `RETRYABLE`, `UNKNOWN`, `CREDENTIALS_PENDING`, `LINT_FAILED`, `LINTER_UNAVAILABLE`, `INVALID_SPEC`, `NO_WORKSPACE`, `COMPILER_ERROR`, `MERGE_CONFLICT`) | M3-op (and escalate to the operator) |
| COD intake (W6, flag-on build only) | W5 done and platform is shopify or salla | M6-1 |
| Shadow mode notice (W6, flag-on build only) | operator onboarding done (`onboarded` and `webhooks_verified` true) | M6-2 |
| Media buyer note (W6, flag-on build only) | W6 promotion step | M6-3 |

---

## M1 - Intake and service-account invite

**When sent:** first contact, start of workflow W1. It asks for the four assets and the GTM invite in one message.

**English gloss:** Welcome to Motahai. To set up tracking we need four items: store URL and platform,
GTM container ID (top bar of the workspace, starts with GTM-), Meta pixel/dataset ID (Events Manager, Data
sources), GA4 Measurement ID (Admin, Data streams, Web stream, starts with G-). Then invite our service account:
GTM, Admin, Container column User Management, "+", Add users, paste the email, container permission Publish,
Invite, Save. No password, login link or screen share is ever needed, and access can be revoked any time by
removing the user. After pressing Save, send the IDs here and we verify the connection automatically.

````text
أهلاً بك في فريق مُتاح AI، يسعدنا خدمتك.

لنبدأ تجهيز التتبع لمتجرك، نحتاج منك أربع بيانات:
1. رابط المتجر والمنصة (شوبيفاي / سلة / زد / ووكومرس).
2. معرّف حاوية GTM: يظهر في الشريط العلوي لمساحة العمل ويبدأ بـ GTM-.
3. معرّف بكسل / مجموعة بيانات ميتا: Events Manager ← Data sources ← معرّف مجموعة البيانات.
4. معرّف قياس GA4: Admin ← Data streams ← Web stream ← Measurement ID، ويبدأ بـ G-.

ثم ادعُ حساب الخدمة الخاص بنا إلى الحاوية:
1. افتح حاويتك في Google Tag Manager.
2. اضغط Admin، ومن عمود Container اختر User Management.
3. اضغط علامة + ثم Add users.
4. الصق هذا البريد: `tariq-gtm-agent@agentic-ai-494313.iam.gserviceaccount.com`
5. في صلاحيات الحاوية (Container permissions) اختر Publish.
6. اضغط Invite ثم Save.

لا نحتاج كلمة مرور ولا رابط دخول ولا مشاركة شاشة أبداً، ويمكنك إلغاء الوصول في أي وقت بإزالة المستخدم.

بعد الضغط على Save أرسل لنا المعرّفات هنا، وسنتحقق من الاتصال تلقائياً.
````

---

## M2 - Connection confirmed

**When sent:** immediately after `hermes_gtm_verify_access` returns `CONNECTED` (that check is read-only, so the message does not claim Publish permission). Fill `{container_name}`,
`{public_id}`.

**English gloss:** Connected successfully. Container: {container_name} ({public_id}), status: connected. Next: we build in a draft workspace (nothing changes on your store yet), run an internal
audit, and publish only after our team approves. We will update you within 24 working hours.

````text
تم الاتصال بنجاح.

الحاوية: {container_name} ({public_id})
الحالة: متصلة

الخطوات القادمة:
1. نبني التتبع في مساحة عمل مسودة، ولا يتغير شيء على متجرك في هذه المرحلة.
2. نجري تدقيقاً داخلياً للإعدادات والأكواد.
3. لا ننشر إلا بعد موافقة فريقنا.

سنوافيك بالتحديث خلال 24 ساعة عمل.
````

---

## M3 - Permission recovery (client action needed)

**When sent:** verify returned a client-audience code (`NOT_INVITED`, `PERMISSION_MISSING`, `CONTAINER_NOT_FOUND`,
`INVALID_ID`), or `hermes_gtm_cloud_publish` returned `PERMISSION_MISSING` (Edit/Approve granted but not Publish; then
Tariq re-verifies on reply and requests a NEW D-003 token, never reusing the spent one). `{missing_permission}`
defaults to `Publish`. If the tool returns `client_action_ar`, it replaces the numbered fix steps. After the client replies (any
reply), Tariq re-verifies automatically; after 3 failed automatic re-verifications it escalates to the operator.

**English gloss:** Thanks, the connection is nearly complete, one small step left. The service account
currently lacks {missing_permission} permission on container {public_id}. Fix: in GTM, Admin, User
Management in the Container column; if the service account is not listed press + then Add users and paste it,
otherwise click it; set the container permission to {missing_permission}; press Save. Send us any message afterwards and we re-check automatically.

````text
شكراً لك، الاتصال قريب من الاكتمال وتبقّت خطوة بسيطة.

حساب الخدمة لا يملك حالياً صلاحية {missing_permission} على الحاوية {public_id}.

لتصحيح ذلك:
1. في GTM اختر Admin ثم User Management في عمود Container.
2. إن لم يظهر `tariq-gtm-agent@agentic-ai-494313.iam.gserviceaccount.com` في القائمة فاضغط + ثم Add users والصقه، وإلا فاضغط عليه.
3. اضبط صلاحية الحاوية على {missing_permission}، ثم اضغط Save.

بعد الحفظ أرسل لنا أي رسالة وسنعيد التحقق تلقائياً.
````

---

## M3-ws - Workspace limit reached

**When sent:** the tool returned `WORKSPACE_LIMIT` (the container has hit GTM's workspace cap, so Tariq cannot
create its dedicated "Tariq - {public_id}" workspace). Fill `{public_id}`.

**English gloss:** Thanks, one small step left: container {public_id} reached GTM's workspace limit. From the
Workspaces list, publish or delete one unused workspace, then send us any message and we continue automatically.

````text
شكراً لك، تبقّت خطوة بسيطة: وصلت الحاوية {public_id} إلى الحد الأقصى لمساحات العمل في GTM.

من قائمة Workspaces، انشر أو احذف مساحة عمل واحدة غير مستخدمة، ثم أرسل لنا أي رسالة وسنكمل تلقائياً.
````

---

## M3-op - Our-side issue (no client action)

**When sent:** verify returned an operator-audience code (for example `SCOPE_INSUFFICIENT`, `CREDENTIALS_PENDING`,
`API_DISABLED`, `UNKNOWN`). Tariq also escalates to the operator via `hermes_consult`. No blame, no jargon.

**English gloss:** Thanks, we received your details. We are finalizing the connection on our side and you do
not need to do anything. We will update you as soon as it is done.

````text
شكراً لك، وصلتنا بياناتك. نقوم حالياً بإنهاء الاتصال من جانبنا ولا يلزمك أي إجراء. سنوافيك فور الانتهاء.
````

---

## M6-1 - COD intake (W6 step a)

**When sent:** once, after W5 is complete for a shopify or salla store. It asks for the missing COD details in one message (W6 asks only for items not already known).

**English gloss:** Thanks. To activate COD (cash on delivery) signal tracking we need: the rough share of COD orders, the couriers you use (Bosta, Oto, Torod, Aramex or other), typical delivery days, and whether Meta's built-in Shopify/Salla integration is on. Send what you have here; this is all we need for now.

````text
شكراً لك. لتفعيل قياس طلبات الدفع عند الاستلام (COD) نحتاج منك هذه المعلومات:
1. نسبة الطلبات التي تُدفع عند الاستلام تقريباً.
2. شركات الشحن التي تستخدمها (بوسطة، أوتو، تورود، أرامكس، أو غيرها).
3. متوسط مدة التوصيل بالأيام.
4. هل تكامل ميتا المدمج في شوبيفاي أو سلة مفعّل الآن؟

أرسل ما يتوفر لديك هنا، وهذا كل ما نحتاجه الآن.
````

---

## M6-2 - Shadow mode notice (W6 step d)

**When sent:** after the operator onboarding is confirmed (`hermes_motahai_tenant_status` shows `onboarded` and `webhooks_verified` true).

**English gloss:** Signal tracking is set up in monitoring mode for 7 days. Your current ads do not change, and nothing is sent to Meta yet. After 7 days we send you the actual numbers and agree the next step together.

````text
تم تجهيز قياس الطلبات بوضع المراقبة لمدة 7 أيام.

خلال هذه المدة:
1. لا يتغير شيء في إعلاناتك الحالية.
2. لا نرسل أي بيانات إلى ميتا بعد.
3. نسجّل الطلبات والتأكيدات والتسليم لقياس الأرقام الفعلية فقط.

بعد 7 أيام نرسل لك الأرقام الفعلية، ثم نحدد الخطوة التالية معك.
````

---

## M6-3 - Media buyer note (W6 step g)

**When sent:** to the store's media buyer, at the W6 promotion step.

**English gloss:** Note for the media team: keep the native Purchase event untouched. Create a Custom Conversion on ConfirmedOrder and use it as an optimization event only in a new ad set, and only if it reaches about 50 or more events in 7 days; otherwise the campaign stays in Learning Limited. DeliveredPurchase is for measurement and reporting only, not the first optimization target. We measure actual numbers and make no promises of specific results.

````text
ملاحظة لفريق الإعلانات:
1. أبقِ حدث الشراء الأصلي (Purchase) كما هو دون تغيير.
2. أنشئ تحويلاً مخصصاً (Custom Conversion) على حدث ConfirmedOrder، ولا تستخدمه كهدف تحسين إلا في مجموعة إعلانات جديدة، وذلك إذا وصل إلى حوالي 50 حدثاً أو أكثر خلال 7 أيام. إذا كان العدد أقل، فالأفضل الانتظار حتى لا تبقى مجموعة الإعلانات في حالة Learning Limited.
3. حدث DeliveredPurchase للقياس والتقارير فقط، وليس هدف التحسين الأول.

نقيس الأرقام الفعلية فقط، ولا نعد بنتائج محددة.
````

---

## Conversation rules & intent routing (S3-9 / G2)

1. **No tool result -> no claim:** Never state a container, store or signal status that a tool did not return in this conversation. If a tool fails, state it plainly, retry only where the routing note allows, then escalate via `hermes_consult`. Never simulate tool output.
2. **One store per conversation:** `tenant_key` / GTM ID are fixed at W1. Any request about a different store -> do not call tools for it; ask the operator via `hermes_consult`.
3. **Resume, don't restart:** When a returning client writes, call the status tool for their stage (`hermes_gtm_verify_access` for W2-W5; with the COD flag, `hermes_motahai_tenant_status` for W6) and continue from the right step; never repeat W1 intake for assets already received.
4. **Waiting message:** Before a call that can take over ~30 s (`hermes_run_task`, `hermes_gtm_workspace_apply`, `hermes_gtm_cloud_publish`), send one short Arabic line saying what is being checked; no silent waits, no fake progress.
5. **Language and pace:** Mirror the client's language/dialect; one question per message; no internal tool names, IDs or English jargon to clients.
6. **Claims list:** Never quote industry return/cancellation rates or "saved budget"; only this store's measured numbers. Google Ads delivered-revenue sync is NOT available - never offer it.
7. **Intent -> workflow map:**
   - `"تجهيز التتبع"`, `"ربط GTM"`, `"tracking setup"`, `"GTM"` -> `W1_INTAKE`
   - `"الطلبات المرتجعة"`, `"مبيعات وهمية"`, `"الدفع عند الاستلام"`, `"COD"` -> `W6_COD_SIGNAL_ACTIVATION` (flag on) else `escalate via hermes_consult`
   - `"هل التتبع شغال"`, `"فحص التتبع"`, `"health"`, `"tracking status"` -> `W7_SIGNAL_HEALTH_CHECK` (flag on) else `W5_VERIFY`

