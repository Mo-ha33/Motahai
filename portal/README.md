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
src/dashboard/            StatsProvider (one summary fetch per tenant + window), DashboardPage
src/api/                  typed Stats API client, pluggable auth, one shared client instance
src/i18n.ts               typed ar (primary) / en dictionaries; a missing key fails tsc
src/theme.ts, tokens.css  theme mode and design tokens
```

Data flow: `FiltersProvider` (tenant + range, persisted in localStorage) -> `StatsProvider` calls
`GET /v1/tenants/{id}/stats/summary?start&end` -> `DashboardPage` renders the page's widgets in a
12-column grid (`span` 3/4/6/8/12; full width under 720px). No mock data ships in `src/`; fixtures
live in `src/test/fixtures` and `*.test.*` only.

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

The browser never reads a key, token or env var. Requests are same-origin (`sameOriginAuth`).

- Dev (`npm run dev` / `npm run preview`): Vite proxies `/v1` to `MOTAHAI_API_URL`
  (default `http://127.0.0.1:8000`) and, when `OPERATOR_API_KEY` is set in the shell that starts
  Vite, adds `Authorization: Bearer ...` server-side in the proxy. The key is not `VITE_`-prefixed
  and never `define`d, so it cannot reach the bundle:
  `OPERATOR_API_KEY=... npm run dev`.
- Served build at `/app`: no proxy adds a key, so stats calls return 401 (shown as "the API proxy has
  no operator key configured") until per-tenant keys (#43) land. Then swap `sameOriginAuth` for
  `bearerAuth(getToken)` in `src/api/index.ts` and drop the tenant input in favour of the tenant
  derived from the key.
