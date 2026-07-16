.PHONY: format lint type type-all test test-unit test-integration test-all all setup-dev bootstrap seed-demo-data teardown-dev extension-zip venv pre-commit-install pre-commit-run check-lock generate-openapi check-openapi check-openapi-validate check-openapi-drift check-file-loc check-layout clean-generated security security-bandit security-deps static-checks

COMPOSE_FILE := ops/docker/docker-compose.yml
DEV_COMPOSE_FILE := ops/docker/docker-compose.dev.yml
DEV_COMPOSE := docker compose -f $(COMPOSE_FILE) -f $(DEV_COMPOSE_FILE)
DOCKERFILE_BOT := ops/docker/Dockerfile
DOCKERFILE_API := ops/docker/Dockerfile.api
DEV_USER_ID ?= 424242
DEV_POSTGRES_PASSWORD ?= ratatoskr-dev-password
DEV_DATABASE_URL ?= postgresql+asyncpg://ratatoskr_app:$(DEV_POSTGRES_PASSWORD)@127.0.0.1:$(POSTGRES_HOST_PORT)/ratatoskr
POSTGRES_HOST_PORT ?= 5432
REDIS_HOST_PORT ?= 6379
QDRANT_HOST_PORT ?= 6333

format:
	uv run --frozen ruff format .
	uv run --frozen isort .

lint:
	uv run --frozen ruff check .
	uv run --frozen python tools/scripts/check_file_size.py --max-loc 1500 --baseline tools/scripts/file_size_baseline.json

check-file-loc:
	uv run --frozen python tools/scripts/check_file_size.py --max-loc 1500 --baseline tools/scripts/file_size_baseline.json

type:
	uv run --frozen mypy app --show-error-codes --pretty --cache-dir .mypy_cache

type-all:
	uv run --frozen mypy app tests

test:
	uv run --frozen pytest tests/ -v

test-unit:
	uv run --frozen pytest tests/ -m "not slow and not integration" -v

test-integration:
	uv run --frozen pytest tests/ -m "integration" -v

test-all:
	uv run --frozen pytest tests/ -v --cov=app --cov-report=term-missing

test-fast:
	uv run --frozen pytest tests/ -m "not slow and not integration" -v -x

# Mirrors the bandit-scan and pip-audit-scan jobs in .github/workflows/ci.yml so
# devs can reproduce CI security checks locally before pushing. Not part of
# `make all` because pip-audit hits the network and is slow on cold caches.
security: security-bandit security-deps

security-bandit:
	uv run --frozen --no-default-groups --only-group ci-security-tools bandit -r app -ll

security-deps:
	PIP_AUDIT_CMD='uv run --frozen --no-default-groups --only-group ci-security-tools pip-audit' bash tools/scripts/audit-deps.sh

# Runs custom Semgrep rules that catch patterns complementary to Ruff:
# mutable-aliasing hazards and bare/broad exception handlers.
# Also enforced in CI and as pre-push hooks.
static-checks:
	uv run --frozen --no-default-groups --only-group ci-security-tools semgrep --config semgrep/python-mutability.yml --error app/ tests/
	uv run --frozen --no-default-groups --only-group ci-security-tools semgrep --config semgrep/python-bare-except.yml --error app/ tests/

# Note: `all` deliberately omits `security`; run `make security` separately.
all: format lint type test

setup-dev:
	uv sync --all-extras --dev
	pre-commit install

