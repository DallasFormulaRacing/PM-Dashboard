# PM Dashboard backend

FastAPI API plus the Discord messaging pipeline (Celery worker and beat). The design doc covers the
architecture; this file covers running the code.

## Layout

| Path | What it is |
| --- | --- |
| `app/models.py` | Tables: users, tasks, announcements, discord_channels, outbound_messages (the outbox) |
| `app/messaging/service.py` | What endpoints call: `post_announcement`, `create_task`, `assign_task`, `task_changed` |
| `app/messaging/outbox.py` | Writes outbox rows; queues them only after the transaction commits |
| `app/messaging/payloads.py` | Discord embed builders and limit checks |
| `app/messaging/condense.py` | Long-note handling; `Summarizer` hook for future LLM summaries (truncates for now) |
| `app/discord/` | Minimal Discord REST client (no gateway; one-way) |
| `app/worker/delivery.py` | Claim, send and record one message: DMs, webhook posts, fallback, edits, retries |
| `app/worker/sweeper.py` | Beat job: re-queues missed, scheduled, retried or stuck rows |
| `app/worker/tasks.py` | Celery tasks `discord.deliver` and `discord.sweep` |
| `app/api/main.py` | FastAPI app; only `/healthz` until the endpoint specs arrive |
| `migrations/` | Alembic |
| `deploy/` | systemd units, Caddyfile, deploy script for the VM |

## How messages are sent

- **Announcements** post to the channel for their purpose, through a webhook under the sender's
  name and avatar. The worker creates the webhook on first use. If the bot lacks Manage Webhooks,
  it posts as the bot with "From <sender>" on the embed.
- **Task assignments** are DMs from the bot with "From <sender>" on the embed. If the assignee's
  DMs are blocked, the message goes to `assignments_fallback` with an @mention.
- **Task edits and reassignments** edit the earlier Discord message in place.
- Nothing is read back from Discord.

## Local setup

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env    # then fill in values
.venv/bin/alembic upgrade head
.venv/bin/python -m app.cli set-channel announcements <guild_id> <channel_id>
.venv/bin/python -m app.cli set-channel assignments_fallback <guild_id> <channel_id>
```

Run each in its own terminal:

```bash
.venv/bin/uvicorn app.api.main:app --reload
.venv/bin/celery -A app.worker.tasks:celery_app worker -Q discord --pool=solo --loglevel=INFO
.venv/bin/celery -A app.worker.tasks:celery_app beat --loglevel=INFO
```

On macOS, use `--pool=solo` for the worker. Celery's default prefork pool fails there because
macOS spawns worker processes instead of forking them. The Linux VM uses the default pool
(see `deploy/systemd/pm-worker.service`).

## Tests

```bash
.venv/bin/python -m pytest
```

Tests use in-memory SQLite by default. To run them against Postgres, which exercises JSONB and
`SKIP LOCKED`, set `TEST_DATABASE_URL=postgresql+psycopg://...`. **The tests drop and recreate
every table in that database**, so point it at a throwaway one.

## Discord bot permissions

The bot needs View Channel, Send Messages, Embed Links, Read Message History and Manage Webhooks.
Read Message History lets a retried webhook post check whether its earlier attempt went through.
Manage Webhooks lets channel posts appear under the sender's name.

## Wiring endpoints later

Each endpoint opens a session, calls the service function, and commits once:

```python
with get_session_factory()() as session:
    task = service.create_task(session, creator=current_user, title=..., assignee=assignee)
    session.commit()  # outbox rows are queued to Celery here
```
