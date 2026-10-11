# Motahai merchant portal

Vite + React 18 + TypeScript app shell. The backend serves the built output at `/app`
(`src/ameen_workforce/service.py`) only when `portal/dist` exists at startup.

## Commands

Run from `portal/`:

- `npm install` installs dependencies (commit `package-lock.json`).
- `npm run dev` starts the Vite dev server.
- `npm run build` type-checks and writes the production build to `dist/`.
- `npm test` runs the vitest suite (jsdom + Testing Library).
- `npm run typecheck` runs `tsc --noEmit` only.

The Python test for the `/app` mount lives at `tests/test_portal_mount.py` and runs with
`python3 -m pytest -q` from the repo root.

## Architecture: config -> registries -> widgets

Everything a product person may want to tweak lives in typed config or CSS tokens, not in components.

```
src/config/dashboard.ts   pages: filters + ordered widget instances (type, titleKey, span, options)
src/config/metrics.ts     metric registry: id, label/hint i18n keys, format, select(summary)
src/widgets/              widget components, registered by type in widgets/index.ts
src/filters/              filter cards (tenant, date-range), registered in filters/registry.ts
src/dashboard/            StatsProvider (parallel fetch of the page's sources per tenant + window), DashboardPage
src/api/                  typed Stats API client, pluggable auth, one shared client instance
src/i18n.ts               typed ar (primary) / en dictionaries; a missing key fails tsc
src/theme.ts, tokens.css  theme mode and design tokens
```

Data flow: `FiltersProvider` (tenant + range, persisted in localStorage) -> `StatsProvider` calls
`GET /v1/tenants/{id}/stats/summary?start&end` -> `DashboardPage` renders the page's widgets in a
12-column grid (`span` 3/4/6/8/12; full width under 720px). No mock data ships in `src/`; fixtures
live in `src/test/fixtures` and `*.test.*` only.

### Data sources
A page declares `sources` (default `['summary']`) and each widget instance a `source` (default
`'summary'`). `StatsProvider` fetches the page's sources in parallel per tenant + window under one abort
controller and keeps a state per source, so a failing source shows an error card only in the widgets it
feeds (a page whose sources all fail shows the single page-level error card). A widget receives
`data`/`envelope` of its own source (`WidgetProps<Options, DataShape>`). Current sources: `summary`
(`/stats/summary`) and `health-score` (`/stats/health-score`, used by the `health-score` widget on the
Signal page).

To add a source: add its id to `StatsSourceId` and its data type to `SourceDataMap` in `src/api/types.ts`,
a method on `StatsClient` in `src/api/client.ts`, one entry in `FETCHERS` in
`src/dashboard/StatsProvider.tsx`, then list it in the page's `sources` and set `source` on its widgets.

### Add a metric
1. Add its id to `METRIC_IDS` and a `MetricDef` in `src/config/metrics.ts` (`select` returns
   `null` for ratios with a zero denominator).
2. Add its label (and optional hint) to `metrics` / `metricHints` in both `ar` and `en` in `src/i18n.ts`.
3. Reference the id from any widget `options` in `src/config/dashboard.ts`.

### Add a widget
1. Create `src/widgets/<name>.tsx` taking `WidgetProps<Options>`; wrap output in `WidgetFrame`.
2. Register it in `src/widgets/index.ts` with `registerWidget('<type>', Component)`.
3. Use it in `defaultDashboard`. Unknown types render a visible error card instead of crashing.

### Add a filter
1. Add the id to `FILTER_IDS` in `src/filters/ids.ts`, build a card component, register it in
   `src/filters/registry.ts`, keep its state in `FiltersContext`, then list it in a page's `filters`.

### Add a page
Add the route to `ROUTES` in `src/router.ts`, nav/page titles in `src/i18n.ts`, and a `PageConfig`
under `pages` in `src/config/dashboard.ts`. Routes without a page config show the honest empty state.

### Theme
Light, dark and auto (OS) modes: the header toggle sets `data-theme` and is persisted. Colours,
chart series (`--chart-1..4`), spacing and `--grid-gap` are tokens in `src/tokens.css`; widget CSS
uses tokens only and logical properties so RTL mirrors. Override at runtime with
`applyTokenOverrides({ '--color-accent': '#c2410c' })` from `src/theme.ts`.

