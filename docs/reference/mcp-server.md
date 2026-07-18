# MCP Server

Ratatoskr exposes an [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server that allows external AI agents (OpenClaw, Claude Desktop, etc.) to search, retrieve, and explore stored article summaries, plus run local trusted aggregation bundles when the server is scoped to a single user.

The server now supports two deployment modes:

- **Local/trusted mode**: stdio or SSE with startup scoping via `MCP_USER_ID` / `--user-id`
- **Hosted public mode**: SSE with request-scoped JWT auth on every HTTP request

This phase evolves the existing MCP server rather than adding a separate MCP gateway. Public identity is resolved from the same access-token model as the REST API, while local stdio/SSE keeps the existing startup-scope behavior.

## Configuration

| Variable | Default | Description |
| ---------- | --------- | ------------- |
| `MCP_ENABLED` | `false` | Enable the MCP server |
| `MCP_TRANSPORT` | `stdio` | Transport: `stdio` or `sse` |
| `MCP_HOST` | `127.0.0.1` | SSE bind address |
| `MCP_PORT` | `8200` | SSE port |
| `MCP_USER_ID` | _(none)_ | Startup user scope for local stdio/SSE deployments (recommended for SSE) |
| `MCP_ALLOW_REMOTE_SSE` | `false` | Allow binding SSE to non-loopback hosts (also disables DNS rebinding protection) |
| `MCP_ALLOW_UNSCOPED_SSE` | `false` | Allow SSE without `MCP_USER_ID` |
| `MCP_ALLOW_UNSCOPED_PRODUCTION` | `false` | Required in addition to `MCP_ALLOW_UNSCOPED_SSE=true` before unscoped SSE can start when `APP_ENV=production`; also allows a non-loopback bind for that intentionally unscoped mode |
| `MCP_ALLOW_UNSCOPED_STDIO` | `false` | Allow stdio without `MCP_USER_ID` |
| `MCP_AUTH_MODE` | `disabled` | Hosted auth mode: `disabled` or `jwt` |
| `MCP_FORWARDED_ACCESS_TOKEN_HEADER` | `X-Ratatoskr-Forwarded-Access-Token` | Trusted-gateway header for forwarding the original access token |
| `MCP_FORWARDED_SECRET_HEADER` | `X-Ratatoskr-MCP-Forwarding-Secret` | Trusted-gateway header carrying the shared forwarding secret |
| `MCP_FORWARDING_SECRET` | _(none)_ | Shared secret required before trusting forwarded access-token headers |
| `MCP_TOOL_RATE_WINDOW_SEC` | `60` | Sliding-window length (seconds) for the per-(operation, tenant) rate limiter (see [Rate limiting](#rate-limiting)) |
| `MCP_TOOL_RATE_LIMIT` | `60` | Max invocations per window for standard read-tier tools and resources |
| `MCP_EXPENSIVE_TOOL_RATE_LIMIT` | `5` | Tighter per-window cap for the billed/expensive tool tier |

See `docs/reference/environment-variables.md` for full config reference.

### Rate limiting

Every MCP tool and resource call passes through an in-process rate limiter keyed by `(operation, tenant)`, so one caller cannot drive unbounded scrape / LLM / embedding cost and, in hosted JWT mode, cannot starve other tenants of a shared budget. Two tiers apply within the `MCP_TOOL_RATE_WINDOW_SEC` window: standard read-tier operations use `MCP_TOOL_RATE_LIMIT`, while the expensive tier (`create_aggregation_bundle`, `promote_to_library`, `semantic_search`, `hybrid_search`, `find_similar_articles`) uses the tighter `MCP_EXPENSIVE_TOOL_RATE_LIMIT` because each triggers a scrape+LLM fan-out or a per-call embedding request. All three knobs are clamped to a minimum of 1, and a non-integer value falls back to its default. The limiter is process-local: a horizontally-scaled deployment needs a shared (Redis) limiter, tracked separately.

## Running

**stdio mode** (default -- for OpenClaw / Claude Desktop, requires startup user scope):

```bash
MCP_USER_ID=123456 python -m app.cli.mcp_server
```

**SSE mode** (HTTP-based integrations):

```bash
python -m app.cli.mcp_server --transport sse --user-id 12345
```

**Hosted public SSE mode** (request-scoped auth, no startup user scope required):

```bash
python -m app.cli.mcp_server --transport sse --auth-mode jwt --allow-remote-sse
```

SSE safety defaults:

- Binds to loopback (`127.0.0.1`) unless you explicitly enable remote bind.
- Requires either startup scoping (`MCP_USER_ID` / `--user-id`) or hosted auth (`MCP_AUTH_MODE=jwt`).
- Unscoped SSE requires `MCP_ALLOW_UNSCOPED_SSE=true`; when `APP_ENV=production`, startup also requires `MCP_ALLOW_UNSCOPED_PRODUCTION=true`.
- Unscoped SSE without `MCP_ALLOW_UNSCOPED_PRODUCTION=true` is forced to `127.0.0.1` even if a non-loopback `MCP_HOST` was configured.
- DNS rebinding protection is enabled by default; when `allow_remote_sse` is set, it is disabled so Docker-internal hostnames (e.g. `ratatoskr-mcp:8200`) are accepted.

stdio safety defaults:

- Requires startup scoping (`MCP_USER_ID` / `--user-id`) by default.
- For maintenance-only local runs that intentionally need all-user reads, pass `--allow-unscoped-stdio` or set `MCP_ALLOW_UNSCOPED_STDIO=true`.

Hosted auth behavior:

- `MCP_AUTH_MODE=jwt` validates the same access tokens used by the REST API.
- Direct `Authorization: Bearer <token>` requests are supported.
- A trusted gateway may forward the original access token via `MCP_FORWARDED_ACCESS_TOKEN_HEADER`, but only when `MCP_FORWARDING_SECRET` is configured and the gateway also sends `MCP_FORWARDED_SECRET_HEADER`.
- Every tool and resource call resolves the effective user from the authenticated HTTP request. The startup scope is preserved for local mode and does not leak across hosted requests.

Aggregation safety defaults:

- Aggregation MCP tools require an effective scoped user, either from startup scope or hosted request auth.
- Aggregation bundle creation now reuses the request-scoped `client_id` when hosted auth is enabled.
- Aggregation requires PostgreSQL write permission and may require writable `/data` for extraction artifacts. Database permission comes from `DATABASE_URL` and PostgreSQL grants; a read-only host-data mount does not make PostgreSQL read-only.

User scoping modes:

- Local mode keeps using the startup scope from `MCP_USER_ID` / `--user-id`.
- Hosted public mode binds `request.state.mcp_identity` from each authenticated SSE request, and the MCP runtime resolves user scope from that request context first.
- Read tools and write tools both use the same request-scoped identity path, so public requests do not depend on process-wide user configuration.
- If no request-scoped identity is available, MCP falls back to the existing startup scope behavior.

## Integration Workflows

### Local stdio Client

For Claude Desktop or another same-machine agent, start MCP in stdio mode with a single trusted startup scope:

```bash
MCP_USER_ID=123456 python -m app.cli.mcp_server
```

Typical local aggregation workflow:

1. Call `create_aggregation_bundle(items, lang_preference, metadata)`
2. The create call blocks until the bundle reaches a terminal status or fails
3. Use `get_aggregation_bundle(session_id)` or `ratatoskr://aggregations/{session_id}` to re-open the persisted result later
4. Use `list_aggregation_bundles(limit, offset, status)` or `ratatoskr://aggregations/recent` for recent context

This mode is for one trusted user at a time. Do not expose it publicly.

### Hosted Public SSE Client with Direct Bearer Auth

First mint an access token through the API:

```bash
curl -X POST https://ratatoskr.example.com/v1/auth/secret-login \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": 123456,
    "client_id": "mcp-agent-v1",
    "secret": "<plaintext-secret>"
  }'
```

Then point the MCP client at `https://ratatoskr.example.com/sse` and send:

```http
Authorization: Bearer <access_token>
```

Hosted request-scoped mode requirements:

- the client must send the bearer token on SSE requests
- `MCP_AUTH_MODE=jwt` must be enabled on the server
- `MCP_USER_ID` should remain unset in hosted mode so the request identity is the source of truth

### Hosted Public SSE Behind a Trusted Gateway

If the MCP client cannot attach bearer headers directly, terminate client auth at a trusted gateway and forward the original access token to Ratatoskr:

```http
X-Ratatoskr-Forwarded-Access-Token: <original-access-token>
X-Ratatoskr-MCP-Forwarding-Secret: <shared-forwarding-secret>
```

Server-side requirements:

- `MCP_AUTH_MODE=jwt`
- `MCP_FORWARDING_SECRET=<shared-forwarding-secret>`
- matching forwarded header names if you override the defaults

Gateway guidance:

- forward the original bearer token, not a raw user ID
- protect the forwarding secret like any other server-to-server credential
- strip untrusted inbound copies of the forwarded headers before re-adding your trusted values

### Aggregation Tool Flow

Current MCP aggregation execution is synchronous from the caller's perspective: `create_aggregation_bundle(...)` waits for extraction plus synthesis and normally returns a terminal session (`completed`, `partial`, or `failed`). Use the follow-up reads when you need to revisit a past run, recover after a network interruption, or browse history:

1. `create_aggregation_bundle(...)`
2. `get_aggregation_bundle(session_id)` or `ratatoskr://aggregations/{session_id}`
3. `list_aggregation_bundles(...)` or `ratatoskr://aggregations/recent`

Useful fields on session reads:

- `status`
- `progress.completionPercent`
- `successful_count` / `failed_count`
- `failure`
- lifecycle timestamps such as `queued_at` and `completed_at`

## Docker Deployment (SSE)

The `ops/docker/docker-compose.yml` file includes three opt-in MCP profiles so the SSE server is not started by a plain `docker compose up`.

### Local SSE Profile with Read-Only Host Data

Start the local/trusted profile explicitly:

```bash
MCP_USER_ID=12345 docker compose -f ops/docker/docker-compose.yml --profile mcp up -d mcp
```

Key design decisions:

- **Opt-in profile** (`profiles: ["mcp"]`) -- keeps MCP disabled during the default compose startup path.
- **Read-only host-data mount** (`../../data:/data:ro`) -- prevents writes to local sessions/media/export paths. PostgreSQL access still follows `DATABASE_URL` and its grants. Use `mcp-write` when a trusted workflow needs durable files as well as database writes.
- **Explicit user scoping** (`MCP_USER_ID`) -- required for SSE unless you also opt into `MCP_ALLOW_UNSCOPED_SSE=true`.
- **Production unscoped gate** (`MCP_ALLOW_UNSCOPED_PRODUCTION=true`) -- required in addition to `MCP_ALLOW_UNSCOPED_SSE=true` when `APP_ENV=production`. Without this production gate, startup exits non-zero; outside production, unscoped SSE is forced to `127.0.0.1`.
- **`MCP_ALLOW_REMOTE_SSE=true`** -- required because `0.0.0.0` is non-loopback inside Docker. This also disables the MCP SDK's DNS rebinding protection so that Docker-internal hostnames (`ratatoskr-mcp`, `ratatoskr-mcp:8200`) are accepted in the `Host` header.
- **Loopback port binding** (`127.0.0.1:8200`) -- prevents direct external access from the host network.

### Writable Trusted SSE Profile

Use the dedicated writable trusted profile when the local/scoped MCP server should be allowed to create aggregation bundles:

```bash
MCP_USER_ID=12345 docker compose -f ops/docker/docker-compose.yml --profile mcp-write up -d mcp-write
```

Key differences from the read-only profile:

- the database service is available for writes, so aggregation tools can persist sessions and items
- the service binds to `127.0.0.1:8201`
- startup scoping via `MCP_USER_ID` is still required

### Hosted JWT SSE Profile

Use the hosted profile when you want request-scoped public MCP over SSE:

```bash
docker compose -f ops/docker/docker-compose.yml --profile mcp-public up -d mcp-public
```

This profile:

- leaves `MCP_USER_ID` unset so request-scoped auth is the source of truth
- enables `MCP_AUTH_MODE=jwt`
- mounts the database read-write so authenticated hosted requests can use aggregation write tools
- exposes the service on `127.0.0.1:8202` for reverse-proxy or local gateway attachment

If you terminate client auth at a trusted gateway, configure `MCP_FORWARDING_SECRET` and forward the original access token instead of forwarding a raw user ID.

### Connecting from another Docker Compose project

To connect from a service in a different compose project (e.g. OpenClaw), attach that service to the same Docker network and point the MCP client at `http://ratatoskr-mcp:8200/sse`.

Example mcporter config:

```json
{
  "mcpServers": {
    "ratatoskr": {
      "description": "Personal knowledge base - article summaries, semantic search, collections",
      "baseUrl": "http://ratatoskr-mcp:8200/sse"
    }
  }
}
```

## Tools (28)

| Tool | Description |
| ------ | ------------- |
| `create_aggregation_bundle(items, lang_preference, metadata)` | Create and run a mixed-source aggregation bundle for the scoped MCP user |
| `get_aggregation_bundle(session_id)` | Get one persisted aggregation bundle by session ID |
| `list_aggregation_bundles(limit, offset, status)` | List aggregation bundles for the scoped MCP user |
| `check_source_supported(url, source_kind_hint)` | Classify whether a URL fits the public aggregation source contract |
| `search_articles(query, limit)` | Full-text search across titles, summaries, tags, entities |
| `get_article(summary_id)` | Full summary details by ID |
| `list_articles(limit, offset, is_favorited, lang, tag)` | Paginated article list with filters |
| `get_article_content(summary_id)` | Original crawled content (markdown/text, capped at 50k chars) |
| `get_stats()` | Database statistics: counts, languages, top tags, request types |
| `find_by_entity(entity_name, entity_type, limit)` | Find articles mentioning a person, org, or location |
| `x_search(query, category, limit)` | Full-text search across ingested X bookmarks |
| `ask_my_archive(query, max_sources)` | Bounded citation-first research across summaries, repositories, X bookmarks, git mirrors, highlights, and notes |
| `list_collections(limit, offset)` | List top-level article collections |
| `get_collection(collection_id, include_items, limit)` | Collection details with articles |
| `list_videos(limit, offset, status)` | List YouTube video downloads with metadata |
| `get_video_transcript(video_id)` | Video transcript text (capped at 50k chars) |
| `check_url(url)` | Check if a URL has already been processed (uses SHA-256 dedup) |
| `semantic_search(description, limit, language, min_similarity, rerank, include_chunks)` | Vector similarity search via Qdrant (falls back to keyword) |
| `hybrid_search(query, limit, language, min_similarity, rerank)` | Combined keyword + semantic retrieval into a single ranked list |
| `find_similar_articles(summary_id, limit, min_similarity, rerank, include_chunks)` | Find articles semantically similar to an existing summary |
| `list_signal_sources(limit)` | List signal sources visible to the scoped MCP user |
| `list_user_signals(limit, status)` | List scored signal candidates visible to the scoped MCP user |
| `update_signal_feedback(signal_id, action)` | Write feedback for a signal candidate (`like`, `dislike`, `skip`, `queue`, `hide_source`, `boost_topic`) |
| `promote_to_library(source_type, source_id)` | Promote a queued signal or X bookmark into a durable summary request |
| `set_signal_source_active(source_id, is_active)` | Enable or disable a subscribed signal source for the scoped MCP user |
| `vector_health()` | Check Qdrant availability and fallback readiness |
| `vector_index_stats(scan_limit)` | Index coverage stats between Postgres summaries and Qdrant |
| `vector_sync_gap(max_scan, sample_size)` | Report sync gaps between Postgres summaries and Qdrant index |

## Resources (17)

| URI | Description |
| ----- | ------------- |
| `ratatoskr://aggregations/recent` | 10 most recent aggregation bundles for the scoped MCP user |
| `ratatoskr://aggregations/{session_id}` | One persisted aggregation bundle for the scoped MCP user |
| `ratatoskr://articles/recent` | 10 most recent article summaries |
| `ratatoskr://articles/favorites` | All favorited summaries |
| `ratatoskr://articles/unread` | Up to 20 unread summaries |
| `ratatoskr://stats` | Database statistics snapshot |
| `ratatoskr://tags` | All topic tags with counts |
| `ratatoskr://entities` | Aggregated people, organizations, locations |
| `ratatoskr://domains` | Source domains with article counts |
| `ratatoskr://collections` | Top-level collections with item counts |
| `ratatoskr://videos/recent` | 10 most recent completed video downloads |
| `ratatoskr://processing/stats` | LLM call counts, token usage, model breakdown, video stats |
| `ratatoskr://vector/health` | Qdrant health and fallback status |
| `ratatoskr://vector/index-stats` | Qdrant index coverage statistics |
| `ratatoskr://vector/sync-gap` | Sync gap report between Postgres and Qdrant |
| `ratatoskr://signals/recent` | Recent scored signal candidates for the scoped MCP user |
| `ratatoskr://sources` | Signal source catalog |

## Graceful Degradation

- Qdrant is optional. When unavailable, `semantic_search` and `hybrid_search` fall back to keyword-based `search_articles`. The `vector_*` tools report availability status rather than failing.
- Signal scoring requires Qdrant. The REST signal health endpoint reports readiness before the worker runs, and MCP exposes signal reads/writes without silently changing the scoring pipeline.
- The MCP server logs to stderr (required by stdio transport) and never writes to stdout outside of MCP protocol messages.
- Unscoped SSE emits `ratatoskr_mcp_unscoped_enabled{app_env="<env>"}` as `1` and logs an error-level startup record with the resolved scope and bind host. The bundled Prometheus rules alert when that gauge is `1` in production for more than five minutes.

## Implementation

Source: `app/mcp/server.py`

The server uses [FastMCP](https://github.com/modelcontextprotocol/python-sdk) and connects to the same Postgres database as the main bot. Read tools use the existing read-scoped MCP runtime; aggregation tools lazily initialize the normal API runtime so they can reuse the standard extraction and synthesis workflow.

For hosted auth, the repo wraps the FastMCP SSE ASGI app with repo-owned HTTP auth middleware instead of relying on process-wide scope. That middleware validates Ratatoskr JWT access tokens, stores `mcp_identity` on the Starlette request, and `McpServerContext` resolves the effective user from the active low-level MCP request context before falling back to local startup scope.