bootstrap: setup-dev
	POSTGRES_PASSWORD="$(DEV_POSTGRES_PASSWORD)" POSTGRES_HOST_PORT="$(POSTGRES_HOST_PORT)" REDIS_HOST_PORT="$(REDIS_HOST_PORT)" QDRANT_HOST_PORT="$(QDRANT_HOST_PORT)" $(DEV_COMPOSE) up -d --wait postgres redis qdrant
	DATABASE_URL="$(DEV_DATABASE_URL)" uv run python -m app.cli.migrate_db --apply
	$(MAKE) seed-demo-data DEV_USER_ID="$(DEV_USER_ID)" DEV_POSTGRES_PASSWORD="$(DEV_POSTGRES_PASSWORD)" POSTGRES_HOST_PORT="$(POSTGRES_HOST_PORT)"
	@echo ""
	@echo "Ratatoskr dev bootstrap complete."
	@echo "Demo user: ALLOWED_USER_IDS=$(DEV_USER_ID)"
	@echo "Postgres:  postgresql+asyncpg://ratatoskr_app:***@127.0.0.1:$(POSTGRES_HOST_PORT)/ratatoskr"
	@echo "Redis:     redis://127.0.0.1:$(REDIS_HOST_PORT)/0"
	@echo "Qdrant:    http://127.0.0.1:$(QDRANT_HOST_PORT)"
	@echo "API:       http://127.0.0.1:18000 after: POSTGRES_PASSWORD=$(DEV_POSTGRES_PASSWORD) ALLOWED_USER_IDS=$(DEV_USER_ID) $(DEV_COMPOSE) up -d mobile-api"
	@echo "Grafana:   http://127.0.0.1:3001 after: POSTGRES_PASSWORD=$(DEV_POSTGRES_PASSWORD) COMPOSE_PROFILES=with-monitoring $(DEV_COMPOSE) up -d grafana"
	@echo "CLI smoke: DATABASE_URL='$(DEV_DATABASE_URL)' uv run python -m app.cli.summary --url https://example.com"

seed-demo-data:
	DATABASE_URL="$(DEV_DATABASE_URL)" uv run python -m app.cli.seed_demo_data --user-id "$(DEV_USER_ID)"

teardown-dev:
	POSTGRES_PASSWORD="$(DEV_POSTGRES_PASSWORD)" POSTGRES_HOST_PORT="$(POSTGRES_HOST_PORT)" REDIS_HOST_PORT="$(REDIS_HOST_PORT)" QDRANT_HOST_PORT="$(QDRANT_HOST_PORT)" $(DEV_COMPOSE) down -v --remove-orphans

extension-zip:
	uv run --frozen python tools/scripts/build_extension_zip.py

venv:
	bash tools/scripts/create_venv.sh

check-layout:
	uv run --frozen python tools/scripts/check_root_hygiene.py

clean-generated:
	rm -rf htmlcov
	rm -f .coverage coverage.json coverage.xml debug_fav.log error.log traceback.log
	rm -rf frontend

.PHONY: pre-commit-install
pre-commit-install:
	uv run --frozen pre-commit install --install-hooks
	uv run --frozen pre-commit autoupdate || true

.PHONY: pre-commit-run
pre-commit-run:
	uv run --frozen pre-commit run --all-files

.PHONY: lock-uv
lock-uv:
	uv lock
	uv export --no-dev --format requirements-txt -p 3.13 -o requirements.txt
	uv export --only-group dev --no-hashes --format requirements-txt -p 3.13 -o requirements-dev.txt
	uv export --no-dev --no-hashes --format requirements-txt -p 3.13 --extra api --extra ml --extra youtube --extra export --extra scheduler --extra mcp --extra graph -o requirements-all.txt
	python3 tools/scripts/check_excluded_versions.py

check-lock:
	uv lock
	uv export --no-dev --format requirements-txt -p 3.13 -o requirements.txt
	uv export --only-group dev --no-hashes --format requirements-txt -p 3.13 -o requirements-dev.txt
	uv export --no-dev --no-hashes --format requirements-txt -p 3.13 --extra api --extra ml --extra youtube --extra export --extra scheduler --extra mcp --extra graph -o requirements-all.txt
	@git diff --exit-code uv.lock requirements.txt requirements-dev.txt requirements-all.txt || (echo "Lockfiles are out of date. Run 'make lock-uv' and commit changes." && exit 1)
	python3 tools/scripts/check_excluded_versions.py

generate-openapi: ## Generate docs/openapi/mobile_api.yaml/json from app.api.main:app
	uv run --frozen --extra api python tools/scripts/generate_openapi.py

sync-openapi: generate-openapi ## Backward-compatible alias for generate-openapi

