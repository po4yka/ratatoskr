---
name: web-frontend-dev
description: Coordinate Ratatoskr backend web integration with the external React + TypeScript + Vite frontend. Trigger keywords -- web, frontend, React, Vite, TypeScript, ratatoskr-web, app/static/web, Frost, UI change.
version: 2.0.0
allowed-tools: Bash, Read, Write, Edit, Grep
---

# Web Frontend Development

Editable frontend source lives in the separate sibling repository `../ratatoskr-web`. This repository owns the FastAPI `/web` serving contract, generated OpenAPI artifacts, a pinned frontend revision, and the reviewed release bundle.

## Repository boundaries

| Concern | Location |
| --- | --- |
| React/TypeScript source, npm checks, browser tests | `../ratatoskr-web/` |
| Backend API and auth | `app/api/` |
| Local staged SPA output | `app/static/web/` (ignored) |
| Pinned frontend revision | `ops/docker/ratatoskr-web.commit` |
| Reviewed release archive | `ops/docker/ratatoskr-web.bundle.tar.gz` |
| Integration contract | `docs/reference/frontend-web.md` |

Do not create or edit a local `web/` source tree in this repository.

## Frontend checks

Run the client repository's own scripts from its checkout and inspect its `package.json` before assuming script names:

```bash
cd ../ratatoskr-web
npm ci
npm run check:static
npm run test
npm run build
```

Render and inspect UI changes in that repository. If browser verification cannot run, report the gap explicitly.

## Backend integration workflow

1. Change FastAPI routers/models in this repository.
2. Regenerate and validate the API contract:

   ```bash
   make generate-openapi
   make check-openapi-drift
   make check-openapi-validate
   make check-openapi
   ```

3. Update or regenerate the client in `ratatoskr-web`.
4. Run client static checks, tests, build, and browser verification there.
5. Stage a local build for directly launched FastAPI only when needed:

   ```bash
   make stage-web WEB_REPO=../ratatoskr-web
   ```

6. For a release, update the reviewed revision and rebuild the deterministic archive:

   ```bash
   make web-bundle WEB_REPO=../ratatoskr-web
   ```

Docker release images consume the reviewed archive, not whatever happens to be in ignored `app/static/web/`.

## Key files

- Integration guide: `docs/reference/frontend-web.md`
- FastAPI app and routers: `app/api/main.py`, `app/api/routers/`
- Static serving: `app/api/main.py` (`/web` routes and `app/static/web/index.html`)
- Bundle builder: `tools/scripts/build_web_bundle.py`
- Revision pin: `ops/docker/ratatoskr-web.commit`
- OpenAPI workflow: `docs/reference/openapi-contract-workflow.md`

Changes to auth, cookies, sync, or streaming contracts must be coordinated with the external client even when no frontend files exist in this repository.
