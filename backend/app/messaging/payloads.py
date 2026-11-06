"""Discord embed builders. Payloads are built when the outbox row is written, so the worker
sends exactly what the dashboard saw, even if the task changes before delivery."""

import re
from datetime import datetime
from typing import Any

from app.models import Announcement, Priority, Task, User

# https://discord.com/developers/docs/resources/message#embed-object-embed-limits
TITLE_MAX = 256
DESCRIPTION_MAX = 4096
FIELD_VALUE_MAX = 1024
AUTHOR_NAME_MAX = 256
EMBED_TOTAL_MAX = 6000
WEBHOOK_USERNAME_MAX = 80

PRIORITY_COLORS = {Priority.LOW: 0x95A5A6, Priority.MEDIUM: 0x3498DB, Priority.HIGH: 0xE74C3C}
ANNOUNCEMENT_COLOR = 0x5865F2


class PayloadTooLarge(ValueError):
    pass


def sender_info(user: User | None) -> dict[str, Any]:
    if user is None:
        return {"name": "PM Dashboard", "avatar_url": None}
    return {"name": user.name, "avatar_url": user.avatar_url}


def webhook_username(name: str, fallback: str = "PM Dashboard") -> str:
    """Discord rejects webhook usernames containing 'discord' or 'clyde', and caps them at 80 chars."""
    cleaned = re.sub(r"(?i)discord|clyde", "", name).strip()
    return (cleaned or fallback)[:WEBHOOK_USERNAME_MAX]


def discord_timestamp(dt: datetime) -> str:
    """Rendered by Discord in each reader's own timezone."""
    return f"<t:{int(dt.timestamp())}:f>"


def task_embed(task: Task, *, headline: str, description: str, link: str, sender: User | None) -> dict[str, Any]:
    fields = [{"name": "Priority", "value": task.priority.value.title(), "inline": True}]
    if task.due_at:
        fields.append({"name": "Due", "value": discord_timestamp(task.due_at), "inline": True})
    fields.append({"name": "Status", "value": task.status.value.replace("_", " ").title(), "inline": True})

    embed = {
        "title": _clip(f"{headline}: {task.title}", TITLE_MAX),
        "url": link,
        "description": _clip(description, DESCRIPTION_MAX),
        "color": PRIORITY_COLORS.get(task.priority, PRIORITY_COLORS[Priority.MEDIUM]),
        "fields": fields,
        "author": _author(sender),
    }
    return _validated(embed)


def notice_embed(task: Task, *, headline: str, description: str, link: str, sender: User | None) -> dict[str, Any]:
    """Replaces an earlier task message, e.g. after the task is reassigned to someone else."""
    embed = {
        "title": _clip(f"{headline}: {task.title}", TITLE_MAX),
        "url": link,
        "description": _clip(description, DESCRIPTION_MAX),
        "color": PRIORITY_COLORS[Priority.LOW],
        "author": _author(sender),
    }
    return _validated(embed)


def announcement_embed(announcement: Announcement, *, description: str, link: str,
                       sender: User | None) -> dict[str, Any]:
    embed = {
        "title": _clip(announcement.title, TITLE_MAX),
        "url": link,
        "description": _clip(description, DESCRIPTION_MAX),
        "color": ANNOUNCEMENT_COLOR,
        "author": _author(sender),
    }
    return _validated(embed)


def _author(sender: User | None) -> dict[str, Any]:
    info = sender_info(sender)
    author: dict[str, Any] = {"name": _clip(f"From {info['name']}", AUTHOR_NAME_MAX)}
    if info["avatar_url"]:
        author["icon_url"] = info["avatar_url"]
    return author


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _validated(embed: dict[str, Any]) -> dict[str, Any]:
    total = len(embed.get("title", "")) + len(embed.get("description", "")) + len(embed["author"]["name"])
    for field in embed.get("fields", []):
        if len(field["value"]) > FIELD_VALUE_MAX:
            raise PayloadTooLarge(f"field {field['name']!r} is over {FIELD_VALUE_MAX} characters")
        total += len(field["name"]) + len(field["value"])
    # Leave room for the footer the worker adds at send time.
    if total > EMBED_TOTAL_MAX - 100:
        raise PayloadTooLarge(f"embed is {total} characters; Discord allows {EMBED_TOTAL_MAX} in total")
    return embed
