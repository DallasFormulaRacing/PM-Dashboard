# Discord Communication Pipeline

Sends **announcements** and **task assignments** from the PM dashboard to Discord.

**Status (2026-10-28):**
- The pipeline is built in `backend/` and has been tested end to end against a real Discord server.
- API endpoints aren't written yet; they're waiting on the endpoint specs. Until then, everything can be driven from the terminal (see [Testing from the terminal](#testing-from-the-terminal)).

## Goals

- Send a Discord message automatically when a task is assigned, changed or reassigned, or an announcement is posted.
- Never lose a message, and never send the same message twice.
- Show who sent each message.
- Track delivery status (sent, failed, not linked, DMs blocked) so the dashboard can display it.
- One-way only: dashboard → Discord. Nothing is read back from Discord: no replies, reactions or slash commands.

## How messages look in Discord

| Message | Where it goes | Who it appears to come from |
| --- | --- | --- |
| Announcement | The channel registered for its purpose (default `announcements`) | **The sender:** their name and avatar, posted through a channel webhook. Discord adds a small **APP** badge |
| Task assigned | DM to the assignee | **The bot**, with "From <sender>" and the sender's avatar at the top of the embed |
| Task assigned, assignee's DMs blocked | `assignments_fallback` channel with an @mention | The sender, through a webhook |
| Task edited | The assignee's existing message, **edited in place** | Unchanged |
| Task reassigned or unassigned | The previous assignee's message is edited to "Reassigned to X." / "You were unassigned"; the new assignee gets a fresh DM | Unchanged / bot |

**Why DMs come from the bot:** Discord doesn't let bots or webhooks send a DM as another person. The only way would be logging in with that person's own account token (a "self-bot"), which breaks Discord's Terms of Service. A webhook post as the person, plus an @mention, is the closest allowed option. That's already how the fallback path works.

**Fallbacks:** if the bot can't create a webhook (missing Manage Webhooks permission), channel posts go out from the bot instead, with "From <sender>" on the embed.

**Long notes:** task descriptions and announcement bodies over 1,500 characters (`SUMMARY_THRESHOLD_CHARS`) are cut off with a "Read the full note" link to the dashboard. A `Summarizer` hook exists for LLM summaries later. No LLM is called yet, and `tasks.summary` / `announcements.summary` are reserved for cached summaries (see [Long-note summaries](#long-note-summaries-design-only)).

## Hosting

Vercel hosts only the React frontend, as static files. Everything in Python runs on one VM.

| Component | Host |
|---|---|
| React dashboard | Vercel (static build) |
| Python API (FastAPI) | **VM**: gunicorn + uvicorn workers behind Caddy (HTTPS) |
| Celery worker + Celery beat | **Same VM**, run with systemd |
| Postgres | Managed (Neon or Supabase) |
| Celery broker | **Redis Cloud** (managed, TLS + password) |

- **Frontend to API:**
  - The browser calls the API over HTTPS at `api.<your-domain>`. The frontend reads that URL from a build-time env var (`VITE_API_URL`).
  - CORS on the API allows only the origins in `FRONTEND_ORIGINS`.
- **Region:** use the same cloud region for the VM, Postgres and Redis Cloud (for example AWS `us-east-1`).
- **VM size:** start with 2 vCPU and 2–4 GB RAM.
- **No gateway bot:** the pipeline only uses Discord's REST API with the bot token. No connection to Discord's gateway is needed.

## Architecture

```mermaid
flowchart LR
    UI["React dashboard<br/>(Vercel, static)"] -->|"HTTPS"| API

    subgraph VM["App VM (Caddy + systemd)"]
        API["FastAPI<br/>(gunicorn/uvicorn)"]
        BEAT["Celery beat<br/>sweep every 30 s"]
        W["Celery worker"]
    end

    subgraph TX["Single DB transaction"]
        direction TB
        T["tasks / announcements"]
        O["outbound_messages<br/>status = pending"]
    end

    API -->|"service.*()"| TX
    API -->|"after commit:<br/>send_task('discord.deliver', id)"| R[("Redis Cloud<br/>broker only")]
    PG[("Postgres<br/>(managed)")] --- TX

    BEAT -->|"re-queue missed, scheduled,<br/>long-retry, stuck rows"| R
    BEAT -.reads.-> PG

    R --> W
    W -->|"claim row, record result"| PG
    W -->|"bot token"| D["Discord REST API"]
    W -->|"webhook token"| D
    D --> CH["#announcements<br/>(as the sender)"]
    D --> DM["User DMs<br/>(from the bot)"]
    D -."error 50007".-> FB["#assignments fallback<br/>@mention, as the sender"]
```

## Message lifecycle

```mermaid
stateDiagram-v2
    [*] --> pending: service writes outbox row
    pending --> sending: worker claims row (conditional UPDATE)
    sending --> sent: Discord 2xx, message id saved
    sending --> pending: 429 (no attempt used) / 5xx / network error (attempt +1, backoff)
    sending --> failed: permanent error (not_linked, channel_not_configured, 4xx)
    sending --> skipped: nothing to edit (unassign with no earlier message)
    sending --> dead: 5th failed attempt
    sending --> pending: sweeper releases rows stuck > 5 min
    sent --> [*]
    failed --> [*]
    skipped --> [*]
    dead --> [*]
```

## Task assignment sequence

```mermaid
sequenceDiagram
    participant UI as Dashboard / CLI
    participant API
    participant DB as Postgres
    participant Q as Redis/Celery
    participant W as Worker
    participant DC as Discord

    UI->>API: assign task
    API->>DB: BEGIN; insert task; insert outbound_messages(pending); COMMIT
    API->>Q: after commit: send_task("discord.deliver", outbox_id)
    API-->>UI: 200 (delivery pending)
    Q->>W: deliver(outbox_id)
    W->>DB: UPDATE ... SET status=sending WHERE id=? AND status=pending AND available_at<=now
    alt assignee has no discord_user_id
        W->>DB: status=failed, last_error=not_linked
    else linked
        W->>DC: open DM channel (cached on users.discord_dm_channel_id)
        W->>DC: POST message (embed, nonce, enforce_nonce)
        alt 2xx
            W->>DB: status=sent, discord_message_id, delivered_via=dm
        else 50007 DMs blocked
            W->>DC: webhook post in #assignments as sender, content "<@user>"
            W->>DB: status=sent, used_fallback=true
        else 429
            W->>DB: status=pending, available_at=now+retry_after (attempts unchanged)
            W->>Q: countdown retry
        else 5xx / timeout
            W->>DB: status=pending, attempts+1, backoff
            W->>Q: countdown retry (short delays) or leave for sweeper
        end
    end
```

## Code layout

| Path | What it does |
| --- | --- |
| `backend/app/models.py` | SQLAlchemy models and enums |
| `backend/app/messaging/service.py` | What endpoints call: `post_announcement`, `create_task`, `assign_task`, `task_changed`. None of them commit; the caller owns the transaction |
| `backend/app/messaging/outbox.py` | Adds outbox rows; queues them to Celery only after commit (SQLAlchemy `after_commit` hook) |
| `backend/app/messaging/payloads.py` | Embed builders, Discord limit checks, webhook username cleanup |
| `backend/app/messaging/condense.py` | Long-note truncation and the `Summarizer` hook |
| `backend/app/discord/client.py` | Minimal Discord REST client with rate-limit handling |
| `backend/app/worker/delivery.py` | `Deliverer`: claim → send/edit → record, plus error handling |
| `backend/app/worker/sweeper.py` | The beat job |
| `backend/app/worker/tasks.py` | Celery tasks `discord.deliver` and `discord.sweep` |
| `backend/app/celery_client.py` | Celery config shared by API and worker |
| `backend/app/api/main.py` | FastAPI app; only `/healthz` so far |
| `backend/app/cli.py` | Admin and testing commands |
| `backend/migrations/` | Alembic migrations (`0001` = initial schema) |
| `backend/deploy/` | systemd units, Caddyfile, `deploy.sh` |
| `backend/tests/` | pytest suite (43 tests; Discord mocked with respx) |

## Data model

**users**: `id`, `name`, `email` (unique), `avatar_url`, `discord_user_id` (unique, set by account linking), `discord_dm_channel_id` (cached), `created_at`

**tasks**: `id`, `title`, `description`, `summary` (reserved), `assignee_id`, `created_by`, `priority` (`low`/`medium`/`high`), `status` (`todo`/`in_progress`/`done`), `due_at`, `created_at`, `updated_at`

**announcements**: `id`, `title`, `body`, `summary` (reserved), `channel_purpose` (default `announcements`), `scheduled_for`, `created_by`, `created_at`

**discord_channels**: `purpose` (PK: `announcements`, `assignments_fallback`, or any other name), `guild_id`, `channel_id`, `webhook_id`, `webhook_token`. The worker creates the webhook on first use. **The webhook token is a credential:** anyone holding it can post in that channel. Encrypting it is a to-do.

**worker_heartbeats**: `name`, `beat_at`. Beat writes `beat` on every sweep.

**outbound_messages** (the queue's source of truth)

| Column | Notes |
|---|---|
| `id` | Passed to Celery as the only argument |
| `kind` | `task_assigned`, `task_updated`, `task_unassigned`, `announcement` |
| `ref_type`, `ref_id` | The task or announcement this is about |
| `sender_id` | The dashboard user who caused it; shown as the author in Discord |
| `target_type` | `dm` or `channel` |
| `recipient_user_id` | For DMs |
| `channel_purpose` | For channel posts |
| `payload` | JSON(B): `{"embed": {...}, "sender": {"name", "avatar_url"}}`, built when the row is written |
| `status` | `pending`, `sending`, `sent`, `failed`, `dead`, `skipped` |
| `attempts`, `last_error` | |
| `nonce` | 24 hex chars; Discord drops duplicate bot posts with the same nonce |
| `discord_message_id`, `discord_channel_id` | Saved on success; used to edit the message later |
| `delivered_via` | `dm`, `bot_channel` or `webhook` (decides how a later edit is made) |
| `used_fallback` | True if DMs were blocked and it went to the fallback channel |
| `available_at` | Earliest send time: now, the scheduled time, or the next retry |
| `enqueued_at` | Last time it was put on the queue (keeps the sweeper from double-queuing) |
| `locked_at` | When a worker claimed it (used to detect stuck rows) |
| `created_at`, `sent_at` | |

Indexes: `(status, available_at)` for the sweeper; `(ref_type, ref_id)` for status lookups and edits.

## Worker behaviour

**Announcement**
1. Look up the channel by purpose. If it's missing, the row fails with `channel_not_configured: <purpose>`.
2. Make sure the channel has a webhook (create one on first use). Post with `username` and `avatar_url` set to the sender's, and with the "From" author line removed, because the post itself shows the sender.
3. If webhook creation is forbidden (50013/50001), post as the bot with nonce and `enforce_nonce`.

**Task assigned**
1. If the assignee has no `discord_user_id`, the row fails with `not_linked`, without calling Discord.
2. Open or reuse the DM channel, then post the embed with a nonce. If the cached DM channel is gone, reopen it once.
3. On error 50007, post in `assignments_fallback` with `content: "<@id>"` and `allowed_mentions.users = [id]`.

**Task updated:** edit the latest `sent` message about this task to this person (PATCH; the webhook edit endpoint for webhook posts). If it was deleted (404/10008), send a new message instead.

**Task unassigned:** edit the previous assignee's latest message. If there's nothing to edit, mark the row `skipped`.

**Every message:**
- `allowed_mentions` is `{"parse": []}` unless a specific user is mentioned, so nobody can trigger `@everyone`.
- The embed footer carries `PM Dashboard · #<first 8 of outbox id>`.

## Reliability

| Mechanism | What it prevents | How |
| --- | --- | --- |
| Outbox pattern | A task saved without its message, or a message for a rolled-back task | Rows are written in the caller's transaction and queued only in `after_commit`; a rollback discards them |
| Row claim | Two workers sending the same row | `UPDATE ... WHERE id=? AND status='pending' AND available_at<=now RETURNING id` |
| Discord nonce | Duplicate bot posts after a timeout that actually succeeded | `nonce` + `enforce_nonce: true` |
| Webhook marker check | Duplicate webhook posts (webhooks don't support nonces) | On a retry, read the last 50 channel messages for the footer marker before posting again |
| No DB transaction during HTTP | Long-held locks | Claim commits first; the result is recorded in a new transaction |
| Sweeper (beat, every 30 s) | Lost jobs, missed enqueues, scheduled and long-delay retries, dead workers | Re-queue `pending` rows that are due and weren't queued in the last 60 s; release `sending` rows locked more than 5 min |
| Late acks | A job lost if a worker dies mid-task | `task_acks_late`, `task_reject_on_worker_lost`, visibility timeout 600 s |

**Retry policy**

| Discord response | Result |
| --- | --- |
| 2xx | `sent` |
| 429, or a known-empty rate-limit bucket | `pending` again after `retry_after`; doesn't use an attempt |
| 5xx, timeout, connection error | Attempt +1, backoff of 10 s, 30 s, 2 min, 10 min, then `dead` on the 5th failure. Delays ≤ 60 s use a Celery countdown; longer ones wait for the sweeper |
| 50007 on a DM | Fallback channel (see above) |
| 10015 / 401 on a webhook | Clear the stored webhook, retry (a new one is created) |
| Any other 4xx | `failed` with the status, code and message |
| Unexpected exception | Logged, then retried like a 5xx |

**Rate limits:**
- The client tracks `X-RateLimit-Remaining` and `X-RateLimit-Reset-After` per route.
- A wait of 2 s or less sleeps inline; anything longer reschedules the row.
- `discord.deliver` also has a Celery rate limit of `10/s`.

## Configuration

Settings are read from environment variables or `backend/.env` (see `backend/.env.example`):

| Variable | Used by | Notes |
| --- | --- | --- |
| `DATABASE_URL` | API, worker | `postgresql+psycopg://user:password@host:5432/db`. URL-encode special characters in the password |
| `REDIS_URL` | API, worker | `redis://...` locally; `rediss://user:password@host:port/0` for Redis Cloud (TLS is turned on automatically) |
| `DISCORD_BOT_TOKEN` | Worker | Never commit it. `.env` is gitignored |
| `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET` | API | For account linking (not built yet) |
| `FRONTEND_BASE_URL` | API | Used for "Read the full note" and title links |
| `FRONTEND_ORIGINS` | API | CORS allow-list, JSON list |
| `USE_WEBHOOKS` | Worker | `true` = channel posts appear as the sender |
| `SUMMARY_THRESHOLD_CHARS` | API | Default 1500 |

Policy defaults live in `backend/app/config.py`:
- `max_attempts=5`, `backoff_seconds`, `max_countdown_seconds=60`
- `stuck_after_seconds=300`, `sweep_interval_seconds=30`, `sweep_grace_seconds=60`
- `deliver_rate_limit="10/s"`

### Redis Cloud

- Redis is only the **broker**; it carries job IDs. Postgres holds all status, so losing Redis data delays messages but loses none.
- Use a plan that includes TLS. Set the eviction policy to `noeviction`.
- Restrict the allow list to the VM's IP, and use a dedicated ACL user.
- Celery settings (in `celery_client.py`):
  - `task_ignore_result=True` (no result backend)
  - `broker_pool_limit=2`
  - `visibility_timeout=600`
  - `worker_prefetch_multiplier=1`
  - `broker_connection_retry_on_startup=True`

## Discord setup

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications), add a bot, and copy its token into `DISCORD_BOT_TOKEN`.
2. Invite the bot (OAuth2 → URL Generator, `bot` scope) with these permissions:
   - **View Channel**, **Send Messages**, **Embed Links**
   - **Manage Webhooks**, so channel posts appear as the sender
   - **Read Message History**, so a retried webhook post can check whether it already went through
3. Turn on Discord **Developer Mode** (Settings → Advanced), then right-click → Copy ID for the server, the channels and users.
4. Register channels with `set-channel` (below).
5. **Account linking (to build):** a "Connect Discord" button that runs OAuth2 with the `identify` scope. It saves `discord_user_id`, the display name and `avatar_url`. The redirect URI is `https://api.<domain>/auth/discord/callback`.

The bot can only DM people who share a server with it.

**Discord limits** (checked in `payloads.py`): title 256 characters, description 4096, field value 1024, 6000 total per embed, webhook username 80 (it can't contain "discord" or "clyde"), nonce 25.

## Local setup

From `backend/`:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env          # fill in DATABASE_URL, DISCORD_BOT_TOKEN
createdb pm_dashboard
.venv/bin/alembic upgrade head
.venv/bin/python -m app.cli set-channel announcements <server_id> <announcements_channel_id>
.venv/bin/python -m app.cli set-channel assignments_fallback <server_id> <fallback_channel_id>
```

Replace every `<...>`, angle brackets included, with real values. zsh treats `<` as a special character.

Run the worker in its own terminal. On macOS it needs `--pool=solo`, because Celery's default prefork pool fails when macOS spawns worker processes:

```bash
.venv/bin/celery -A app.worker.tasks:celery_app worker -Q discord --pool=solo --loglevel=INFO
```

Beat is only needed for scheduled announcements, long retries and recovery. Run it in another terminal:

```bash
.venv/bin/celery -A app.worker.tasks:celery_app beat --loglevel=INFO
```

To serve the API (`/healthz` only so far):

```bash
.venv/bin/uvicorn app.api.main:app --reload
```

## Testing from the terminal

Until there's a UI, `app/cli.py` does what the dashboard will do. Run everything from `backend/` with the worker running. Add `--help` to any command for all of its options.

| Command | What it does |
| --- | --- |
| `.venv/bin/python -m app.cli add-user you@team.org --discord-id 123...` | Add or update a user. Their name and avatar are pulled from Discord |
| `.venv/bin/python -m app.cli users` | List users and whether they're linked |
| `.venv/bin/python -m app.cli announce --as you@team.org --title "Shop day" --body "Saturday 9am"` | Post an announcement as that user |
| `.venv/bin/python -m app.cli announce --as you@team.org --title "Reminder" --body "..." --in 2` | Schedule it 2 minutes out (needs beat) |
| `.venv/bin/python -m app.cli assign --as you@team.org --to bob@team.org --title "Wire harness" --priority high --due 2026-11-05T17:00` | Create a task and DM the assignee. Prints the task ID |
| `.venv/bin/python -m app.cli edit-task last --as you@team.org --title "Wire harness v2"` | Edit a task; the assignee's DM updates in place |
| `.venv/bin/python -m app.cli reassign last --as you@team.org --to carol@team.org` | Move it; the old DM says "Reassigned to Carol.", Carol gets a new DM. `--to none` unassigns |
| `.venv/bin/python -m app.cli status` | Recent messages with status, delivery route and error |
| `.venv/bin/python -m app.cli set-channel <purpose> <server_id> <channel_id>` | Map a purpose to a channel (clears its webhook if the channel changes) |

`last` means the most recently created task; you can also pass a full task ID. `--due` takes local time.

**Reading `status`:**

```
10-28 16:58:11  task_assigned    lead@test   failed               not_linked
10-28 16:58:11  task_unassigned  me@test     sent     dm
10-28 16:58:03  task_updated     me@test     sent     dm
10-28 16:57:30  announcement     #announcements  sent  webhook
```

| You see | Meaning |
| --- | --- |
| `sent dm` / `sent webhook` / `sent bot_channel` | Delivered, and how |
| `sent ... (fallback)` | The assignee's DMs were blocked; posted in the fallback channel with an @mention |
| `pending` | Waiting for the worker, a retry, or its scheduled time. Is the worker running? |
| `failed not_linked` | That user has no `--discord-id` |
| `failed channel_not_configured: X` | Run `set-channel X ...` |
| `failed 403 code=50013` / `50001` | The bot is missing a permission in that channel |
| `skipped` | Unassign or reassign with no earlier message to edit |
| `dead` | Five temporary failures in a row (Discord down, network). Check the worker log |

**Automated tests** (no Discord needed):

```bash
.venv/bin/python -m pytest
```

They use in-memory SQLite by default. To run them against Postgres, set `TEST_DATABASE_URL=postgresql+psycopg://...`, pointing at a **throwaway** database: the tests drop all tables.

## Deploying to the VM

Files are in `backend/deploy/`:

- `systemd/pm-api.service`: gunicorn on `127.0.0.1:8000`, env from `/etc/pm-dashboard/api.env`
- `systemd/pm-worker.service`: Celery worker (default prefork pool on Linux), `TimeoutStopSec=60`, env from `/etc/pm-dashboard/worker.env`
- `systemd/pm-beat.service`: Celery beat. Run **exactly one**
- `Caddyfile`: HTTPS reverse proxy for `api.<domain>`
- `deploy.sh [branch]`: pull, install, `alembic upgrade head`, restart the services, check `/healthz`

Expected layout on the VM: code at `/srv/pm-dashboard`, services run as user `app`, secrets in `/etc/pm-dashboard/*.env` (mode `600`).

Firewall: inbound 22 (key-only), 80 and 443; outbound to Postgres, Redis Cloud and `discord.com`.

Monitoring:
- `/healthz` checks the database and Redis.
- Logs are in journald (`journalctl -u pm-worker -f`).
- Alert on `dead` rows, rows `pending` for more than 5 minutes, or a `worker_heartbeats.beat` older than 2 minutes.

## Long-note summaries (design only)

`condense()` runs when an outbox row is built:

1. If the text is at or under `SUMMARY_THRESHOLD_CHARS`, it's sent unchanged.
2. Otherwise it uses `tasks.summary` / `announcements.summary` if one is set.
3. Otherwise it asks the `Summarizer` (currently `NoopSummarizer`, which returns nothing).
4. Otherwise it truncates at a word boundary and adds "Read the full note".

To add LLM summaries later, implement `Summarizer.summarize(text, max_chars)`, or have a background agent fill the `summary` columns. Nothing else in the pipeline changes.

## To do

- [ ] API endpoints (waiting on specs). Each should open a session, call `service.*`, and commit once
- [ ] Discord account linking (OAuth2 `identify`)
- [ ] Decide whether task assignments should also (or instead) be channel posts as the sender
- [ ] Encrypt `discord_channels.webhook_token`
- [ ] `#ops` alerts for dead rows and a stale beat heartbeat
- [ ] GitHub Action running pytest
- [ ] Provision Neon/Supabase, Redis Cloud and the VM

## Open decisions

1. **Dashboard login:** will users sign in with Discord OAuth? That would make account linking automatic.
2. **Servers:** one Discord server (current assumption), or should each team connect its own?
3. **Assignment delivery:** bot DMs showing "From …" (current), channel posts as the sender, or both?
