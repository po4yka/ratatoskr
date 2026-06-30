---
name: adding-telegram-command
description: Add a new Telegram bot command to Ratatoskr via the command registry pattern. Trigger keywords -- new command, bot command, telegram handler, CommandRegistry, command_handlers, register_command, slash command.
version: 2.0.0
allowed-tools: Bash, Read, Write, Edit, Grep
---

# Adding a Telegram Command

Add a new slash command (`/foo`) to the Ratatoskr bot via the existing registry pattern.

## The Registry Pattern

Commands are NOT dispatched by `if text.startswith("/foo")` branches in the router. Instead:

1. Each command is a handler class in `app/adapters/telegram/command_handlers/`
2. The handler is wired into the registry via `CommandRegistry.register_command(prefix, handler)` in `app/adapters/telegram/commands.py`
3. `MessageRouter` walks the registry and dispatches -- it does not know about specific commands

Adding a command means: write the handler, register it with a prefix, add tests. Done.

## Naming Convention

The repo's actual convention (verify with `ls app/adapters/telegram/command_handlers/`):

| Artifact | Pattern | Example |
|---|---|---|
| File | `<name>_handler.py` | `admin_handler.py`, `digest_handler.py`, `init_session_handler.py` |
| Class | `<Name>Handler` | `AdminHandler`, `DigestHandler`, `InitSessionHandler` |
| Base | `HandlerDependenciesMixin` (`base_handler.py`) -- optional; many handlers roll their own `__init__` instead | -- |

The handler does not need to subclass anything specific. The registry accepts any callable or any object implementing the `Command` protocol.

## Steps

### 1. Create the handler

`app/adapters/telegram/command_handlers/foo_handler.py`:

```python
"""/foo command -- one-line description of what it does."""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.telegram.command_handlers.execution_context import (
        CommandExecutionContext,
    )
    from app.config import AppConfig
    from app.db.session import Database

logger = get_logger(__name__)


class FooHandler:
    """Implementation of /foo."""

    def __init__(
        self,
        cfg: AppConfig,
        db: Database,
        response_formatter: ResponseFormatter,
    ) -> None:
        self._cfg = cfg
        self._db = db
        self._formatter = response_formatter

    async def handle(self, ctx: CommandExecutionContext) -> None:
        # ctx carries the Telegram message, args, correlation id, user id.
        await self._formatter.safe_reply(ctx.message, "Foo did the thing.")
```

Match the constructor signature of an existing handler that has the dependencies you need. Look at `admin_handler.py` for a DB-heavy example or `init_session_handler.py` for a multi-step interaction example.

### 2. Register it

Inside the builder in `app/adapters/telegram/commands.py` (find the function that constructs the `CommandRegistry` -- it lives near the top-level bot wiring):

```python
from app.adapters.telegram.command_handlers.foo_handler import FooHandler

# After the registry is constructed and dependencies are available:
foo = FooHandler(cfg=cfg, db=db, response_formatter=response_formatter)
registry.register_command("/foo", foo.handle)         # single prefix
# or
registry.register_command(["/foo", "/foobar"], foo.handle)  # aliases
```

`register_command` accepts either a `Command`-protocol object or a plain async callable -- it auto-wraps callables in `SimpleCommand`. The first argument is the prefix (string) or list of aliases.

### 3. Reply via `ResponseFormatter`

Always go through the `response_formatter` your handler was constructed with -- it centralizes logging, correlation-ID attachment, and the error envelope (`Error ID: <correlation_id>`). Do not call `message.reply(...)` directly.

Common methods (see `ResponseFormatterFacade`): `safe_reply`, `send_error_notification`, etc.

### 4. Write tests

`tests/adapters/telegram/test_foo_handler.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from app.adapters.telegram.command_handlers.foo_handler import FooHandler


@pytest.mark.asyncio
async def test_foo_replies():
    cfg = MagicMock()
    db = MagicMock()
    formatter = MagicMock()
    formatter.safe_reply = AsyncMock()

    handler = FooHandler(cfg=cfg, db=db, response_formatter=formatter)
    ctx = MagicMock()
    ctx.message = MagicMock()

    await handler.handle(ctx)
    formatter.safe_reply.assert_awaited_once()
```

For commands that hit the DB, use `tests/db_helpers_async.py` rather than writing fresh fixtures (per CLAUDE.md rule).

### 5. (Optional) BotFather command list

If the command should appear in Telegram's autocomplete menu, register it once with BotFather -- that's a one-time manual step outside the codebase.

## Access Control

`AccessController` (`app/adapters/telegram/access_controller.py`) gates the entire router on `ALLOWED_USER_IDS`. New commands inherit this gate automatically. For commands that should be public (rare), look at how the digest commands handle exceptions -- they declare an explicit bypass.

## Existing Handlers (Reference)

Look at these as templates (use `ls app/adapters/telegram/command_handlers/` to see the full list):

| File | Pattern |
|---|---|
| `admin_handler.py` | DB-heavy administration; multiple sub-commands |
| `init_session_handler.py` | Multi-step interaction (phone -> OTP -> 2FA) |
| `digest_handler.py` | DB query + formatted reply, uses external `UserbotClient` |
| `rss_handler.py` | CRUD-shaped command family |
| `search_handler.py` | Combines DB lookup + vector search |
| `settings_handler.py` | User preference updates |

## Key Files

- **Handlers**: `app/adapters/telegram/command_handlers/`
- **Registry & wiring**: `app/adapters/telegram/commands.py`
- **Router**: `app/adapters/telegram/message_router.py`
- **Access control**: `app/adapters/telegram/access_controller.py`
- **Reply helper**: `ResponseFormatterFacade` in `app/adapters/external/formatting/`
- **Execution context**: `app/adapters/telegram/command_handlers/execution_context.py`
- **DI container**: `app/di/`

## Important Notes

- `MessageRouter` is the only caller of the registry -- do not invoke handlers directly from elsewhere.
- The prefix string passed to `register_command` includes the leading `/` (e.g., `"/foo"`).
- Errors should include `Error ID: <correlation_id>` in user-visible messages (CLAUDE.md rule). `ResponseFormatter.send_error_notification` does this for you.
- Persist any side effects in the appropriate table (`requests`, `audit_logs`, etc.) -- the observability discipline matters.
- For long-running operations, use the in-process `StreamHub` (`app/adapters/content/streaming/`) so the user sees progress instead of a frozen reply.
- `HandlerDependenciesMixin` (`base_handler.py`) is available if your handler needs nothing beyond `cfg`, `db`, and `response_formatter` -- but most existing handlers declare their own `__init__` for richer dependencies, so inheriting it is optional.
