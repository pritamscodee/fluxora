from datetime import datetime, timezone

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.ids import public_id


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow,
        onupdate=utcnow, nullable=False,
    )


class PublicIdMixin:
    """Every externally visible row is keyed by an opaque id, never a sequence."""

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=public_id)


__all__ = ["Base", "TimestampMixin", "PublicIdMixin", "utcnow"]
