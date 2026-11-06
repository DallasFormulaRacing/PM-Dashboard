"""Admin and testing commands until there's a UI. Run from backend/ with the worker running.

    python -m app.cli set-channel announcements <guild_id> <channel_id>
    python -m app.cli add-user nish@team.org --discord-id 4315...      # name/avatar pulled from Discord
    python -m app.cli users
    python -m app.cli announce --as nish@team.org --title "Shop day" --body "Saturday 9am"
    python -m app.cli announce --as nish@team.org --title "Reminder" --body "..." --in 2   # minutes; needs beat
    python -m app.cli assign --as nish@team.org --to bob@team.org --title "Wire harness" --due 2026-11-05
    python -m app.cli edit-task <task_id> --as nish@team.org --title "Wire harness v2" --priority high
    python -m app.cli reassign <task_id> --as nish@team.org --to carol@team.org     # or --to none
    python -m app.cli status
"""

import argparse
import sys
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.config import get_settings
from app.db import get_session_factory, utcnow
from app.discord import DiscordClient
from app.messaging import service
from app.models import DiscordChannel, OutboundMessage, Priority, Task, User


def set_channel(args) -> None:
    with get_session_factory()() as session:
        channel = session.get(DiscordChannel, args.purpose)
        if channel is None:
            session.add(DiscordChannel(purpose=args.purpose, guild_id=args.guild_id, channel_id=args.channel_id))
        elif channel.channel_id != args.channel_id:
            # A webhook belongs to one channel; the worker creates a new one on next send.
            channel.guild_id, channel.channel_id = args.guild_id, args.channel_id
            channel.webhook_id = channel.webhook_token = None
        session.commit()
    print(f"{args.purpose} -> {args.channel_id}")


def add_user(args) -> None:
    name, avatar = args.name, args.avatar_url
    if args.discord_id and not (name and avatar):
        profile = _discord_profile(args.discord_id)
        name = name or profile["name"]
        avatar = avatar or profile["avatar_url"]
    if not name:
        sys.exit("give --name, or --discord-id to pull the name from Discord")

    with get_session_factory()() as session:
        user = session.scalar(select(User).where(User.email == args.email))
        if user is None:
            user = User(email=args.email, name=name)
            session.add(user)
        user.name = name
        user.avatar_url = avatar
        if args.discord_id:
            if args.discord_id != user.discord_user_id:
                user.discord_dm_channel_id = None
            user.discord_user_id = args.discord_id
        session.commit()
    print(f"{args.email}: {name}" + (f" (discord {args.discord_id})" if args.discord_id else " (not linked)"))


def list_users(args) -> None:
    with get_session_factory()() as session:
        for u in session.scalars(select(User).order_by(User.created_at)):
            print(f"{u.email:28} {u.name:24} {'discord ' + u.discord_user_id if u.discord_user_id else 'not linked'}")


def announce(args) -> None:
    with get_session_factory()() as session:
        author = _user(session, args.author)
        scheduled = utcnow() + timedelta(minutes=args.in_minutes) if args.in_minutes else None
        ann = service.post_announcement(session, author=author, title=args.title, body=args.body,
                                        channel_purpose=args.channel, scheduled_for=scheduled)
        session.commit()
    when = f"scheduled for {scheduled:%H:%M:%S} UTC (needs beat running)" if scheduled else "queued"
    print(f"announcement {ann.id} {when}")


def assign(args) -> None:
    with get_session_factory()() as session:
        task = service.create_task(
            session,
            creator=_user(session, args.author),
            title=args.title,
            description=args.description,
            assignee=_user(session, args.to),
            priority=Priority(args.priority),
            due_at=_parse_due(args.due),
        )
        session.commit()
    print(f"task {task.id} assigned to {args.to}")


def edit_task(args) -> None:
    with get_session_factory()() as session:
        task = _task(session, args.task_id)
        if args.title:
            task.title = args.title
        if args.description is not None:
            task.description = args.description
        if args.priority:
            task.priority = Priority(args.priority)
        if args.due:
            task.due_at = _parse_due(args.due)
        msg = service.task_changed(session, task=task, actor=_user(session, args.author))
        session.commit()
    print(f"task {task.id} updated" + ("; assignee's message will be edited" if msg else "; no assignee to notify"))


def reassign(args) -> None:
    with get_session_factory()() as session:
        task = _task(session, args.task_id)
        assignee = None if args.to.lower() == "none" else _user(session, args.to)
        msgs = service.assign_task(session, task=task, assignee=assignee, actor=_user(session, args.author))
        session.commit()
    print(f"task {task.id} -> {args.to} ({len(msgs)} message(s) queued)")


