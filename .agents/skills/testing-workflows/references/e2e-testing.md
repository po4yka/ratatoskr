# E2E Testing and Docker

## Docker Testing

### Build and Run

```bash
# Build and run through the deployment source of truth
docker compose -f ops/docker/docker-compose.yml build ratatoskr
docker compose -f ops/docker/docker-compose.yml up -d ratatoskr
```

### Check Bot Health

```bash
# View logs
docker logs ratatoskr

# Check if bot is running
docker ps | grep ratatoskr

# Inspect database
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "\dt"
```

## E2E Tests (Gated)

E2E tests require live API keys and are gated behind the `E2E` environment variable.

```bash
# Enable E2E tests
export E2E=1

# Run E2E tests (requires live API keys)
python -m pytest tests/ -v -m integration
```

These tests hit real external services (Firecrawl, OpenRouter) and require valid credentials in `.env`.