check-openapi: ## Run OpenAPI spec sync checks (includes generated drift check)
	uv run --frozen --extra api pytest tests/api/test_openapi_sync.py tests/api/test_openapi_security.py tests/api/test_runtime_openapi_drift.py tests/tools/test_generate_openapi.py -v

check-openapi-validate: ## Validate OpenAPI spec syntax
	uv run --frozen --extra api openapi-spec-validator docs/openapi/mobile_api.yaml
	uv run --frozen --extra api openapi-spec-validator docs/openapi/mobile_api.json

check-openapi-drift: ## Fail if committed OpenAPI docs differ from app.api.main:app
	uv run --frozen --extra api python tools/scripts/generate_openapi.py --check

check-openapi-json-sync: check-openapi-drift ## Backward-compatible alias for generated spec drift check

# ==============================================================================
# Docker targets
# ==============================================================================

.PHONY: docker-build docker-build-no-cache docker-run docker-stop docker-restart
.PHONY: docker-logs docker-shell docker-test docker-clean docker-size docker-deploy
.PHONY: docker-build-mobile-api docker-build-mobile-api-no-cache docker-restart-mobile-api
.PHONY: docker-rebuild-mobile-api docker-logs-mobile-api docker-shell-mobile-api

docker-build:
	DOCKER_BUILDKIT=1 docker build -f $(DOCKERFILE_BOT) --tag ratatoskr:latest --progress=plain .

docker-build-no-cache:
	DOCKER_BUILDKIT=1 docker build -f $(DOCKERFILE_BOT) --no-cache --tag ratatoskr:latest --progress=plain .

docker-build-mobile-api:
	DOCKER_BUILDKIT=1 docker compose -f $(COMPOSE_FILE) build mobile-api

docker-build-mobile-api-no-cache:
	DOCKER_BUILDKIT=1 docker compose -f $(COMPOSE_FILE) build --no-cache mobile-api

docker-run:
	docker compose -f $(COMPOSE_FILE) up -d

docker-stop:
	docker compose -f $(COMPOSE_FILE) down

docker-restart: docker-stop docker-run

docker-logs:
	docker compose -f $(COMPOSE_FILE) logs -f ratatoskr

docker-logs-tail:
	docker compose -f $(COMPOSE_FILE) logs --tail=100 -f ratatoskr

docker-logs-mobile-api:
	docker compose -f $(COMPOSE_FILE) logs -f mobile-api

docker-shell:
	docker compose -f $(COMPOSE_FILE) exec ratatoskr sh

docker-shell-root:
	docker compose -f $(COMPOSE_FILE) exec -u root ratatoskr sh

docker-shell-mobile-api:
	docker compose -f $(COMPOSE_FILE) exec mobile-api sh

docker-restart-mobile-api:
	docker compose -f $(COMPOSE_FILE) up -d mobile-api

docker-rebuild-mobile-api: docker-build-mobile-api docker-restart-mobile-api

docker-test:
	DOCKER_BUILDKIT=1 docker build -f $(DOCKERFILE_BOT) --target builder --tag ratatoskr:test .
	docker run --rm ratatoskr:test uv run pytest

docker-clean:
	docker compose -f $(COMPOSE_FILE) down -v
	docker rmi ratatoskr:latest ratatoskr:test 2>/dev/null || true
	docker builder prune -f

docker-size:
	@echo "=== Docker Image Size ==="
	@docker images ratatoskr --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}\t{{.CreatedAt}}"
	@echo ""
	@echo "=== Layer Analysis ==="
	@docker history ratatoskr:latest --human --format "table {{.Size}}\t{{.CreatedBy}}" | head -15

docker-deploy:
	docker compose -f $(COMPOSE_FILE) build ratatoskr
	docker compose -f $(COMPOSE_FILE) up -d --no-deps --force-recreate ratatoskr
	@echo "=== Deployment complete ==="
	@echo "Check logs with: make docker-logs"

# Local-development helper: build the SPA in the sibling ratatoskr-web/
# checkout and stage it for a directly launched FastAPI process. Docker images
# ignore this directory and use the reviewed archive produced by `web-bundle`,
# so release contents never depend on local ignored files.
.PHONY: stage-web web-bundle
WEB_REPO ?= ../ratatoskr-web

