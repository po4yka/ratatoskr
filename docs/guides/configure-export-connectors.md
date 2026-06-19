# Configure Export Connectors

Ratatoskr can export newly created summaries to Notion, Readwise, and a local Obsidian vault. Export integrations are per-user, disabled by default, and only run when an integration is explicitly enabled.

## API Surface

- `GET /v1/export-integrations` lists configured integrations. Tokens are never returned; responses expose only `tokenConfigured`.
- `POST /v1/export-integrations` creates an integration. `enabled` defaults to `false`.
- `PATCH /v1/export-integrations/{integration_id}` updates config, rotates the token, or toggles `enabled`.
- `DELETE /v1/export-integrations/{integration_id}` revokes the stored token and disables the integration.
- `GET /v1/export-integrations/{integration_id}/deliveries` lists success and failure logs.
- `POST /v1/export-integrations/{integration_id}/test?summary_id=<id>` sends one owned summary to that integration and writes a delivery log.

All API tokens are encrypted at rest with the same Fernet token crypto used by GitHub integrations. Configure `GITHUB_TOKEN_ENCRYPTION_KEY` before enabling Notion or Readwise connectors.

## Notion

Create a Notion internal integration, copy its secret token, share the target database with that integration, and copy the database ID. Create the connector with provider `notion`, `apiToken`, and `config.database_id`.

```json
{
  "provider": "notion",
  "name": "Knowledge DB",
  "apiToken": "secret_...",
  "config": { "database_id": "notion-database-id" },
  "enabled": false
}
```

Ratatoskr creates a page in the configured database with title, source URL, TL;DR, summary text, tags, and highlights. To revoke access, delete or rotate the Notion integration token in Notion, then call `DELETE /v1/export-integrations/{id}` or `PATCH` with a new `apiToken`.

## Readwise

Create a Readwise access token and create the connector with provider `readwise` and `apiToken`.

```json
{
  "provider": "readwise",
  "name": "Readwise",
  "apiToken": "readwise-token",
  "config": {},
  "enabled": false
}
```

Ratatoskr sends key ideas or extractive quotes as highlights with the summary as the note, the article title, source URL, and topic tags. To revoke access, delete or rotate the token in Readwise, then call `DELETE /v1/export-integrations/{id}` or `PATCH` with a new `apiToken`.

## Obsidian

Obsidian export is local-first for self-hosted deployments. Mount the vault into the Ratatoskr container or host filesystem, then configure provider `obsidian` with `config.vault_path` and optional `config.folder`.

```json
{
  "provider": "obsidian",
  "name": "Local Vault",
  "config": { "vault_path": "/data/obsidian", "folder": "Ratatoskr" },
  "enabled": false
}
```

Ratatoskr writes one Markdown file per summary. Filenames are sanitized and path-checked so the generated file cannot escape the configured vault path. To revoke access, disable or delete the integration and unmount the vault path from the deployment.

## Delivery Logs

Every export attempt writes an `export_delivery_logs` row with provider, event type, summary ID, response status/body, duration, success flag, and error text. Failures do not block summary persistence or vector indexing. Use `GET /v1/export-integrations/{integration_id}/deliveries` to inspect delivery history.
