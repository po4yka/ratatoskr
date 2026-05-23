"""Meta social account adapters."""

from app.adapters.social.meta.instagram_client import InstagramClient, InstagramMedia
from app.adapters.social.meta.oauth import (
    InstagramOAuthConfig,
    InstagramOAuthError,
    ThreadsOAuthConfig,
    ThreadsOAuthError,
)
from app.adapters.social.meta.threads_client import ThreadsClient, ThreadsMedia

__all__ = [
    "InstagramClient",
    "InstagramMedia",
    "InstagramOAuthConfig",
    "InstagramOAuthError",
    "ThreadsClient",
    "ThreadsMedia",
    "ThreadsOAuthConfig",
    "ThreadsOAuthError",
]
