"""GitHub integration configuration."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ._secret_marker import SECRET_MARKER


class GitHubConfig(BaseModel):
    """GitHub integration configuration."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    # API client knobs
    request_timeout_sec: float = Field(
        default=30.0,
        ge=1.0,
        le=300.0,
        validation_alias="GITHUB_REQUEST_TIMEOUT_SEC",
        description="HTTP request timeout in seconds for GitHub API calls",
    )
    readme_max_bytes: int = Field(
        default=51200,
        ge=1024,
        le=524288,
        validation_alias="GITHUB_README_MAX_BYTES",
        description="Maximum README size in bytes to fetch and process",
    )
    concurrency_per_user: int = Field(
        default=2,
        ge=1,
        le=10,
        validation_alias="GITHUB_CONCURRENCY_PER_USER",
        description="Maximum concurrent GitHub API requests per user",
    )

    # OAuth App registration (only required when OAuth Device Flow is used; PAT works without these)
    oauth_app_client_id: str | None = Field(
        default=None,
        validation_alias="GITHUB_OAUTH_APP_CLIENT_ID",
        description="GitHub OAuth App client ID (required for Device Flow)",
    )
    oauth_app_client_secret: SecretStr | None = Field(
        default=None,
        validation_alias="GITHUB_OAUTH_APP_CLIENT_SECRET",
        description="GitHub OAuth App client secret (required for Device Flow)",
        json_schema_extra=SECRET_MARKER,
    )

    # Token encryption (REQUIRED for PAT or OAuth — Fernet key, 32 url-safe base64 bytes)
    token_encryption_key: SecretStr | None = Field(
        default=None,
        validation_alias="GITHUB_TOKEN_ENCRYPTION_KEY",
        description="Fernet encryption key (32 url-safe base64 bytes) for stored tokens",
        json_schema_extra=SECRET_MARKER,
    )
    # Previous Fernet keys for zero-downtime rotation (comma-separated; see token_crypto.py)
    token_previous_keys: SecretStr | None = Field(
        default=None,
        validation_alias="GITHUB_TOKEN_PREVIOUS_KEYS",
        description=(
            "Comma-separated previous Fernet keys still needed to decrypt existing rows. "
            "Remove each key only after running `python -m app.cli.rotate_github_tokens`."
        ),
        json_schema_extra=SECRET_MARKER,
    )

    # Daily stars sync
    sync_enabled: bool = Field(
        default=True,
        validation_alias="GITHUB_SYNC_ENABLED",
        description="Enable daily GitHub stars sync",
    )
    sync_cron: str = Field(
        default="0 2 * * *",
        validation_alias="GITHUB_SYNC_CRON",
        description="UTC cron expression for daily stars sync",
    )
    llm_concurrency: int = Field(
        default=2,
        ge=1,
        le=10,
        validation_alias="GITHUB_SYNC_LLM_CONCURRENCY",
        description="Maximum concurrent LLM calls during stars sync",
    )
    llm_daily_budget: int = Field(
        default=100,
        ge=0,
        le=10000,
        validation_alias="GITHUB_SYNC_LLM_DAILY_BUDGET",
        description="Maximum LLM calls per day for GitHub operations",
    )
    sync_batch_size: int = Field(
        default=50,
        ge=1,
        le=500,
        validation_alias="GITHUB_SYNC_BATCH_SIZE",
        description="Number of repo upserts to batch in a single transaction during stars sync",
    )
    sync_star_lists: bool = Field(
        default=True,
        validation_alias="GITHUB_SYNC_STAR_LISTS",
        description=(
            "Mirror GitHub star lists into repositories.list_names via GraphQL. "
            "Costs roughly one request per 100 list items per sync run; a failure "
            "is logged and never fails the star sync itself."
        ),
    )
    full_sync_interval_days: int = Field(
        default=7,
        ge=0,
        le=365,
        validation_alias="GITHUB_FULL_SYNC_INTERVAL_DAYS",
        description=(
            "Days between full star snapshots. Incremental runs only add and update; "
            "soft-unstar (is_starred=false for repos no longer starred) needs the full "
            "listing, so it can only run on a snapshot. 0 disables periodic snapshots, "
            "leaving the first sync as the only full one."
        ),
    )

    # Star-list suggestion for newly added repositories
    star_list_suggest_k: int = Field(
        default=15,
        ge=1,
        le=100,
        validation_alias="GITHUB_STAR_LIST_SUGGEST_K",
        description=(
            "How many already-filed repositories to consult when suggesting a star "
            "list for a new one. The vote is weighted by similarity, so a larger k "
            "mostly adds weak votes rather than changing the winner."
        ),
    )
    star_list_suggest_min_similarity: float = Field(
        default=0.35,
        ge=0.0,
        le=1.0,
        validation_alias="GITHUB_STAR_LIST_SUGGEST_MIN_SIMILARITY",
        description="Cosine-similarity floor below which a neighbour is not counted at all.",
    )
    star_list_suggest_min_score: float = Field(
        default=1.0,
        ge=0.0,
        le=100.0,
        validation_alias="GITHUB_STAR_LIST_SUGGEST_MIN_SCORE",
        description=(
            "Summed-similarity a list must reach before it is applied without asking. "
            "Roughly two or three agreeing neighbours at the default similarity floor."
        ),
    )
    star_list_suggest_dominance_ratio: float = Field(
        default=1.5,
        ge=1.0,
        le=10.0,
        validation_alias="GITHUB_STAR_LIST_SUGGEST_DOMINANCE_RATIO",
        description=(
            "How far ahead of the runner-up the winning list must be. A near-tie is "
            "treated as inconclusive and handed to the LLM fallback instead."
        ),
    )
    star_list_suggest_llm_fallback: bool = Field(
        default=True,
        validation_alias="GITHUB_STAR_LIST_SUGGEST_LLM_FALLBACK",
        description=(
            "Ask an LLM to pick a list when the neighbour vote is inconclusive. "
            "Costs one structured call per added repository in that case. When "
            "disabled, an inconclusive vote leaves the repository unfiled."
        ),
    )
