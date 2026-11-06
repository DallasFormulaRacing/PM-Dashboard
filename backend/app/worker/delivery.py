"""Delivers one outbox row to Discord: claim -> send -> record.

Kept free of Celery so it can be tested directly. No DB transaction is held open while
calling Discord: the claim commits first, and the result is recorded in a new transaction.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db import utcnow
from app.discord import DiscordAPIError, DiscordClient, RateLimited, TransientDiscordError
from app.messaging.payloads import webhook_username
from app.models import DeliveredVia, DiscordChannel, MessageKind, MessageStatus, OutboundMessage, User

log = logging.getLogger(__name__)

FALLBACK_PURPOSE = "assignments_fallback"


@dataclass
class Outcome:
    status: MessageStatus | None  # None: the row wasn't claimable (already taken, done, or not due)
    retry_in: float | None = None  # set when the caller should schedule a Celery countdown


@dataclass
class Sent:
    message_id: str
    channel_id: str
    via: DeliveredVia
    used_fallback: bool = False


class PermanentFailure(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Skipped(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Deliverer:
    def __init__(self, session_factory: sessionmaker, client: DiscordClient, settings: Settings):
        self._session_factory = session_factory
        self._client = client
        self._settings = settings

    def deliver(self, outbox_id: uuid.UUID) -> Outcome:
        with self._session_factory() as session:
            msg = self._claim(session, outbox_id)
            if msg is None:
                return Outcome(None)
            try:
                sent = self._send(session, msg)
            except RateLimited as exc:
                return self._reschedule(session, msg, exc.retry_after, f"rate limited: {exc}")
            except TransientDiscordError as exc:
                return self._retry_or_die(session, msg, str(exc))
            except DiscordAPIError as exc:
                return self._finish(session, msg, MessageStatus.FAILED, str(exc))
            except PermanentFailure as exc:
                return self._finish(session, msg, MessageStatus.FAILED, exc.reason)
            except Skipped as exc:
                return self._finish(session, msg, MessageStatus.SKIPPED, exc.reason)
            except Exception as exc:
                log.exception("unexpected error delivering outbox %s", outbox_id)
                session.rollback()
                return self._retry_or_die(session, msg, f"unexpected: {exc!r}")
            return self._record_sent(session, msg, sent)

    # --- claim / record ---------------------------------------------------------------

    def _claim(self, session: Session, outbox_id: uuid.UUID) -> OutboundMessage | None:
        now = utcnow()
        claimed = session.execute(
            update(OutboundMessage)
            .where(OutboundMessage.id == outbox_id,
                   OutboundMessage.status == MessageStatus.PENDING,
                   OutboundMessage.available_at <= now)
            .values(status=MessageStatus.SENDING, locked_at=now)
            .returning(OutboundMessage.id)
            .execution_options(synchronize_session=False)
        ).scalar_one_or_none()
        session.commit()
        if claimed is None:
            return None
        return session.get(OutboundMessage, outbox_id, populate_existing=True)

    def _record_sent(self, session: Session, msg: OutboundMessage, sent: Sent) -> Outcome:
        msg.status = MessageStatus.SENT
        msg.discord_message_id = sent.message_id
        msg.discord_channel_id = sent.channel_id
        msg.delivered_via = sent.via
        msg.used_fallback = sent.used_fallback
        msg.sent_at = utcnow()
        msg.locked_at = None
        msg.last_error = None
        session.commit()
        return Outcome(MessageStatus.SENT)

    def _finish(self, session: Session, msg: OutboundMessage, status: MessageStatus, error: str) -> Outcome:
        log.info("outbox %s -> %s: %s", msg.id, status, error)
        msg.status = status
        msg.last_error = error
        msg.locked_at = None
        session.commit()
        return Outcome(status)

    def _retry_or_die(self, session: Session, msg: OutboundMessage, error: str) -> Outcome:
        msg.attempts += 1
        if msg.attempts >= self._settings.max_attempts:
            return self._finish(session, msg, MessageStatus.DEAD, error)
        backoff = self._settings.backoff_seconds
        return self._reschedule(session, msg, backoff[min(msg.attempts - 1, len(backoff) - 1)], error)

    def _reschedule(self, session: Session, msg: OutboundMessage, delay: float, error: str) -> Outcome:
        msg.status = MessageStatus.PENDING
        msg.available_at = utcnow() + timedelta(seconds=delay)
        msg.locked_at = None
        msg.last_error = error
        short = delay <= self._settings.max_countdown_seconds
        # Short delays get a Celery countdown; marking them enqueued keeps the sweeper away
        # unless that countdown is lost. Long delays are left to the sweeper.
        msg.enqueued_at = msg.available_at if short else None
        session.commit()
        return Outcome(MessageStatus.PENDING, retry_in=delay if short else None)

    # --- routing ----------------------------------------------------------------------

    def _send(self, session: Session, msg: OutboundMessage) -> Sent:
        match msg.kind:
            case MessageKind.ANNOUNCEMENT:
                return self._post_to_channel(session, self._channel(session, msg.channel_purpose), msg)
            case MessageKind.TASK_ASSIGNED:
                return self._dm_or_fallback(session, msg)
            case MessageKind.TASK_UPDATED:
                prior = self._prior_message(session, msg)
                if prior is not None:
                    try:
                        return self._edit(session, prior, msg)
                    except DiscordAPIError as exc:
                        if not exc.is_missing:
                            raise
                        log.info("earlier message for outbox %s is gone; sending a new one", msg.id)
                return self._dm_or_fallback(session, msg)
            case MessageKind.TASK_UNASSIGNED:
                prior = self._prior_message(session, msg)
                if prior is None:
                    raise Skipped("no earlier message to edit")
                try:
                    return self._edit(session, prior, msg)
                except DiscordAPIError as exc:
                    if exc.is_missing:
                        raise Skipped("earlier message was deleted") from exc
                    raise
        raise PermanentFailure(f"unknown message kind {msg.kind!r}")

    def _dm_or_fallback(self, session: Session, msg: OutboundMessage) -> Sent:
        recipient = msg.recipient
        if recipient is None:
            raise PermanentFailure("recipient_missing")
        if not recipient.discord_user_id:
            raise PermanentFailure("not_linked")
        try:
            return self._send_dm(session, recipient, msg)
        except DiscordAPIError as exc:
            if exc.code != DiscordAPIError.CANNOT_DM_USER:
                raise
        log.info("DMs blocked for user %s; using the fallback channel", recipient.id)
        channel = self._channel(session, FALLBACK_PURPOSE)
        return self._post_to_channel(session, channel, msg, mention=recipient.discord_user_id, fallback=True)

    def _send_dm(self, session: Session, recipient: User, msg: OutboundMessage) -> Sent:
        cached = recipient.discord_dm_channel_id
        channel_id = cached or self._open_dm(session, recipient)
        try:
            data = self._client.create_message(channel_id, self._bot_payload(msg))
        except DiscordAPIError as exc:
            if not (cached and exc.is_missing):
                raise
            channel_id = self._open_dm(session, recipient)  # the cached DM channel went stale
            data = self._client.create_message(channel_id, self._bot_payload(msg))
        return Sent(data["id"], channel_id, DeliveredVia.DM)

    def _open_dm(self, session: Session, recipient: User) -> str:
        channel_id = self._client.create_dm_channel(recipient.discord_user_id)
        recipient.discord_dm_channel_id = channel_id
        session.commit()
        return channel_id

    def _post_to_channel(self, session: Session, channel: DiscordChannel, msg: OutboundMessage, *,
                         mention: str | None = None, fallback: bool = False) -> Sent:
        if self._ensure_webhook(session, channel):
            if msg.attempts > 0:
                # Webhooks don't support nonces: after an ambiguous failure, check whether the
                # earlier attempt actually landed before posting again.
                existing = self._find_by_marker(channel.channel_id, msg.marker)
                if existing:
                    return Sent(existing, channel.channel_id, DeliveredVia.WEBHOOK, fallback)
            try:
                data = self._client.execute_webhook(channel.webhook_id, channel.webhook_token,
                                                    self._webhook_payload(msg, mention))
            except DiscordAPIError as exc:
                if exc.code == DiscordAPIError.UNKNOWN_WEBHOOK or exc.status == 401:
                    # Someone deleted the webhook in Discord; recreate it on the next attempt.
                    channel.webhook_id = channel.webhook_token = None
                    session.commit()
                    raise TransientDiscordError("webhook was deleted; will recreate it") from exc
                raise
            return Sent(data["id"], channel.channel_id, DeliveredVia.WEBHOOK, fallback)

        data = self._client.create_message(channel.channel_id, self._bot_payload(msg, mention))
        return Sent(data["id"], channel.channel_id, DeliveredVia.BOT_CHANNEL, fallback)

    def _edit(self, session: Session, prior: OutboundMessage, msg: OutboundMessage) -> Sent:
        if prior.delivered_via == DeliveredVia.WEBHOOK:
            channel = session.scalar(select(DiscordChannel).where(
                DiscordChannel.channel_id == prior.discord_channel_id))
            if channel is None or not channel.webhook_id:
                raise DiscordAPIError(404, DiscordAPIError.UNKNOWN_WEBHOOK, "webhook for the earlier message is gone")
            embed = self._embed(msg)
            embed.pop("author", None)
            self._client.edit_webhook_message(channel.webhook_id, channel.webhook_token, prior.discord_message_id,
                                              {"embeds": [embed], "allowed_mentions": {"parse": []}})
        else:
            self._client.edit_message(prior.discord_channel_id, prior.discord_message_id,
                                      {"embeds": [self._embed(msg)], "allowed_mentions": {"parse": []}})
        return Sent(prior.discord_message_id, prior.discord_channel_id, prior.delivered_via, prior.used_fallback)

    # --- lookups ----------------------------------------------------------------------

    def _channel(self, session: Session, purpose: str | None) -> DiscordChannel:
        channel = session.get(DiscordChannel, purpose) if purpose else None
        if channel is None:
            raise PermanentFailure(f"channel_not_configured: {purpose}")
        return channel

    def _prior_message(self, session: Session, msg: OutboundMessage) -> OutboundMessage | None:
        """The latest delivered message about the same task to the same person."""
        return session.scalar(
            select(OutboundMessage)
            .where(OutboundMessage.ref_type == msg.ref_type,
                   OutboundMessage.ref_id == msg.ref_id,
                   OutboundMessage.recipient_user_id == msg.recipient_user_id,
                   OutboundMessage.status == MessageStatus.SENT,
                   OutboundMessage.discord_message_id.is_not(None),
                   OutboundMessage.id != msg.id)
            .order_by(OutboundMessage.sent_at.desc())
            .limit(1)
        )

    def _ensure_webhook(self, session: Session, channel: DiscordChannel) -> bool:
        if not self._settings.use_webhooks:
            return False
        if channel.webhook_id and channel.webhook_token:
            return True
        try:
            hook = self._client.create_webhook(channel.channel_id, self._settings.webhook_name)
        except DiscordAPIError as exc:
            if exc.code in (DiscordAPIError.MISSING_PERMISSIONS, DiscordAPIError.MISSING_ACCESS):
                log.warning("bot can't create a webhook in #%s (needs Manage Webhooks); posting as the bot",
                            channel.purpose)
                return False
            raise
        channel.webhook_id, channel.webhook_token = hook["id"], hook["token"]
        session.commit()
        return True

    def _find_by_marker(self, channel_id: str, marker: str) -> str | None:
        try:
            recent = self._client.recent_messages(channel_id, limit=50)
        except DiscordAPIError:
            log.warning("can't read history in channel %s (needs Read Message History)", channel_id)
            return None
        for message in recent:
            for embed in message.get("embeds") or []:
                if marker in ((embed.get("footer") or {}).get("text") or ""):
                    return message["id"]
        return None

    # --- payloads ---------------------------------------------------------------------

    def _embed(self, msg: OutboundMessage) -> dict[str, Any]:
        embed = dict(msg.payload["embed"])
        embed["footer"] = {"text": f"PM Dashboard · {msg.marker}"}
        return embed

    def _allowed_mentions(self, mention: str | None) -> dict[str, Any]:
        # Never let message text ping @everyone or roles; only the one user we mean to mention.
        return {"parse": [], "users": [mention]} if mention else {"parse": []}

    def _bot_payload(self, msg: OutboundMessage, mention: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "embeds": [self._embed(msg)],
            "allowed_mentions": self._allowed_mentions(mention),
            "nonce": msg.nonce,
            "enforce_nonce": True,
        }
        if mention:
            payload["content"] = f"<@{mention}>"
        return payload

    def _webhook_payload(self, msg: OutboundMessage, mention: str | None = None) -> dict[str, Any]:
        sender = msg.payload.get("sender") or {}
        embed = self._embed(msg)
        embed.pop("author", None)  # the post itself carries the sender's name and avatar
        payload: dict[str, Any] = {
            "username": webhook_username(sender.get("name") or "", self._settings.webhook_name),
            "embeds": [embed],
            "allowed_mentions": self._allowed_mentions(mention),
        }
        if sender.get("avatar_url"):
            payload["avatar_url"] = sender["avatar_url"]
        if mention:
            payload["content"] = f"<@{mention}>"
        return payload
