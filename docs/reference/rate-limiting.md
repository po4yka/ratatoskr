# Rate Limiting

Ratatoskr applies API rate limits before request handlers run. In production/public deployments (`APP_ENV=production` or `API_PUBLIC_EXPOSURE=true`), rate limiting must be Redis-backed so auth and login attempts share counters across workers and survive process restarts.

## Production Policy

- Set `REDIS_ENABLED=true` and `REDIS_REQUIRED=true`.
- Keep `RATE_LIMIT_REDIS_OVERRIDE=false` or unset.
- Production/public startup fails fast if `RATE_LIMIT_REDIS_OVERRIDE=true`.
- Production/public startup also fails if Redis is disabled or if Redis fallback is allowed through `REDIS_REQUIRED=false`.

`RATE_LIMIT_REDIS_OVERRIDE=true` remains a local development escape hatch only. It must not appear in production env files because the local limiter is per-process and resets on restart, which multiplies effective auth limits in multi-worker deployments.

## Deploy Check

Validate private production env files before deploying:

```bash
python tools/scripts/check_prod_rate_limit_override.py .env.production
```

The CI compose smoke job runs the same checker with `--allow-missing` against conventional committed production env filenames (`.env.production`, `.env.prod`, `ops/docker/.env.production`, `ops/docker/.env.prod`). This catches accidental checked-in overrides while still allowing private env files to stay outside git.

## Failure Mode

If production startup fails with `RATE_LIMIT_REDIS_OVERRIDE=true`, remove the override and configure Redis instead:

```env
APP_ENV=production
REDIS_ENABLED=true
REDIS_REQUIRED=true
RATE_LIMIT_REDIS_OVERRIDE=false
```
