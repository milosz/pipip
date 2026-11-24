"""SQLAlchemy 2.0 ORM models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Target(Base):
    """A monitored target and its latest known state."""

    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    target_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    current_version: Mapped[str | None] = mapped_column(String(255))
    previous_version: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    __table_args__ = (
        Index("ix_applications_status", "status"),
        CheckConstraint(
            "status IN ('ok', 'error', 'no_release', 'pending', 'removed')",
            name="ck_applications_status",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Target(name={self.name!r}, version={self.current_version!r})"


class VersionRecord(Base):
    """Immutable version history entry for an application."""

    __tablename__ = "version_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True, nullable=False
    )
    version: Mapped[str] = mapped_column(String(255), nullable=False)
    release_url: Mapped[str | None] = mapped_column(Text)
    release_notes: Mapped[str | None] = mapped_column(Text)
    digest: Mapped[str | None] = mapped_column(String(255))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, default=dict, nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        Index("ix_version_history_detected_at", "detected_at"),
        Index(
            "ix_version_history_app_detected",
            "application_id",
            "detected_at",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"VersionRecord(app_id={self.application_id}, version={self.version!r})"


class VersionDigestHistory(Base):
    """Audit trail of digest changes for a version record."""

    __tablename__ = "version_digest_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    version_record_id: Mapped[int] = mapped_column(
        ForeignKey("version_history.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    version: Mapped[str] = mapped_column(String(255), nullable=False)
    previous_digest: Mapped[str | None] = mapped_column(String(255))
    digest: Mapped[str] = mapped_column(String(255), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        Index(
            "ix_version_digest_history_app_detected",
            "application_id",
            "detected_at",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"VersionDigestHistory(app_id={self.application_id}, "
            f"version={self.version!r}, digest={self.digest!r})"
        )


class NotificationOutbox(Base):
    """Durable queue of notification deliveries with retry/dedup."""

    __tablename__ = "notification_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )
    notifier: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    app_name: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="pending", index=True, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "ix_notification_outbox_status_next",
            "status",
            "next_attempt_at",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"NotificationOutbox(id={self.id}, notifier={self.notifier!r}, "
            f"status={self.status!r})"
        )
