# Web Frontend Guide

Reference for the web interface. Source code lives in [ratatoskr-web](https://github.com/po4yka/ratatoskr-web).

**Audience:** Frontend developers, integrators, operators **Type:** Reference **Related:** [Mobile API Spec](mobile-api.md), [Deployment Guide](../guides/deploy-production.md)

> **Source repository:** The frontend source code lives in [ratatoskr-web](https://github.com/po4yka/ratatoskr-web). This document covers deployment integration, auth contracts, and API surface from the backend perspective.

---

## Overview

The web interface is the sole frontend surface — a standalone SPA built with:

- React 19 + TypeScript + Vite
- The Frost design system under `src/design/` (project-owned, brutalist; see `DESIGN.md`)
- `@tanstack/react-query` for server state and polling

Built with `npm run build` in the ratatoskr-web repo (output: `dist/`); `dist/` is then placed into the backend's `app/static/web/` for FastAPI to serve on `/web` and `/web/*`.

### Design system — Frost

`src/design/` exports the Frost design system: editorial monospace minimalism with a two-color rule (ink + page) and a single critical accent (spark). See `DESIGN.md` at the repo root for the canonical spec.

Frost primitives: `BracketButton`, `BracketSearch`, `BrutalistCard`, `BrutalistSkeleton` (+ Text/Placeholder/DataTable variants), `MonoInput`, `MonoTextArea`, `MonoSelect` (+ `MonoSelectItem`), `MonoProgressBar`, `SparkLoading`, `StatusBadge`, `Toast`, `Tag`, `Link`, `IconButton`, `Toggle`, `Checkbox`, `RadioButton`, `Accordion`, `NumberInput`, `UnorderedList`, `CodeSnippet`, `FileUploader`.

Navigation: `BracketTabs` (+ `BracketTabList`/`BracketTab`/ `BracketTabPanels`/`BracketTabPanel`), `BracketPagination`, `TreeView`, `ContentSwitcher`.

Table: `BrutalistTable` (+ `BrutalistTableContainer`) for the high-level render-props API. Lower-level `Table`/`TableHead`/`TableBody`/ `TableRow`/`TableCell`/`TableHeader` primitives compose with it.

Modal: `BrutalistModal` (+ `BrutalistModalHeader`/ `BrutalistModalBody`/`BrutalistModalFooter`).

Shell: `FrostHeader` family + `FrostSideNav` family, `Content` and `Theme` wrappers.

Multiselect / pickers: `MultiSelect`, `FilterableMultiSelect`, `Dropdown`, `DatePicker`, `TimePicker`.

Feature code imports exclusively from `../design`. Tokens (`tokens.css`), fonts (`fonts.css`, self-hosted JetBrains Mono + Source Serif 4 italic via `@fontsource`), and reset/skeleton styles (`base.css`) are imported once through `src/design/index.ts` and read via `var(--frost-*)` custom properties. The Frost token surface includes `--frost-ink`, `--frost-page`, `--frost-spark`, the eight-step alpha ladder (`--frost-alpha-quiet` … `--frost-alpha-active`), cell-grid spacing (`--frost-cell` 8px, `--frost-line` 16px, `--frost-gap-section` 48px, `--frost-gap-page` 64px, `--frost-pad-page` 32px, `--frost-strip-1` … `--frost-strip-8` 176-1408px), mono/serif typography slots, and motion keyframes (`frost-blinker`, `frost-pulse`, `frost-toast`).

The mobile-consumable token source is `tokens/tokens.json` (in the ratatoskr-web repo); update that JSON first, then keep `src/design/tokens.css` in sync. Theme selection writes `data-theme="light" | "dark"` on `<html>` so token CSS resolves on first paint without a flash. Add new components by extending the design directory; never reach for an external design system in feature code.

### Mobile responsiveness

Frost spans web (1440px / 178-col grid) and mobile (393px / 48-col grid) with a tablet (768-1199px) range that uses web tokens. The React frontend adapts via container queries on the AppShell main content area: every responsive component uses `@container main (max-width: 768px)` rather than `@media`, isolating mobile reflow from the viewport (e.g., a drawer overlay reflows to match the available content width even though the viewport is larger).

Below 768px the cell grid switches to 48 columns (boot script in `index.html` sets `--ch = window.innerWidth / 48` vs `/178` on desktop), the FrostHeader collapses to 54px, the FrostSideNav becomes a slide-in drawer with a `page@0.85` backdrop, and a fixed-bottom 56px MobileTabBar (`[ QUEUE · DIGESTS · TOPICS · SETTINGS ]`) takes over primary navigation. Modals go full-screen. All interactive primitives size their hit areas to ≥44×44px via container-query overrides. Per-route layouts transform desktop tables → stacked cards, multi-column grids → single column, `BracketTabs` → horizontal-scroll segmented controls.

Mobile-specific tokens (in `tokens.css`): `--frost-spark-mobile` (4px vs web's 2px), `--frost-tab-bar-height` (56px), `--frost-mobile-header` (54px), `--frost-pad-page-mobile` (16px), `--frost-gap-ext` (10px). Per-route mobile CSS lives in dedicated `*.mobile.css` files imported once via the `mobile.css` aggregator at `src/design/mobile.css`.

---

## Directory Layout

```text
ratatoskr-web/         # https://github.com/po4yka/ratatoskr-web
  src/
    api/            # Typed API gateway + envelope normalization
    auth/           # Hybrid auth provider, guards, storage, redirects
    components/     # App shell + shared UI
    design/         # Project-owned design shim: primitives, table, modal, icons, tokens
    features/       # Route-level pages (library/search/submit/collections/...)
    routes/         # Route manifest + feature flags
  vite.config.ts    # base=/static/web, outDir points to backend app/static/web
```

---

## Serving Contract

- Static assets: `/static/web/*`
- SPA entrypoint: `/web` and `/web/{path:path}`
- FastAPI implementation: `app/api/main.py` (`web_interface` route)
- If bundle is missing, backend returns `404 "Web interface is not built"`

Build output contract:

- Vite `outDir`: `../../app/static/web`
- Vite `base`: `/static/web/`

---

## Route Map

- `/web/library`
- `/web/library/:id`
- `/web/articles`
- `/web/search`
- `/web/submit`
- `/web/collections`
- `/web/collections/:id`
- `/web/feeds`
- `/web/signals`
- `/web/digest`
- `/web/repositories`
- `/web/repositories/:repositoryId`
- `/web/preferences`
- `/web/admin`
- `/web/login`

Route-level feature flags live in `src/routes/features.ts`. The canonical route and side-nav manifest lives in `src/routes/manifest.tsx`.

---

## Repositories Feature

**Routes:** `/web/repositories` (list), `/web/repositories/:repositoryId` (detail)

**Feature directory:** `src/features/repositories/`

**API client modules:**

- `src/api/repositories.ts` -- `GET /v1/repositories`, `POST /v1/repositories`, `GET /v1/repositories/{id}`, `POST /v1/repositories/{id}/reanalyze`, `DELETE /v1/repositories/{id}`
- `src/api/github.ts` -- `GET /v1/auth/github/status`, `POST /v1/auth/github/pat`, `POST /v1/auth/github/device/start`, `POST /v1/auth/github/device/poll`, `DELETE /v1/auth/github`

The list route (`/web/repositories`) uses `@tanstack/react-virtual` for row virtualization (same pattern as LibraryPage) and Frost design tokens throughout. No deviations from DESIGN.md defaults; `--frost-*` custom properties only.

**GitHub Integration panel:** `src/features/preferences/` -- `PreferencesPage.tsx` contains a GitHub Integration slot where users connect via PAT or initiate the OAuth Device Flow (when the backend is configured with `GITHUB_OAUTH_APP_CLIENT_ID`).

---

## Real-time Progress Streaming (SSE)

The SubmitPage opens a Server-Sent Events stream against `GET /v1/requests/{id}/stream` whenever a request is in flight. As `phase` events land it advances the progress indicator (`extracting → summarizing → validating → persisting → done`), and `section` events progressively populate the summary card before the final `fetchSummary` call resolves.

**Modules:**

- `src/api/streamRequest.ts` -- `subscribeToRequest(requestId, handlers)` helper. Uses `@microsoft/fetch-event-source` so it can attach `Authorization: Bearer <token>` (native `EventSource` cannot). Performs single-shot 401 → `refreshAccessToken` → reconnect using the same `getStoredTokens` / `setStoredTokens` chain as `client.ts`. Exponential backoff 250ms → 5s.
- `src/hooks/useRequestStream.ts` -- `useRequestStream(requestId)` returns `{ phase, sectionsBySlug, isStreaming, error, fellBack }`. After two consecutive fatal closes the hook flips `fellBack=true` so the page can switch to the existing `useRequestStatus` polling path.
- `src/features/submit/SubmitPage.tsx` -- consumes `useRequestStream`; falls back to polling when `fellBack`.

**Generated types:** `StreamPhaseEvent`, `StreamSectionEvent`, `StreamDoneEvent`, `StreamErrorEvent` are emitted into `src/api/generated.ts` from `docs/openapi/mobile_api.yaml`.

---

## Authentication Model

Auth is hybrid and selected at runtime in `detectAuthMode`:

1. `telegram-webapp` mode - Trigger: `window.Telegram.WebApp.initData` exists - Request header: `X-Telegram-Init-Data` - Typical use: launched from Telegram Mini App context

2. `jwt` mode - Trigger: no WebApp initData - Two login variants share this mode: - Telegram Login Widget → `POST /v1/auth/telegram-login` - Nickname/email + password → `POST /v1/auth/credentials-login`. Always rendered as the primary form in JWT mode; the route returns `503` if the deploy has not set `CREDENTIALS_LOGIN_PEPPER`, which surfaces as the canonical sign-in error in the UI. - Client id: `web-v1` - Session: bearer token storage + auto refresh via `POST /v1/auth/refresh` - Pre-v1 client id renames may require signing in again because sessions are scoped by client id.

### Token storage (dual-bucket)

`src/auth/storage.ts` writes to two buckets keyed by the same `ratatoskr_web_auth_tokens` envelope name:

- `localStorage` when the login persists across browser close (Telegram, secret-key, or credentials with Remember Me checked).
- `sessionStorage` when the credentials login was made with Remember Me unchecked. Tokens are dropped automatically when the browser tab closes; the refresh cookie is also issued without `Max-Age` so the server-side rotation chain stops at the same point.

The chosen bucket is encoded in the persisted JSON (`persistent: boolean`) so `client.ts:refreshAccessToken` writes the rotated tokens back to the same place. Read order is `sessionStorage → localStorage` so a fresh non-remembered login can never be shadowed by a stale localStorage row.

Auth provider implementation: `src/auth/AuthProvider.tsx`. Credentials form: `src/features/auth/CredentialsLoginForm.tsx` — Frost primitives only (`MonoInput`, `Checkbox`, `BracketButton`, `StatusBadge`); the form renders a single canonical "Invalid credentials." string for every 401 path so timing/wording cannot leak which dimension was wrong.

First-time onboarding belongs at `/web/onboarding`. The web app should call `GET /v1/users/me` after auth, redirect users with `profile.onboardingCompletedAt === null` to onboarding, persist language/theme/display-name/default-summary-language with `PUT /v1/users/me`, then call `POST /v1/users/me/onboarding/complete`. The Telegram `/start` copy uses the same setup sequence: choose language, theme, display name, and default summary language, then start sending links.

---

## API Layer Conventions

The frontend API gateway (`src/api/client.ts`) provides:

- Envelope handling (`success/data/meta/error`)
- Mixed key-style normalization (`snake_case` + `camelCase`)
- Standard error mapping
- JWT refresh retry on `401` in JWT mode
- Automatic auth header injection based on active auth mode

Submission flow (`src/features/submit`) includes:

- URL validation + duplicate pre-check
- Status polling lifecycle (`pending` -> `crawling|processing` -> `completed|failed`)
- Retry operation for failed requests

Search flow includes advanced filters:

- Mode (`auto|keyword|semantic|hybrid`)
- Language, read/favorite state, date range
- Tag/domain multi-select and similarity threshold

Collections flow includes:

- Tree view navigation
- Add/remove/move/reorder items
- Inline create/rename/delete operations

Digest flow parity (web route `/web/digest`) includes:

- Channel subscriptions
- Digest preferences
- Trigger digest now / trigger single-channel (owner)
- Delivery history

Note: digest endpoints require Telegram WebApp auth context.

Signals flow (`/web/signals`) includes:

- Ranked signal queue from `GET /v1/signals`
- Vector store/source readiness from `GET /v1/signals/health`
- Source health and pause/resume controls from `/v1/signals/sources/*`
- Feedback actions: like, queue, skip, hide source
- Topic preference creation via `POST /v1/signals/topics`

Admin page (`/web/admin`) includes:

- Database info (file size, table row counts)
- Cache controls (clear Redis URL cache)

---

## Generated API Types

`src/api/generated.ts` is auto-generated by `openapi-typescript` from `docs/openapi/mobile_api.yaml`. Do not edit it manually — CI verifies freshness via `npm run generate:api`.

### What is enforced today

`npm run check:static` runs `tools/check-api-types.mjs`, which fails if any of these files declare a hand-written `interface` for a type that must be derived from the generated schema:

| File | Banned hand-written interfaces |
|---|---|
| `summaries.ts` | `SummaryCompact`, `SummaryDetail` |
| `auth.ts` | `SummaryCompact`, `SummaryDetail`, `Request` |
| `requests.ts` | `SummaryCompact`, `SummaryDetail` |

Derived types live in `src/api/types.ts`:

```ts
import type { components } from "./generated";
export type SummaryCompact = components["schemas"]["SummaryListItem"];
```

### Modules still needing migration (first-pass complete, 15 remaining)

The following modules define their own interfaces independently of `generated.ts`. Migrate them incrementally — highest traffic first:

`highlights.ts`, `collections.ts`, `search.ts`, `tags.ts`, `user.ts`, `digest.ts`, `customDigest.ts`, `rss.ts`, `signals.ts`, `rules.ts`, `webhooks.ts`, `backups.ts`, `importExport.ts`, `admin.ts`, `session.ts`

### How to migrate a module

1. Find the matching schema name in `generated.ts`:

   ```bash
   grep -n "YourTypeName\|YourSchemaName" src/api/generated.ts
   ```

2. In `types.ts`, replace (or add) the hand-written interface with a type alias:

   ```ts
   import type { components } from "./generated";

   // spec: components["schemas"]["YourSchemaName"]
   export type YourType = components["schemas"]["YourSchemaName"];
   ```

3. If a field is present in the hand-written type but absent from the generated schema (a **contract gap**), do NOT add a shim. Instead, open a backend task to add the field to the OpenAPI spec, and document the gap with a comment in `types.ts`.

4. Update the module to `import type { YourType } from "./types"` instead of declaring its own interface.

5. Run `npm run check:static` — it must pass at 0 errors.

6. Add the banned interface name to the relevant rule in `tools/check-api-types.mjs` so future regressions are caught automatically.

### Known contract gaps

| Gap | Description |
|---|---|
| `SummaryDetail` | No flat generated schema — `SummaryDetailData` nests fields under `.summary` / `.request` / `.source` / `.processing`. Kept as manual type. |
| `RequestStatus.progressPct` | Generated `RequestStatusData` uses `progress.percentage` (nested). Frontend projects to flat `progressPct`. |
| `RequestStatus.summaryId` | Not in `RequestStatusData`; resolved via a separate `GET /v1/requests/{id}` call at runtime. |

---

## UI Architecture

- Global shell: design `Header` + `SideNav` (`src/components/AppShell.tsx`)
- Session UX: - in-app session status label - manual "Verify session" action - inline session warnings + re-auth actions
- Read experience polish (`/web/library/:id`): - reading progress bar - text size + density controls - copy/share helpers - favorite/read/collection actions

---

## Local Development

Run these commands from the ratatoskr-web repo root:

```bash
npm ci
npm run dev
```

Optional environment variables:

- `VITE_API_BASE_URL` (default: same-origin)
- `VITE_TELEGRAM_BOT_USERNAME` (required for JWT mode login widget)
- `VITE_ROUTER_BASENAME` (default: `/web`)

When testing same-host serving (instead of Vite proxy):

```bash
npm run build
# Then in the backend repo:
uvicorn app.api.main:app --reload
# open http://localhost:8000/web/library
```

---

## Quality Checks

Web commands (run from the ratatoskr-web repo root):

```bash
npm run lint
npm run typecheck
npm run check:static
npm run test
npm run test:e2e
npm run build
```

CI jobs in `.github/workflows/ci.yml`:

- `web-build`
- `web-test`
- `web-static-check`
- `web-storybook-build`
- `web-playwright-visual` — route screenshots (16 routes × 4 device profiles) + Storybook story snapshots (~50 components); all baselines committed to repo; see `docs/reference/visual-regression.md`

---

## Deployment Notes

- The CI/CD pipeline builds ratatoskr-web separately, copies `dist/` into the backend's `app/static/web/`, then builds and pushes the backend container.
- Runtime image ships the static bundle at `/app/app/static/web`.
- Same-host deployment avoids CORS complexity.

---

## Troubleshooting

### `/web` returns 404

Web bundle is not built into `app/static/web`.

```bash
# In the ratatoskr-web repo:
npm ci
npm run build
# Then copy dist/ into the backend's app/static/web/
```

### Login page shows "Missing configuration"

`VITE_TELEGRAM_BOT_USERNAME` is not set for JWT mode.

### Digest page fails outside Telegram

Digest endpoints require Telegram WebApp `initData`; use Telegram-launched context.

### Repeated auth failures in browser mode

Clear stored tokens (login page has "Clear local session") and sign in again.

---

**Last Updated:** 2026-04-30
