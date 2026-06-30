---
name: web-frontend-dev
description: Develop and verify the Ratatoskr React + TypeScript + Vite web frontend served by FastAPI at /web. Trigger keywords -- web, frontend, React, Vite, TypeScript, npm run check, web/src, app/static/web, Frost, UI change.
version: 1.0.0
allowed-tools: Bash, Read, Write, Edit, Grep
---

# Web Frontend Development

The Ratatoskr web frontend (codename Frost) is a React 18 + TypeScript + Vite app under `web/`. The built bundle is served by FastAPI at `/web` from `app/static/web/`.

## Layout

```
web/
+-- src/                 # React app source
+-- public/              # Static assets
+-- package.json
+-- vite.config.ts
+-- tsconfig.json
```

The build artifact (`web/dist/`) is copied into `app/static/web/` by the deploy pipeline. `web/dist/`, `app/static/web/`, and `app/static/digest/` are all gitignored.

## Required Checks Before Reporting Done

Per CLAUDE.md, UI changes are NOT complete until you have:

1. Run the static check
2. Run the unit tests
3. Verified the change in a browser (golden path + edge cases)

### Static check (lint + typecheck)

```bash
cd web && npm run check:static
```

This is the fastest signal -- run it after every meaningful edit.

### Unit tests

```bash
cd web && npm run test
```

### Browser verification

```bash
cd web && npm run dev
```

Opens the dev server (usually `http://localhost:5173`). Vite proxies API calls to the FastAPI backend if configured -- check `web/vite.config.ts` for the proxy target.

If you cannot test in a browser, **say so explicitly** instead of claiming the UI works. Static checks verify code correctness, not feature correctness.

## Production Bundle

```bash
cd web && npm run build
# Output: web/dist/
```

The CI/CD pipeline copies `web/dist/` into `app/static/web/` inside the image. Locally, FastAPI serves whatever is already in `app/static/web/`.

## Dependency Management

```bash
cd web && npm install        # install/update lockfile
cd web && npm ci             # clean install from lockfile (CI mode)
cd web && npm outdated       # check for updates
```

Commit both `package.json` and `package-lock.json` together.

## Talking to the Backend

The web frontend consumes the Mobile API (`app/api/`). Endpoints are documented via FastAPI's OpenAPI schema:

```bash
# Local API + docs
uvicorn app.api.main:app --reload
# Then: http://localhost:8000/docs
```

Auth uses JWT (see `app/api/routers/auth/`); the frontend stores tokens in localStorage (check existing implementation, don't reinvent).

## CI Jobs

GitHub Actions runs these jobs for the web app on every PR:

- `web-build` -- `npm run build`
- `web-test` -- `npm run test`
- `web-static-check` -- `npm run check:static`

Match them locally before pushing -- it's the cheapest way to avoid red CI.

## Key Files

- **Entry**: `web/src/main.tsx` (or equivalent)
- **Vite config**: `web/vite.config.ts`
- **TS config**: `web/tsconfig.json`
- **Backend API**: `app/api/main.py`, `app/api/routers/`
- **Static serve**: FastAPI mounts `app/static/web/` at `/web`
- **Built bundle target**: `app/static/web/` (gitignored)

## Important Notes

- `web/`, `app/static/web/`, `app/static/digest/` are all in `.gitignore` -- don't commit build artifacts.
- The mobile API and the web frontend share auth (`app/api/routers/auth/`); changes to JWT flow affect both surfaces.
- For SSE/streaming endpoints, check `app/api/routers/streams.py` and `app/adapters/content/streaming/` (in-process StreamHub).
- If you add a new API endpoint, run `make` targets or check that the OpenAPI spec validation in CI still passes.
- For canonical frontend docs, look under `docs/` (e.g., `docs/explanation/`) -- CLAUDE.md previously referenced a top-level `FRONTEND.md` that no longer exists in the tree.
