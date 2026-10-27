from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator

from app.schemas.common import ApiModel, RequestModel

EVENT_TYPES = (
    "video.upload.created",
    "video.upload.asset_created",
    "video.upload.cancelled",
    "video.upload.errored",
    "video.asset.created",
    "video.asset.ready",
    "video.asset.errored",
    "video.asset.updated",
    "video.asset.deleted",
    "video.asset.track.created",
    "video.asset.track.ready",
    "video.asset.track.errored",
    "video.asset.static_rendition.ready",
    "video.live_stream.created",
    "video.live_stream.connected",
    "video.live_stream.recording",
    "video.live_stream.active",
    "video.live_stream.disconnected",
    "video.live_stream.idle",
    "video.live_stream.updated",
    "video.live_stream.disabled",
    "video.live_stream.deleted",
)


class WebhookCreate(RequestModel):
    url: HttpUrl
    # Empty means every event type, which is the useful default for a first integration.
    events: list[str] = Field(default_factory=list)
    description: str | None = Field(default=None, max_length=255)

    @field_validator("events")
    @classmethod
    def known_events(cls, v: list[str]) -> list[str]:
        unknown = sorted(set(v) - set(EVENT_TYPES))
        if unknown:
            raise ValueError(f"unknown event types: {', '.join(unknown)}")
        return v

    @field_validator("url")
    @classmethod
    def https_outside_dev(cls, v: HttpUrl) -> HttpUrl:
        host = (v.host or "").lower()
        local = host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".localhost")
        if v.scheme != "https" and not local:
            raise ValueError("webhook url must use https")
        return v


class WebhookUpdate(RequestModel):
    url: HttpUrl | None = None
    events: list[str] | None = None
    active: bool | None = None
    description: str | None = Field(default=None, max_length=255)

    @field_validator("events")
    @classmethod
    def known_events(cls, v):
        if v is None:
            return v
        return WebhookCreate.known_events(v)


class WebhookOut(ApiModel):
    id: str
    url: str
    events: list
    active: bool
    description: str | None = None
    created_at: datetime


class WebhookCreated(WebhookOut):
    secret: str


class DeliveryOut(ApiModel):
    id: str
    event_id: str
    status: str
    attempts: int
    response_status: int | None = None
    error: str | None = None
    next_attempt_at: datetime | None = None
    delivered_at: datetime | None = None
    created_at: datetime


class EventOut(ApiModel):
    id: str
    type: str
    object_type: str
    object_id: str
    data: dict
    created_at: datetime


class BeaconEvent(RequestModel):
    """Player telemetry. Accepted unauthenticated (the playback id is the capability), so
    every field is bounded and nothing here is trusted for billing."""

    playback_id: str = Field(max_length=40)
    session_id: str = Field(max_length=64)
    viewer_id: str | None = Field(default=None, max_length=64)
    event: Literal["view_start", "heartbeat", "view_end", "error", "rebuffer"]
    watch_time: float = Field(default=0.0, ge=0, le=86400)
    playing_time: float = Field(default=0.0, ge=0, le=86400)
    startup_time_ms: int | None = Field(default=None, ge=0, le=600000)
    rebuffer_count: int = Field(default=0, ge=0, le=10000)
    rebuffer_seconds: float = Field(default=0.0, ge=0, le=86400)
    completed: bool = False
    player_name: str | None = Field(default=None, max_length=64)
    browser: str | None = Field(default=None, max_length=64)
    os: str | None = Field(default=None, max_length=64)
    device_type: str | None = Field(default=None, max_length=32)


class MetricValue(BaseModel):
    date: str
    value: float


class MetricOut(BaseModel):
    metric: str
    total: float
    timeframe: list[str]
    data: list[MetricValue]


class VideoViewOut(ApiModel):
    id: str
    asset_id: str | None = None
    playback_id: str | None = None
    session_id: str
    viewer_id: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    watch_time_seconds: float
    playing_time_seconds: float
    startup_time_ms: int | None = None
    rebuffer_count: int
    completed: bool
    browser: str | None = None
    os: str | None = None
    device_type: str | None = None


class UsageOut(BaseModel):
    timeframe: list[str]
    totals: dict[str, float]
    daily: list[dict]


class JobOut(ApiModel):
    id: str
    type: str
    status: str
    attempts: int
    max_attempts: int
    progress: float
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class AgentRunCreate(RequestModel):
    prompt: str = Field(min_length=3, max_length=8192)
    asset_id: str | None = Field(default=None, max_length=40)


class AgentApproval(RequestModel):
    approved: bool


class AgentRunOut(ApiModel):
    id: str
    status: str
    prompt: str
    plan: list | None = None
    requires_approval: bool
    approved: bool | None = None
    steps: list | None = None
    result: dict | None = None
    error: str | None = None
    trace_url: str | None = None
    created_at: datetime
