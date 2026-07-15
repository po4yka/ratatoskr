---
name: adding-telegram-command
description: Add a Telegram bot command through TelegramCommandContribution routing. Trigger keywords -- new command, bot command, telegram handler, command_handlers, TelegramCommandContribution, TextCommandRoute, slash command.
version: 3.0.0
allowed-tools: Bash, Read, Write, Edit, Grep
---

# Adding a Telegram Command

Add a slash command (`/foo`) through the contribution-based dispatcher. `MessageRouter` delegates command handling to `TelegramCommandDispatcher`; command wiring belongs in the DI layer.

## Current dispatch pattern

1. Implement the handler in `app/adapters/telegram/command_handlers/`.
2. Construct it in `app/di/telegram_commands.py::build_command_dispatcher_deps`.
3. Add a `TelegramCommandContribution` with the appropriate route dataclass.
4. Test the handler and the assembled dispatcher routes.

Do not add command-specific branches to `MessageRouter`.

## Handler

Handlers normally accept a `CommandExecutionContext` and reply through `ctx.response_formatter`:

```python
class FooHandler:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def handle(self, ctx: CommandExecutionContext) -> None:
        await ctx.response_formatter.safe_reply(ctx.message, "Foo did the thing.")
```

Match the constructor and error-handling pattern of a nearby handler with similar dependencies. Persist side effects through the existing repository/application layer.

## Wiring

In `app/di/telegram_commands.py`, import and construct the handler before the `contributions` tuple, then add a route:

```python
foo_handler = FooHandler(db=db)

contributions = (
    # existing contributions ...
    TelegramCommandContribution(
        name="foo",
        pre_summarize_text=(
            TextCommandRoute(
                "/foo",
                _build_text_handler(context_factory, foo_handler.handle),
            ),
        ),
    ),
)
```

Choose the route family by the required handler signature:

- `UidCommandRoute` + `_build_uid_handler` for commands that do not need the full text.
- `TextCommandRoute` + `_build_text_handler` for commands that parse text or arguments.
- `AliasCommandRoute` + `_build_alias_handler` for a family of aliases.

`merge_command_contributions()` preserves declaration order and builds the immutable `TelegramCommandRoutes` consumed by the dispatcher.

## Tests

- Unit-test the handler with an explicit `CommandExecutionContext` and mocked collaborators.
- Extend `tests/test_command_dispatcher.py` or `tests/test_telegram_dispatcher_wiring.py` when route ordering or contribution assembly changes.
- For database behavior, reuse `tests/db_helpers_async.py`.

Run the smallest relevant tests first, then the dispatcher tests:

```bash
python -m pytest tests/test_command_dispatcher.py tests/test_telegram_dispatcher_wiring.py -q
```

## Key files

- Handlers: `app/adapters/telegram/command_handlers/`
- Route dataclasses and merge: `app/adapters/telegram/command_dispatch/routes.py`
- Dispatcher: `app/adapters/telegram/command_dispatcher.py`
- DI and route contributions: `app/di/telegram_commands.py`
- Execution context: `app/adapters/telegram/command_handlers/execution_context.py`
- Access control: `app/adapters/telegram/access_controller.py`

User-visible errors must retain `Error ID: <correlation_id>`. Use the existing response formatter and exception helpers instead of replying directly through Telethon.
