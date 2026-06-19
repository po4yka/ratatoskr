from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import app.di.mcp as mcp_di
from app.mcp.http_auth import McpRequestIdentity

if TYPE_CHECKING:
    from collections.abc import Iterator

_NO_REQUEST_USER_SCOPE = object()


class McpServerContext:
    """Owns MCP runtime state, user scope, and lazy semantic-search services."""

    def __init__(
        self,
        *,
        database_dsn: str | None = None,
        user_id: int | None = None,
        logger: logging.Logger | None = None,
        vector_retry_interval_sec: float | None = None,
        local_vector_retry_interval_sec: float | None = None,
    ) -> None:
        self.logger = logger or logging.getLogger("ratatoskr.mcp")
        self.database_dsn = database_dsn
        self._runtime: Any = None
        self._api_runtime: Any = None
        self._user_id = user_id
        self._request_identity: contextvars.ContextVar[McpRequestIdentity | None | object] = (
            contextvars.ContextVar(
                "mcp_request_identity",
                default=_NO_REQUEST_USER_SCOPE,
            )
        )
        self._api_runtime_lock: asyncio.Lock = asyncio.Lock()
        self._vector_retry_interval_sec = (
            vector_retry_interval_sec
            if vector_retry_interval_sec is not None
            else mcp_di.VECTOR_RETRY_INTERVAL_SEC
        )
        self._local_vector_retry_interval_sec = (
            local_vector_retry_interval_sec
            if local_vector_retry_interval_sec is not None
            else mcp_di.LOCAL_VECTOR_RETRY_INTERVAL_SEC
        )

    @property
    def user_id(self) -> int | None:
        identity = self.request_identity
        if identity is not None:
            return identity.user_id
        return self._runtime.scope.user_id if self._runtime is not None else self._user_id

    @property
    def request_identity(self) -> McpRequestIdentity | None:
        request_identity = self._active_mcp_request_identity()
        if request_identity is not None:
            return request_identity
        scoped_identity = self._request_identity.get()
        if scoped_identity is _NO_REQUEST_USER_SCOPE:
            return None
        return cast("McpRequestIdentity | None", scoped_identity)

    @property
    def client_id(self) -> str | None:
        identity = self.request_identity
        return identity.client_id if identity is not None else None

    @property
    def username(self) -> str | None:
        identity = self.request_identity
        return identity.username if identity is not None else None

    @property
    def auth_source(self) -> str | None:
        identity = self.request_identity
        return identity.auth_source if identity is not None else None

    @property
    def runtime(self) -> Any | None:
        return self._runtime

    @property
    def api_runtime(self) -> Any | None:
        return self._api_runtime

    @property
    def vector_last_failed_at(self) -> float | None:
        if self._runtime is None:
            return None
        return self._runtime.vector_state.last_failed_at

    @property
    def local_vector_last_failed_at(self) -> float | None:
        if self._runtime is None:
            return None
        return self._runtime.local_vector_state.last_failed_at

    def init_runtime(
        self,
        database_dsn: str | None = None,
    ) -> Any:
        """Initialize the MCP runtime immediately."""
        if database_dsn is not None:
            self.database_dsn = database_dsn
        mcp_di.VECTOR_RETRY_INTERVAL_SEC = self._vector_retry_interval_sec
        mcp_di.LOCAL_VECTOR_RETRY_INTERVAL_SEC = self._local_vector_retry_interval_sec
        # Request-scoped overrides are transient and must not leak into the shared runtime.
        self._runtime = mcp_di.build_mcp_runtime(
            database_dsn=self.database_dsn,
            user_id=self._user_id,
        )
        self.database_dsn = self._runtime.database_dsn
        self.logger.info("MCP database connected: %s", self._runtime.database_dsn)
        return self._runtime

    def ensure_runtime(
        self,
        database_dsn: str | None = None,
    ) -> Any:
        if self._runtime is None or (
            database_dsn is not None and database_dsn != self.database_dsn
        ):
            return self.init_runtime(database_dsn=database_dsn)
        return self._runtime

    def set_user_scope(self, user_id: int | None) -> None:
        self._user_id = user_id
        if self._runtime is not None:
            mcp_di.set_mcp_user_scope(self._runtime, user_id)

    def _request_identity_from_request(self, request: Any | None) -> McpRequestIdentity | None:
        if request is None:
            return None
        state = getattr(request, "state", None)
        identity = getattr(state, "mcp_identity", None)
        return identity if isinstance(identity, McpRequestIdentity) else None

    def _active_mcp_request_identity(self) -> McpRequestIdentity | None:
        with contextlib.suppress(ImportError, LookupError):
            from mcp.server.lowlevel.server import request_ctx

            request_context = request_ctx.get()
            request = getattr(request_context, "request", None)
            return self._request_identity_from_request(request)
        return None

    def set_request_identity(
        self,
        identity: McpRequestIdentity | None,
    ) -> contextvars.Token[Any]:
        return self._request_identity.set(identity)

    def reset_request_identity(self, token: contextvars.Token[Any]) -> None:
        self._request_identity.reset(token)

    @contextlib.contextmanager
    def request_identity_scope(self, identity: McpRequestIdentity | None) -> Iterator[None]:
        token = self.set_request_identity(identity)
        try:
            yield
        finally:
            self.reset_request_identity(token)

    async def init_api_runtime(
        self,
        database_dsn: str | None = None,
    ) -> Any:
        """Initialize a write-capable API runtime for trusted MCP aggregation tools."""
        from app.config import load_config
        from app.di.api import build_api_runtime

        if database_dsn is not None:
            self.database_dsn = database_dsn
        cfg = load_config(allow_stub_telegram=True)
        if self.database_dsn is not None and cfg.database.dsn != self.database_dsn:
            cfg = replace(
                cfg,
                database=cfg.database.model_copy(update={"dsn": self.database_dsn}),
            )
        self._api_runtime = await build_api_runtime(cfg)
        self.database_dsn = self._api_runtime.db.config.dsn
        self.logger.info(
            "API runtime connected for MCP aggregation tools: %s",
            self.database_dsn,
        )
        return self._api_runtime

    async def ensure_api_runtime(
        self,
        database_dsn: str | None = None,
    ) -> Any:
        if self._api_runtime is not None and (
            database_dsn is None or database_dsn == self.database_dsn
        ):
            return self._api_runtime
        async with self._api_runtime_lock:
            if self._api_runtime is not None and (
                database_dsn is None or database_dsn == self.database_dsn
            ):
                return self._api_runtime
            return await self.init_api_runtime(database_dsn=database_dsn)

    def request_scope_filters(self, request_model: Any) -> list[Any]:
        filters: list[Any] = [request_model.is_deleted == False]  # noqa: E712
        if self.user_id is not None:
            filters.append(request_model.user_id == self.user_id)
        return filters

    def collection_scope_filters(self, collection_model: Any) -> list[Any]:
        filters: list[Any] = [collection_model.is_deleted == False]  # noqa: E712
        if self.user_id is not None:
            filters.append(collection_model.user_id == self.user_id)
        return filters

    async def init_vector_service(self) -> Any:
        """Initialize (or return cached) runtime-owned vector search service."""
        service = await mcp_di.ensure_mcp_vector_service(self.ensure_runtime())
        if service is None:
            self.logger.warning("Vector store unavailable — semantic_search tool will be disabled")
        else:
            self.logger.info("Vector search service initialised")
        return service

    async def init_local_vector_service(self) -> Any:
        """Initialize (or return cached) runtime-owned local embedding fallback service."""
        service = await mcp_di.ensure_mcp_local_vector_service(self.ensure_runtime())
        if service is None:
            self.logger.warning("Local vector fallback unavailable")
        else:
            self.logger.info("Local vector fallback service initialised")
        return service

    async def aclose(self) -> None:
        if self._api_runtime is not None:
            from app.di.api import close_api_runtime

            api_runtime = self._api_runtime
            self._api_runtime = None
            await close_api_runtime(api_runtime)
        if self._runtime is None:
            return
        runtime = self._runtime
        self._runtime = None
        await mcp_di.close_mcp_runtime(runtime)
