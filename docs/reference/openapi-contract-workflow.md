# OpenAPI Contract Workflow

The backend FastAPI app is the source of truth for the Mobile API contract. The committed files `docs/openapi/mobile_api.yaml` and `docs/openapi/mobile_api.json` are generated artifacts used by web and KMP clients; do not edit them by hand.

## Backend

Change routers, request/response models, or `app.api.models.responses.common.API_CONTRACT_VERSION` first, then regenerate the committed OpenAPI files:

```bash
make generate-openapi
make check-openapi-drift
make check-openapi-validate
make check-openapi
```

CI runs `tools/scripts/generate_openapi.py --check`, so a PR fails if `app.api.main:app` would generate a different YAML or JSON file. `info.version` is sourced from `API_CONTRACT_VERSION`; it is the API contract semver and must not be tied to deploy/build metadata.

## External clients

The editable web and KMP clients live in separate repositories. After committing a backend change and regenerated specification:

1. pin the client repository to the intended backend commit/specification;
2. run that repository's documented API generation command;
3. run its generated-code drift, type, test, and build checks;
4. fix incompatible shapes in the backend model or document a narrowly scoped generator workaround in the client repository;
5. commit the pin and generated client artifacts together.

Do not copy commands or generator-script paths into this repository unless they can be validated here. The backend acceptance bar remains the four Make targets above; each client owns its downstream generation implementation.
