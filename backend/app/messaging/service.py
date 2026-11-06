"""Entry points the API endpoints will call. None of them commit: the endpoint owns the
transaction, so the domain change and its outbox rows are saved (or rolled back) together."""

from datetime import datetime

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import utcnow
from app.messaging import outbox
from app.messaging.condense import Summarizer, condense
from app.messaging.payloads import announcement_embed, notice_embed, sender_info, task_embed
from app.models import (
    Announcement,
    MessageKind,
    OutboundMessage,
    Priority,
    RefType,
    TargetType,
    Task,
    User,
)


def post_announcement(
    session: Session,
    *,
    author: User,
    title: str,
    body: str,
    channel_purpose: str = "announcements",
    scheduled_for: datetime | None = None,
    settings: Settings | None = None,
    summarizer: Summarizer | None = None,
) -> Announcement:
    settings = settings or get_settings()
    if scheduled_for is not None and scheduled_for.tzinfo is None:
        raise ValueError("scheduled_for must be timezone-aware")

    announcement = Announcement(title=title, body=body, channel_purpose=channel_purpose,
                                scheduled_for=scheduled_for, created_by=author.id)
    session.add(announcement)
    session.flush()

    link = f"{settings.frontend_base_url}/announcements/{announcement.id}"
    description = condense(body, link=link, threshold=settings.summary_threshold_chars,
                           cached_summary=announcement.summary, summarizer=summarizer)
    outbox.add(session, OutboundMessage(
        kind=MessageKind.ANNOUNCEMENT,
        ref_type=RefType.ANNOUNCEMENT,
        ref_id=announcement.id,
        sender_id=author.id,
        target_type=TargetType.CHANNEL,
        channel_purpose=channel_purpose,
        payload={"embed": announcement_embed(announcement, description=description, link=link, sender=author),
                 "sender": sender_info(author)},
        available_at=scheduled_for or utcnow(),
    ))
    return announcement


def create_task(
    session: Session,
    *,
    creator: User,
    title: str,
    description: str = "",
    assignee: User | None = None,
    priority: Priority = Priority.MEDIUM,
    due_at: datetime | None = None,
    settings: Settings | None = None,
) -> Task:
    task = Task(title=title, description=description, created_by=creator.id, priority=priority, due_at=due_at)
    session.add(task)
    session.flush()
    if assignee is not None:
        assign_task(session, task=task, assignee=assignee, actor=creator, settings=settings)
    return task


def assign_task(
    session: Session,
    *,
    task: Task,
    assignee: User | None,
    actor: User,
    settings: Settings | None = None,
    summarizer: Summarizer | None = None,
) -> list[OutboundMessage]:
    """Change the assignee. DMs the new assignee and edits the previous assignee's message."""
    settings = settings or get_settings()
    previous_id = task.assignee_id
    if assignee is not None and assignee.id == previous_id:
        return []

    task.assignee_id = assignee.id if assignee else None
    session.flush()
    link = _task_link(task, settings)
    messages = []

    if previous_id is not None:
        notice = f"Reassigned to {assignee.name}." if assignee else "You were unassigned from this task."
        messages.append(outbox.add(session, OutboundMessage(
            kind=MessageKind.TASK_UNASSIGNED,
            ref_type=RefType.TASK,
            ref_id=task.id,
            sender_id=actor.id,
            target_type=TargetType.DM,
            recipient_user_id=previous_id,
            payload={"embed": notice_embed(task, headline="Reassigned", description=notice, link=link,
                                           sender=actor),
                     "sender": sender_info(actor)},
        )))

    if assignee is not None:
        messages.append(outbox.add(session, OutboundMessage(
            kind=MessageKind.TASK_ASSIGNED,
            ref_type=RefType.TASK,
            ref_id=task.id,
            sender_id=actor.id,
            target_type=TargetType.DM,
            recipient_user_id=assignee.id,
            payload={"embed": task_embed(task, headline="New task", link=link, sender=actor,
                                         description=_task_description(task, link, settings, summarizer)),
                     "sender": sender_info(actor)},
        )))
    return messages


def task_changed(
    session: Session,
    *,
    task: Task,
    actor: User,
    settings: Settings | None = None,
    summarizer: Summarizer | None = None,
) -> OutboundMessage | None:
    """Call after editing a task's title, description, priority, due date or status.
    The worker edits the assignee's existing message in place, or sends a new one if there is none."""
    if task.assignee_id is None:
        return None
    settings = settings or get_settings()
    session.flush()
    link = _task_link(task, settings)
    return outbox.add(session, OutboundMessage(
        kind=MessageKind.TASK_UPDATED,
        ref_type=RefType.TASK,
        ref_id=task.id,
        sender_id=actor.id,
        target_type=TargetType.DM,
        recipient_user_id=task.assignee_id,
        payload={"embed": task_embed(task, headline="Task updated", link=link, sender=actor,
                                     description=_task_description(task, link, settings, summarizer)),
                 "sender": sender_info(actor)},
    ))


def _task_link(task: Task, settings: Settings) -> str:
    return f"{settings.frontend_base_url}/tasks/{task.id}"


def _task_description(task: Task, link: str, settings: Settings, summarizer: Summarizer | None) -> str:
    return condense(task.description or "", link=link, threshold=settings.summary_threshold_chars,
                    cached_summary=task.summary, summarizer=summarizer)
