# Bot Commands Reference

## Available Commands

- `/start` or `/help` -- Show help and usage
- `/summarize <URL>` -- Summarize URL immediately
- `/summarize` -- Bot asks for URL in next message
- `/summarize_all <URLs>` -- Process multiple URLs without confirmation
- `/cancel` -- Cancel pending operation
- `/init_session` -- Initialize userbot session via Mini App OTP/2FA flow
- `/digest` -- Generate a digest of subscribed channels now
- `/channels` -- List currently subscribed channels
- `/subscribe @channel` -- Subscribe to a Telegram channel for digests
- `/unsubscribe @channel` -- Unsubscribe from a channel

## Command Processing

- Commands are defined in `app/adapters/telegram/commands.py`
- Routing logic in `app/adapters/telegram/message_router.py`
- State management in `app/adapters/telegram/task_manager.py`
- Processor in `app/adapters/telegram/command_processor.py`
