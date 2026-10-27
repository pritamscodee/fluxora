from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PublicIdMixin, TimestampMixin

JOB_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")
DELIVERY_STATUSES = ("pending", "delivered", "failed", "exhausted")


class Event(Base, TimestampMixin):
    """Append-only log of everything that happened. Webhook deliveries reference an event
    rather than re-deriving a payload, which is what makes replay honest."""

    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    object_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class WebhookEndpoint(Base, PublicIdMixin, TimestampMixin):
    __tablename__ = "webhook_endpoints"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    secret: Mapped[str] = mapped_column(String(128), nullable=False)
    # Empty list means "every event type"; explicit lists are matched exactly.
    events: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))

    def wants(self, event_type: str) -> bool:
        return self.active and (not self.events or event_type in self.events)


class WebhookDelivery(Base, PublicIdMixin, TimestampMixin):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        # One delivery row per (endpoint, event) so a redelivery is an explicit new attempt
        # chain and a double-dispatch of the same event cannot create duplicates.
        UniqueConstraint("endpoint_id", "event_id", "attempt_chain", name="uq_delivery_attempt"),
    )

    endpoint_id: Mapped[str] = mapped_column(
        ForeignKey("webhook_endpoints.id", ondelete="CASCADE"), index=True, nullable=False
    )
    event_id: Mapped[str] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), index=True, nullable=False
    )
    attempt_chain: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[str | None] = mapped_column(String(2048))
    error: Mapped[str | None] = mapped_column(String(1024))


class Job(Base, PublicIdMixin, TimestampMixin):
    """Durable work queue. Claimed with SELECT ... FOR UPDATE SKIP LOCKED; see ADR-0001."""

    __tablename__ = "jobs"

    project_id: Mapped[str | None] = mapped_column(String(40), index=True)
    type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSON)

    # Set on every job; a retried handler must converge on the same output.
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)

    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    run_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # Visibility timeout: a job whose lock is older than the lease is reclaimable, which is
    # how we recover from a worker that was killed mid-ffmpeg.
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(64))
    lease_seconds: Mapped[int] = mapped_column(Integer, default=3600, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(4096))
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


class IdempotencyRecord(Base, TimestampMixin):
    """Replay protection for mutating API-key requests."""

    __tablename__ = "idempotency_records"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)  # project_id:key
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict] = mapped_column(JSON, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class VideoView(Base, PublicIdMixin, TimestampMixin):
    """One row per playback session, written by the player beacon."""

    __tablename__ = "video_views"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    asset_id: Mapped[str | None] = mapped_column(String(40), index=True)
    playback_id: Mapped[str | None] = mapped_column(String(40), index=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    viewer_id: Mapped[str | None] = mapped_column(String(64), index=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    watch_time_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    playing_time_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    startup_time_ms: Mapped[int | None] = mapped_column(Integer)
    rebuffer_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rebuffer_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    player_name: Mapped[str | None] = mapped_column(String(64))
    browser: Mapped[str | None] = mapped_column(String(64))
    os: Mapped[str | None] = mapped_column(String(64))
    device_type: Mapped[str | None] = mapped_column(String(32))
    country: Mapped[str | None] = mapped_column(String(8))


class UsageRecord(Base, TimestampMixin):
    """Daily metered rollup. `metric` is one of encoding_minutes, storage_gb_hours,
    delivery_minutes, delivery_gb."""

    __tablename__ = "usage_records"
    __table_args__ = (UniqueConstraint("project_id", "day", "metric", name="uq_usage_day_metric"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    day: Mapped[str] = mapped_column(String(10), nullable=False, index=True)  # YYYY-MM-DD
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


class AgentRun(Base, PublicIdMixin, TimestampMixin):
    __tablename__ = "agent_runs"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    thread_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    prompt: Mapped[str] = mapped_column(String(8192), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="planning", nullable=False, index=True)
    plan: Mapped[list | None] = mapped_column(JSON)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approved: Mapped[bool | None] = mapped_column(Boolean)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    steps: Mapped[list | None] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(String(4096))
    trace_url: Mapped[str | None] = mapped_column(String(1024))


Index("ix_jobs_claim", Job.status, Job.priority, Job.run_after)
Index("ix_deliveries_retry", WebhookDelivery.status, WebhookDelivery.next_attempt_at)
Index("ix_views_project_started", VideoView.project_id, VideoView.started_at)
