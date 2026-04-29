"""Deal chat — E2E transport-only (brief task 5.3).

V0 backend treats `encrypted_content` and `nonce` as opaque blobs the
server never decrypts. Real key exchange + symmetric encryption is FASE
11 (mobile client) — the backend just persists what the client sends.

Caps:
  - 100 messages per deal (anti-spam, anti-storage-bloat for V0).
  - 4 KB per message (`encrypted_content` byte length). Anything more
    suggests the client is mis-using the chat as a file transfer.

Auth:
  - Both endpoints require `tier ≥ 2` (only deal parties can chat).
  - Deal must be `confirmed` before chat opens. Pending or cancelled
    deals don't get a chat channel.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schema import Deal, DealMessage
from app.services import audit_service, deal_service


MAX_MESSAGES_PER_DEAL: Final[int] = 100
MAX_MESSAGE_BYTES: Final[int] = 4096

DEFAULT_LIST_LIMIT: Final[int] = 50
MAX_LIST_LIMIT: Final[int] = 100


# ---------------------------------------------------------------------------
# Errors (alias to deal_service for uniform mapping in api/deals.py)
# ---------------------------------------------------------------------------


class DealMessageError(deal_service.DealError):
    code = "deal_message_error"
    http_status = 400


class MessageTooLarge(DealMessageError):
    code = "message_too_large"
    http_status = 422


class MessageQuotaExceeded(DealMessageError):
    code = "message_quota_exceeded"
    http_status = 429


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------


@dataclass
class MessageListPage:
    rows: list[DealMessage]
    total: int
    limit: int


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# send / list
# ---------------------------------------------------------------------------


async def send_message(
    db: AsyncSession,
    *,
    user_id: str,
    deal_id: str,
    encrypted_content: bytes,
    nonce: bytes,
) -> DealMessage:
    """Persist an opaque encrypted message. Caller (API) supplies bytes."""
    if len(encrypted_content) > MAX_MESSAGE_BYTES:
        raise MessageTooLarge(
            f"encrypted_content exceeds {MAX_MESSAGE_BYTES} bytes"
        )

    deal = await deal_service.get_deal_for_user(
        db, user_id=user_id, deal_id=deal_id
    )
    if deal.status != "confirmed":
        raise deal_service.DealNotConfirmed(
            f"deal {deal.id!r} is in status {deal.status!r}; chat is "
            f"unlocked only after both signatures land"
        )

    # Quota check.
    current = int(
        await db.scalar(
            select(func.count())
            .select_from(DealMessage)
            .where(DealMessage.deal_id == deal_id)
        )
        or 0
    )
    if current >= MAX_MESSAGES_PER_DEAL:
        raise MessageQuotaExceeded(
            f"deal {deal.id!r} reached the {MAX_MESSAGES_PER_DEAL}-message cap"
        )

    msg = DealMessage(
        deal_id=deal.id,
        sender_user_id=user_id,
        encrypted_content=encrypted_content,
        nonce=nonce,
        sent_at=_utcnow(),
    )
    db.add(msg)
    await db.flush()

    await audit_service.log_intent_event(
        db,
        user_id=user_id,
        action=audit_service.DealActions.SEND_MESSAGE,
        params={
            "deal_id": deal.id,
            "message_bytes": len(encrypted_content),
        },
        result={"message_id": msg.id},
        success=True,
    )

    await db.commit()
    await db.refresh(msg)

    # 6.1 — fire-and-forget UX notification to the recipient (the OTHER
    # party in the deal). Server doesn't decrypt the content; the
    # notification body is generic.
    from app.services import notification_service

    recipient_user_id = (
        deal.seller_user_id if user_id == deal.buyer_user_id else deal.buyer_user_id
    )
    await notification_service.create_notification(
        db,
        user_id=recipient_user_id,
        notification_type=notification_service.NotificationType.DEAL_MESSAGE_RECEIVED,
        title="Nuovo messaggio",
        body="Hai ricevuto un messaggio nella chat del deal.",
        payload={"deal_id": deal.id, "message_id": msg.id},
    )

    return msg


async def list_messages(
    db: AsyncSession,
    *,
    user_id: str,
    deal_id: str,
    limit: int = DEFAULT_LIST_LIMIT,
    before_id: str | None = None,
) -> MessageListPage:
    """List chat messages, newest-first, optionally cursor-paginated by id."""
    limit = max(1, min(MAX_LIST_LIMIT, limit))

    # Owner check via deal_service raises NotPartyToDeal if needed.
    deal = await deal_service.get_deal_for_user(
        db, user_id=user_id, deal_id=deal_id
    )

    base_filters = [DealMessage.deal_id == deal.id]
    if before_id is not None:
        # Find the cursor's sent_at, return strictly older messages.
        cursor_msg = await db.get(DealMessage, before_id)
        if cursor_msg is not None:
            base_filters.append(DealMessage.sent_at < cursor_msg.sent_at)

    total = int(
        await db.scalar(
            select(func.count())
            .select_from(DealMessage)
            .where(DealMessage.deal_id == deal.id)
        )
        or 0
    )
    rows = list(
        await db.scalars(
            select(DealMessage)
            .where(*base_filters)
            .order_by(desc(DealMessage.sent_at))
            .limit(limit)
        )
    )
    return MessageListPage(rows=rows, total=total, limit=limit)
