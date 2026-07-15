# Web frontend integration

The editable React/TypeScript frontend is maintained in the separate `ratatoskr-web` repository. This repository owns the FastAPI integration contract, a pinned frontend revision, and the reviewed compiled artifact used by release images.

## Backend serving contract

`app/api/main.py`:

- mounts compiled assets under `/static/web`;
- serves `app/static/web/index.html` for the `/web` application routes;
- falls back to the single-page application entry point for supported client-side routes;
- keeps REST endpoints under `/v1` and API documentation routes separate from the SPA.

For a directly launched FastAPI process, `make stage-web` builds a sibling `ratatoskr-web` checkout and copies `dist/` into `app/static/web/`. That directory is ignored by Docker builds so stale local assets cannot enter a release image.

Release images instead consume `ops/docker/ratatoskr-web.bundle.tar.gz`. The archive is built from the exact SHA stored in `ops/docker/ratatoskr-web.commit`, includes `.source-commit` provenance, and is refreshed with:

```bash
make web-bundle WEB_REPO=../ratatoskr-web
```

The build helper checks out that revision in an isolated directory and runs the frontend static checks, tests, and production build before writing a deterministic archive. Review the revision and regenerated archive together.

## API integration

The frontend consumes the generated contract in `docs/openapi/mobile_api.yaml` or `docs/openapi/mobile_api.json`. Authentication, refresh-token behavior, error envelopes, and SSE semantics are backend-owned and documented in [Mobile API](mobile-api.md).

When changing the backend contract:

1. update FastAPI routers and Pydantic models;
2. regenerate and validate OpenAPI;
3. update or regenerate the client in `ratatoskr-web`;
4. run the frontend repository's type, test, and build checks;
5. update `ops/docker/ratatoskr-web.commit` to the reviewed frontend revision;
6. run `make web-bundle` and review the new archive/provenance;
7. run the Docker browser smoke checks.

The frontend source layout, package scripts, component catalog, and design-token implementation are intentionally not duplicated here because they cannot be validated from this repository.