def status(args) -> None:
    with get_session_factory()() as session:
        rows = session.scalars(select(OutboundMessage).order_by(OutboundMessage.created_at.desc()).limit(args.limit))
        for m in rows:
            to = m.recipient.email if m.recipient else f"#{m.channel_purpose}"
            how = (m.delivered_via.value if m.delivered_via else "") + (" (fallback)" if m.used_fallback else "")
            print(f"{m.created_at:%m-%d %H:%M:%S}  {m.kind.value:16} {to:24} {m.status.value:8} {how:22} "
                  f"{m.last_error or ''}")


# --- helpers --------------------------------------------------------------------------

def _user(session, email: str) -> User:
    user = session.scalar(select(User).where(User.email == email))
    if user is None:
        sys.exit(f"no user {email!r}; add one with: python -m app.cli add-user {email} --discord-id <id>")
    return user


def _task(session, task_id: str) -> Task:
    if task_id == "last":
        task = session.scalar(select(Task).order_by(Task.created_at.desc()).limit(1))
    else:
        try:
            task = session.get(Task, uuid.UUID(task_id))
        except ValueError:
            sys.exit(f"{task_id!r} isn't a task id; use the id printed by 'assign', or 'last'")
    if task is None:
        sys.exit(f"no task {task_id}")
    return task


def _parse_due(value: str | None) -> datetime | None:
    if not value:
        return None
    due = datetime.fromisoformat(value)
    return due if due.tzinfo else due.replace(tzinfo=datetime.now().astimezone().tzinfo).astimezone(UTC)


def _discord_profile(discord_id: str) -> dict:
    settings = get_settings()
    client = DiscordClient(settings.discord_bot_token.get_secret_value(), base_url=settings.discord_api_base)
    try:
        data = client.get_user(discord_id)
    finally:
        client.close()
    avatar = (f"https://cdn.discordapp.com/avatars/{discord_id}/{data['avatar']}.png?size=128" if data.get("avatar")
              else f"https://cdn.discordapp.com/embed/avatars/{(int(discord_id) >> 22) % 6}.png")
    return {"name": data.get("global_name") or data["username"], "avatar_url": avatar}


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("set-channel", help="map a purpose to a Discord channel")
    p.add_argument("purpose")
    p.add_argument("guild_id")
    p.add_argument("channel_id")
    p.set_defaults(func=set_channel)

    p = sub.add_parser("add-user", help="add or update a user")
    p.add_argument("email")
    p.add_argument("--discord-id")
    p.add_argument("--name", help="defaults to their Discord display name")
    p.add_argument("--avatar-url", help="defaults to their Discord avatar")
    p.set_defaults(func=add_user)

    p = sub.add_parser("users", help="list users")
    p.set_defaults(func=list_users)

    p = sub.add_parser("announce", help="post an announcement")
    p.add_argument("--as", dest="author", required=True, help="author's email")
    p.add_argument("--title", required=True)
    p.add_argument("--body", required=True)
    p.add_argument("--channel", default="announcements", help="channel purpose")
    p.add_argument("--in", dest="in_minutes", type=float, help="schedule this many minutes from now")
    p.set_defaults(func=announce)

    p = sub.add_parser("assign", help="create a task and assign it")
    p.add_argument("--as", dest="author", required=True, help="assigner's email")
    p.add_argument("--to", required=True, help="assignee's email")
    p.add_argument("--title", required=True)
    p.add_argument("--description", default="")
    p.add_argument("--priority", choices=[x.value for x in Priority], default="medium")
    p.add_argument("--due", help="e.g. 2026-11-05 or 2026-11-05T17:00 (local time)")
    p.set_defaults(func=assign)

    p = sub.add_parser("edit-task", help="change a task; edits the assignee's message")
    p.add_argument("task_id", help="id printed by 'assign', or 'last'")
    p.add_argument("--as", dest="author", required=True)
    p.add_argument("--title")
    p.add_argument("--description")
    p.add_argument("--priority", choices=[x.value for x in Priority])
    p.add_argument("--due")
    p.set_defaults(func=edit_task)

    p = sub.add_parser("reassign", help="move a task to someone else (or 'none')")
    p.add_argument("task_id", help="id printed by 'assign', or 'last'")
    p.add_argument("--as", dest="author", required=True)
    p.add_argument("--to", required=True)
    p.set_defaults(func=reassign)

    p = sub.add_parser("status", help="recent messages and their delivery status")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
