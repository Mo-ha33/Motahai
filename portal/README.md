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
