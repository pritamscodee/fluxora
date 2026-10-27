from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator

from app.schemas.common import ApiModel, RequestModel

PlaybackPolicy = Literal["public", "signed"]
Mp4Support = Literal["none", "standard", "capped-1080p"]
MasterAccess = Literal["none", "temporary"]
TrackType = Literal["video", "audio", "text"]
TextType = Literal["subtitles", "captions"]


# ---- Inputs ----

class AssetInput(RequestModel):
    """One element of `input[]`. A bare `url` is the video source; adding `type: "text"`
    makes it a sidecar subtitle track, matching the shape ingest clients already use."""

    url: HttpUrl
    type: TrackType = "video"
    text_type: TextType | None = None
    language_code: str | None = Field(default=None, max_length=16)
    name: str | None = Field(default=None, max_length=255)
    closed_captions: bool = False
    start_time: float | None = Field(default=None, ge=0)
    end_time: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def check_window(self):
        if self.start_time is not None and self.end_time is not None:
            if self.end_time <= self.start_time:
                raise ValueError("end_time must be greater than start_time")
        if self.type == "text" and not self.language_code:
            raise ValueError("language_code is required for text inputs")
        return self


class NewAssetSettings(RequestModel):
    playback_policy: list[PlaybackPolicy] = Field(default_factory=lambda: ["public"])
    mp4_support: Mp4Support = "none"
    master_access: MasterAccess = "none"
    normalize_audio: bool = False
    passthrough: str | None = Field(default=None, max_length=255)
    test: bool = False
    encoding_tier: Literal["baseline", "smart"] = "smart"
    input: list[AssetInput] = Field(default_factory=list)


class AssetCreate(NewAssetSettings):
    input: list[AssetInput] = Field(min_length=1)

    @model_validator(mode="after")
    def exactly_one_video_source(self):
        videos = [i for i in self.input if i.type == "video"]
        if len(videos) != 1:
            raise ValueError("input must contain exactly one entry of type 'video'")
        return self


class AssetUpdate(RequestModel):
    passthrough: str | None = Field(default=None, max_length=255)
    mp4_support: Mp4Support | None = None
    master_access: MasterAccess | None = None


class ClipCreate(RequestModel):
    start_time: float = Field(ge=0)
    end_time: float = Field(gt=0)
    playback_policy: list[PlaybackPolicy] = Field(default_factory=lambda: ["public"])
    mp4_support: Mp4Support = "none"
    passthrough: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be greater than start_time")
        if self.end_time - self.start_time < 0.5:
            raise ValueError("a clip must be at least 0.5 seconds long")
        return self


class PlaybackIdCreate(RequestModel):
    policy: PlaybackPolicy = "public"


class TrackCreate(RequestModel):
    url: HttpUrl
    type: Literal["text", "audio"] = "text"
    text_type: TextType = "subtitles"
    language_code: str = Field(max_length=16)
    name: str | None = Field(default=None, max_length=255)
    closed_captions: bool = False
    passthrough: str | None = Field(default=None, max_length=255)


class UploadCreate(RequestModel):
    new_asset_settings: NewAssetSettings = Field(default_factory=NewAssetSettings)
    cors_origin: str | None = Field(default=None, max_length=255)
    timeout: int = Field(default=3600, ge=60, le=604800)
    test: bool = False


class LiveStreamCreate(RequestModel):
    playback_policy: list[PlaybackPolicy] = Field(default_factory=lambda: ["public"])
    new_asset_settings: NewAssetSettings = Field(default_factory=NewAssetSettings)
    latency_mode: Literal["standard", "reduced", "low"] = "standard"
    reconnect_window: int = Field(default=60, ge=0, le=1800)
    max_continuous_duration: int = Field(default=43200, ge=60, le=43200)
    passthrough: str | None = Field(default=None, max_length=255)
    test: bool = False


class LiveStreamUpdate(RequestModel):
    latency_mode: Literal["standard", "reduced", "low"] | None = None
    reconnect_window: int | None = Field(default=None, ge=0, le=1800)
    max_continuous_duration: int | None = Field(default=None, ge=60, le=43200)
    passthrough: str | None = Field(default=None, max_length=255)


# ---- Outputs ----

class PlaybackIdOut(ApiModel):
    id: str
    policy: str


class TrackOut(ApiModel):
    id: str
    type: str
    status: str
    duration: float | None = None
    max_width: int | None = None
    max_height: int | None = None
    max_frame_rate: float | None = None
    max_channels: int | None = None
    codec: str | None = None
    bitrate: int | None = None
    text_type: str | None = None
    language_code: str | None = None
    name: str | None = None
    closed_captions: bool = False
    generated: bool = False


class StaticRenditionOut(ApiModel):
    id: str
    name: str
    ext: str
    status: str
    width: int | None = None
    height: int | None = None
    bitrate: int | None = None
    filesize: int | None = None


class AssetErrors(BaseModel):
    type: str
    messages: list[str]


class AssetOut(ApiModel):
    id: str
    status: str
    created_at: datetime
    duration: float | None = None
    aspect_ratio: str | None = None
    max_stored_resolution: str | None = None
    max_stored_frame_rate: float | None = None
    mp4_support: str
    master_access: str
    normalize_audio: bool
    passthrough: str | None = None
    test: bool
    encoding_tier: str
    source_asset_id: str | None = None
    live_stream_id: str | None = None
    upload_id: str | None = None
    playback_ids: list[PlaybackIdOut] = Field(default_factory=list)
    tracks: list[TrackOut] = Field(default_factory=list)
    static_renditions: list[StaticRenditionOut] = Field(default_factory=list)
    errors: AssetErrors | None = None
    ai_summary: str | None = None
    ai_chapters: list | None = None

    @staticmethod
    def of(asset) -> "AssetOut":
        out = AssetOut.model_validate(asset)
        if asset.error_type:
            out.errors = AssetErrors(type=asset.error_type, messages=asset.error_messages or [])
        return out


class UploadOut(ApiModel):
    id: str
    status: str
    url: str | None = Field(default=None, validation_alias="upload_url")
    timeout: int = Field(validation_alias="timeout_seconds")
    cors_origin: str | None = None
    test: bool
    asset_id: str | None = None
    new_asset_settings: dict | None = None
    created_at: datetime
    errors: AssetErrors | None = None

    @staticmethod
    def of(upload) -> "UploadOut":
        out = UploadOut.model_validate(upload)
        if upload.error_type:
            out.errors = AssetErrors(type=upload.error_type, messages=upload.error_messages or [])
        # The presigned URL is a write capability; never hand it back once it is spent.
        if upload.status != "waiting":
            out.url = None
        return out


class LiveStreamOut(ApiModel):
    id: str
    status: str
    stream_key: str
    latency_mode: str
    reconnect_window: int
    max_continuous_duration: int
    passthrough: str | None = None
    test: bool
    active_asset_id: str | None = None
    recent_asset_ids: list | None = None
    created_at: datetime
    playback_ids: list[PlaybackIdOut] = Field(default_factory=list)
    rtmp_url: str | None = None


class PlaybackIdResolved(BaseModel):
    object_type: str = Field(serialization_alias="type")
    object_id: str = Field(serialization_alias="id")
    policy: str