## Auth model

The portal opens on a sign-in screen with two modes (`src/auth/`):

- **Tenant mode** (production): the merchant pastes a tenant API key (`mtk_` + 8 hex + `_` + secret,
  scope `stats:read`). The format is checked client-side, then `GET /v1/tenant/me` validates it; a 401
  shows "key not valid" and nothing is stored. On success the key and profile are kept in
  `sessionStorage` only (gone when the tab closes; never `localStorage`, never logged, never in a
  URL). Every API call then sends `Authorization: Bearer <key>` (`bearerAuth(() => session.key)`),
  the tenant comes from the profile (the tenant filter is hidden), and money uses the tenant's
  `currency` from the stats envelope. Any 401 from a stats call signs out and shows "session expired".
  The backend answers 404 for another tenant's id and the same 401 for any bad key.
- **Operator mode** (local dev, "Operator mode (local proxy)"): no browser credential
  (`sameOriginAuth`); Vite proxies `/v1` to `MOTAHAI_API_URL` (default `http://127.0.0.1:8000`) and,
  when `OPERATOR_API_KEY` is set in the shell that starts Vite, adds `Authorization: Bearer ...`
  server-side. A browser-supplied `Authorization` header (tenant mode) is never overwritten. The key
  is not `VITE_`-prefixed and never `define`d, so it cannot reach the bundle:
  `OPERATOR_API_KEY=... npm run dev`. The tenant is chosen with the tenant filter. The served build at
  `/app` has no proxy, so operator mode returns 401 there; use a tenant key.

`ApiProvider` (`src/dashboard/ApiContext.tsx`) builds the app-wide client from the session; tests can
inject `fetchImpl` via `<App fetchImpl={...} />`.

## Onboarding wizard (operator only)

The `#/onboarding` route is a five-step wizard over `/v1/operator/onboarding/*` (`src/api/onboarding.ts`,
`src/onboarding/`): create tenant (or continue one by id), Meta dataset + CAPI token, Bosta/OTO webhook secrets,
first API key, checklist. Tenant-mode sessions see an "operator only" card and make no calls; the client always uses
same-origin auth (the dev proxy adds the operator key).

Secrets handling:

- The CAPI token is a `type=password`, `autocomplete=off` input, cleared from component state as soon as the request
  is issued; it is never echoed back, shown or stored.
- Courier secrets/URLs and the first API key come back from the server exactly once. They are shown in a "copy now"
  panel (clipboard API, with select-text fallback) and live only in component state; leaving the step or the page
  drops them. They are never written to any storage and never logged.
- The only thing persisted is the numeric tenant id in `sessionStorage` (`motahai.onboarding.tenant`), so a reload
  resumes at the checklist.

Switching a tenant to live is deliberately not offered; the checklist's `ready_for_live` is informational and the
switch stays a separate operator action. Step fields are config-driven (`src/onboarding/fields.ts`); copy lives
under `onboarding` in `src/i18n.ts` (ar + en).

## Audiences page (M4-5, #52)

Route `#/audiences`. Shows the two hashed Meta customer lists (`exclude_refusers`, `seed_delivered_buyers`) with row
count, last-updated time and a warning when a list is under the API's `min_list_rows` (Meta may not use small
audiences), plus the weekly export schedule (last run, ok/failed, next due). Backed by
`/v1/tenants/{id}/audiences` (status), `/{name}.csv` (download) and `POST /export` (operator only).

- The tenant comes from the session (tenant mode) or the tenant filter card (operator mode).
- Downloads go through the authenticated client (`fetch` -> Blob -> temporary object URL, revoked right after the
  click) because a plain link cannot carry the bearer token. Hashes are never rendered on the page.
- A tenant key needs the `audiences:read` scope, which an operator must grant explicitly (new keys default to
  `stats:read`). A 401 on this page in tenant mode shows an "ask your operator to grant audience access" message and
  does NOT sign out, since the key is still valid for stats.
- "Export now" is shown in operator mode only (a tenant key gets 403 from the API).
