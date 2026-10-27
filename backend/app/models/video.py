from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, PublicIdMixin, TimestampMixin

ASSET_STATUSES = ("preparing", "ready", "errored", "deleted")
UPLOAD_STATUSES = ("waiting", "asset_created", "errored", "cancelled", "timed_out")
TRACK_STATUSES = ("preparing", "ready", "errored")
PLAYBACK_POLICIES = ("public", "signed")
LIVE_STATUSES = ("idle", "active", "disabled")


class Asset(Base, PublicIdMixin, TimestampMixin):
    __tablename__ = "assets"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default="preparing", nullable=False, index=True)

    # Source
    source_key: Mapped[str | None] = mapped_column(String(512))
    source_url: Mapped[str | None] = mapped_column(String(2048))
    source_bytes: Mapped[int | None] = mapped_column(Integer)

    # Probed metadata
    duration: Mapped[float | None] = mapped_column(Float)
    aspect_ratio: Mapped[str | None] = mapped_column(String(16))
    max_stored_resolution: Mapped[str | None] = mapped_column(String(16))
    max_stored_frame_rate: Mapped[float | None] = mapped_column(Float)
    input_info: Mapped[dict | None] = mapped_column(JSON)

    # Settings
    mp4_support: Mapped[str] = mapped_column(String(32), default="none", nullable=False)
    master_access: Mapped[str] = mapped_column(String(32), default="none", nullable=False)
    normalize_audio: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    passthrough: Mapped[str | None] = mapped_column(String(255))
    test: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    encoding_tier: Mapped[str] = mapped_column(String(32), default="smart", nullable=False)

    # Provenance: a clip points back at the asset it was cut from.
    source_asset_id: Mapped[str | None] = mapped_column(String(40), index=True)
    clip_start: Mapped[float | None] = mapped_column(Float)
    clip_end: Mapped[float | None] = mapped_column(Float)
    live_stream_id: Mapped[str | None] = mapped_column(String(40), index=True)
    upload_id: Mapped[str | None] = mapped_column(String(40), index=True)

    # Outputs
    hls_key: Mapped[str | None] = mapped_column(String(512))
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    errored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_type: Mapped[str | None] = mapped_column(String(64))
    error_messages: Mapped[list | None] = mapped_column(JSON)

    # AI-derived, populated by the agent tools
    ai_summary: Mapped[str | None] = mapped_column(String)
    ai_chapters: Mapped[list | None] = mapped_column(JSON)
    ai_moderation: Mapped[dict | None] = mapped_column(JSON)

    playback_ids: Mapped[list["PlaybackId"]] = relationship(
        back_populates="asset", cascade="all, delete-orphan", lazy="selectin"
    )
    tracks: Mapped[list["AssetTrack"]] = relationship(
        back_populates="asset", cascade="all, delete-orphan", lazy="selectin"
    )
    static_renditions: Mapped[list["StaticRendition"]] = relationship(
        back_populates="asset", cascade="all, delete-orphan", lazy="selectin"
    )


class PlaybackId(Base, PublicIdMixin, TimestampMixin):
    """Indirection between a stable asset and the URL the world sees, so access can be
    rotated or revoked without touching the asset."""

    __tablename__ = "playback_ids"

    asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), index=True
    )
    live_stream_id: Mapped[str | None] = mapped_column(String(40), index=True)
    policy: Mapped[str] = mapped_column(String(16), default="public", nullable=False)

    asset: Mapped[Asset | None] = relationship(back_populates="playback_ids")


class AssetTrack(Base, PublicIdMixin, TimestampMixin):
    __tablename__ = "asset_tracks"

    asset_id: Mapped[str] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), index=True, nullable=False
    )
    type: Mapped[str] = mapped_column(String(16), nullable=False)  # video | audio | text
    status: Mapped[str] = mapped_column(String(32), default="ready", nullable=False)

    duration: Mapped[float | None] = mapped_column(Float)
    max_width: Mapped[int | None] = mapped_column(Integer)
    max_height: Mapped[int | None] = mapped_column(Integer)
    max_frame_rate: Mapped[float | None] = mapped_column(Float)
    max_channels: Mapped[int | None] = mapped_column(Integer)
    sample_rate: Mapped[int | None] = mapped_column(Integer)
    codec: Mapped[str | None] = mapped_column(String(64))
    bitrate: Mapped[int | None] = mapped_column(Integer)

    # Text-track fields
    text_type: Mapped[str | None] = mapped_column(String(32))  # subtitles | captions
    language_code: Mapped[str | None] = mapped_column(String(16))
    name: Mapped[str | None] = mapped_column(String(255))
    closed_captions: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    object_key: Mapped[str | None] = mapped_column(String(512))
    source_url: Mapped[str | None] = mapped_column(String(2048))
    generated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_messages: Mapped[list | None] = mapped_column(JSON)

    asset: Mapped[Asset] = relationship(back_populates="tracks")


class StaticRendition(Base, PublicIdMixin, TimestampMixin):
    """Progressive MP4 for download / offline playback."""

    __tablename__ = "static_renditions"

    asset_id: Mapped[str] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    ext: Mapped[str] = mapped_column(String(8), default="mp4", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="preparing", nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    bitrate: Mapped[int | None] = mapped_column(Integer)
    filesize: Mapped[int | None] = mapped_column(Integer)
    object_key: Mapped[str | None] = mapped_column(String(512))

    asset: Mapped[Asset] = relationship(back_populates="static_renditions")


class DirectUpload(Base, PublicIdMixin, TimestampMixin):
    __tablename__ = "direct_uploads"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default="waiting", nullable=False, index=True)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    upload_url: Mapped[str | None] = mapped_column(String(4096))
    cors_origin: Mapped[str | None] = mapped_column(String(255))
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=3600, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    new_asset_settings: Mapped[dict | None] = mapped_column(JSON)
    test: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    asset_id: Mapped[str | None] = mapped_column(String(40), index=True)
    error_type: Mapped[str | None] = mapped_column(String(64))
    error_messages: Mapped[list | None] = mapped_column(JSON)


class LiveStream(Base, PublicIdMixin, TimestampMixin):
    __tablename__ = "live_streams"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default="idle", nullable=False, index=True)
    stream_key: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    latency_mode: Mapped[str] = mapped_column(String(32), default="standard", nullable=False)
    reconnect_window: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    max_continuous_duration: Mapped[int] = mapped_column(Integer, default=43200, nullable=False)
    new_asset_settings: Mapped[dict | None] = mapped_column(JSON)
    passthrough: Mapped[str | None] = mapped_column(String(255))
    test: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active_asset_id: Mapped[str | None] = mapped_column(String(40))
    recent_asset_ids: Mapped[list | None] = mapped_column(JSON)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlaybackRestriction(Base, PublicIdMixin, TimestampMixin):
    __tablename__ = "playback_restrictions"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    referrer_allowed_domains: Mapped[list | None] = mapped_column(JSON)
    referrer_allow_no_referrer: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    user_agent_block_bots: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


Index("ix_assets_project_status_created", Asset.project_id, Asset.status, Asset.created_at)
Index("ix_uploads_status_expires", DirectUpload.status, DirectUpload.expires_at)
