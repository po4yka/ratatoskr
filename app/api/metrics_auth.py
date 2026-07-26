"""Machine-to-machine authentication for Prometheus scraping."""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status

from app.config import load_config


async def require_metrics_bearer(request: Request) -> None:
    """Require the dedicated metrics bearer token without weakening owner auth."""
    expected = load_config(allow_stub_telegram=True).auth.metrics_bearer_token
    scheme, _, provided = request.headers.get("authorization", "").partition(" ")
    authorized = (
        expected is not None
        and scheme.lower() == "bearer"
        and bool(provided)
        and hmac.compare_digest(provided, expected)
    )
    if not authorized:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid metrics credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