stage-web:
	@test -d "$(WEB_REPO)" || { echo "WEB_REPO=$(WEB_REPO) not found; clone ratatoskr-web alongside ratatoskr/" >&2; exit 1; }
	cd "$(WEB_REPO)" && npm ci && npm run build
	rm -rf app/static/web
	mkdir -p app/static/web
	rsync -a --delete "$(WEB_REPO)/dist/" app/static/web/
	@echo "==> staged $$(du -sh app/static/web | cut -f1) into app/static/web"

# Update the immutable Docker artifact from the exact frontend SHA recorded in
# ops/docker/ratatoskr-web.commit. The script runs frontend static checks,
# tests, and the production build before writing a deterministic archive.
web-bundle:
	python tools/scripts/build_web_bundle.py --web-repo "$(WEB_REPO)"

# Build the arm64 image locally (Mac) and stream it to the Pi over SSH so the
# Pi never has to run the heavy build. Override SERVICE=mobile-api to ship
# the API image instead. See tools/scripts/build-and-deploy-pi.sh for flags
# and env vars (RASPI_HOST, RASPI_REMOTE_PATH, COMPOSE_PROJECT).
.PHONY: pi-deploy pi-deploy-no-cache pi-build-only pi-migrate pi-rollback pi-deploy-all pi-smoke
SERVICE ?= ratatoskr
RASPI_HOST ?= raspi
PI_SMOKE_PORT ?= 18000
APPLY ?= 0

pi-deploy:
	bash tools/scripts/build-and-deploy-pi.sh --service $(SERVICE)

pi-deploy-no-cache:
	bash tools/scripts/build-and-deploy-pi.sh --service $(SERVICE) --no-cache

pi-build-only:
	bash tools/scripts/build-and-deploy-pi.sh --service $(SERVICE) --no-restart

pi-migrate:
	bash tools/scripts/build-and-deploy-pi.sh --migrate-only $(if $(filter 1 true yes,$(APPLY)),--apply,)

pi-rollback:
	bash tools/scripts/build-and-deploy-pi.sh --service $(SERVICE) --rollback

# End-to-end: build+ship+restart the four ratatoskr services (bot/worker/
# scheduler/mobile-api) in one pass. The image includes the reviewed SPA
# archive, then the smoke check verifies /web/ and /health/ready from the Pi host.
pi-deploy-all:
	bash tools/scripts/build-and-deploy-pi.sh --services "ratatoskr worker scheduler mobile-api"
	$(MAKE) pi-smoke

# Smoke-test mobile-api on the Pi via its mapped host port. /health/ready exercises
# the DB; /web/ confirms the SPA bundle is present. Retries briefly because
# uvicorn binds a few seconds after the container reports healthy.
pi-smoke:
	@echo "==> Smoke-testing http://${RASPI_HOST}:${PI_SMOKE_PORT}"
	@for i in 1 2 3 4 5 6 7 8; do \
	  out=$$(ssh $(RASPI_HOST) curl -fsS -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:$(PI_SMOKE_PORT)/health/ready 2>/dev/null || echo "000"); \
	  echo "    /health/ready attempt $$i -> $$out"; \
	  [ "$$out" = "200" ] && break; \
	  [ $$i -eq 8 ] && { echo "ERROR: /health/ready never returned 200" >&2; exit 1; }; \
	  sleep 4; \
	done
	@out=$$(ssh $(RASPI_HOST) curl -fsS -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:$(PI_SMOKE_PORT)/web/ 2>/dev/null || echo "000"); \
	  echo "    /web/    -> $$out"; \
	  [ "$$out" = "200" ] || { echo "ERROR: /web/ returned $$out" >&2; exit 1; }
	@echo "==> Smoke OK"

docker-health:
	@docker compose -f $(COMPOSE_FILE) ps
	@echo ""
	@docker inspect --format='{{json .State.Health}}' ratatoskr-bot 2>/dev/null | python -m json.tool || echo "Container not running or no health check configured"
