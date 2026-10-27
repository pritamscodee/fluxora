"""The agent's tool surface.

Every tool is an explicitly registered Python function with a Pydantic argument schema and
a declared risk level. The model never emits code or shell — it emits a tool name and a
JSON object, which is validated before anything runs (ADR-0001 decision 7).

Each tool is also scoped to the project the run belongs to. A tool cannot be aimed at
another tenant's asset even if the model is told to: the project id comes from the
authenticated run, never from model output.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Asset, AssetTrack, PlaybackId, Project, utcnow
from app.schemas.video import ClipCreate
from app.services import assets as asset_service
from app.services import events, jobs, storage

log = logging.getLogger("fluxora.agents.tools")


class Risk(str, Enum):
    """How much a tool can cost or destroy.

    READ is auto-approved. WRITE changes metadata and is cheap to undo. EXPENSIVE starts
    transcoding work that costs real CPU and money. DESTRUCTIVE cannot be undone. The last
    two require explicit human approval before the graph will run them.
    """

    READ = "read"
    WRITE = "write"
    EXPENSIVE = "expensive"
    DESTRUCTIVE = "destructive"

    @property
    def needs_approval(self) -> bool:
        return self in (Risk.EXPENSIVE, Risk.DESTRUCTIVE)


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool is allowed to know. Deliberately narrow."""

    db: AsyncSession
    project: Project
    run_id: str


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_schema: type[BaseModel]
    risk: Risk
    handler: Callable[[ToolContext, BaseModel], Awaitable[dict]]

    def json_schema(self) -> dict:
        schema = self.args_schema.model_json_schema()
        schema.pop("title", None)
        return {
            "name": self.name,
            "description": self.description,
            "risk": self.risk.value,
            "parameters": schema,
        }


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, args: type[BaseModel], risk: Risk):
    def register(fn):
        REGISTRY[name] = Tool(
            name=name, description=description, args_schema=args, risk=risk, handler=fn
        )
        return fn

    return register


# ---------------------------------------------------------------- argument schemas

class ListAssetsArgs(BaseModel):
    model_config = {"extra": "forbid"}
    status: str | None = Field(
        default=None, description="Filter by status: preparing, ready, or errored."
    )
    limit: int = Field(default=10, ge=1, le=50)


class AssetRefArgs(BaseModel):
    model_config = {"extra": "forbid"}
    asset_id: str = Field(max_length=40, description="The asset to act on.")


class ClipArgs(BaseModel):
    model_config = {"extra": "forbid"}
    asset_id: str = Field(max_length=40)
    start_time: float = Field(ge=0, description="Clip in-point in seconds.")
    end_time: float = Field(gt=0, description="Clip out-point in seconds.")
    passthrough: str | None = Field(default=None, max_length=255)


class ThumbnailArgs(BaseModel):
    model_config = {"extra": "forbid"}
    asset_id: str = Field(max_length=40)
    time: float = Field(default=0.0, ge=0, description="Timestamp in seconds.")
    width: int | None = Field(default=None, ge=16, le=3840)


class Mp4Args(BaseModel):
    model_config = {"extra": "forbid"}
    asset_id: str = Field(max_length=40)
    level: str = Field(
        default="standard", description="Either 'standard' or 'capped-1080p'."
    )


class SubtitleArgs(BaseModel):
    model_config = {"extra": "forbid"}
    asset_id: str = Field(max_length=40)
    language_code: str = Field(default="en", max_length=16)
    name: str | None = Field(default=None, max_length=255)


class SummaryArgs(BaseModel):
    model_config = {"extra": "forbid"}
    asset_id: str = Field(max_length=40)
    summary: str = Field(max_length=4000, description="The written summary to store.")
    chapters: list[dict] | None = Field(
        default=None, description="Optional chapter markers: [{start, title}]."
    )


class PlaybackArgs(BaseModel):
    model_config = {"extra": "forbid"}
    asset_id: str = Field(max_length=40)
    policy: str = Field(default="public", description="Either 'public' or 'signed'.")


# ---------------------------------------------------------------- helpers

async def _asset(ctx: ToolContext, asset_id: str) -> Asset:
    # Scoped lookup: project_id comes from the run, so a model-supplied id belonging to
    # another tenant simply does not resolve.
    return await asset_service.get_for_project(ctx.db, ctx.project.id, asset_id)


def _describe(asset: Asset) -> dict:
    return {
        "asset_id": asset.id,
        "status": asset.status,
        "duration": asset.duration,
        "aspect_ratio": asset.aspect_ratio,
        "resolution": asset.max_stored_resolution,
        "mp4_support": asset.mp4_support,
        "test": asset.test,
        "passthrough": asset.passthrough,
        "created_at": asset.created_at.isoformat() if asset.created_at else None,
        "playback_ids": [
            {"id": p.id, "policy": p.policy} for p in (asset.playback_ids or [])
        ],
        "has_summary": bool(asset.ai_summary),
    }


# ---------------------------------------------------------------- tools

@tool(
    "list_assets",
    "List video assets in this project, newest first. Use this to find an asset "
    "when the user describes one but does not give an id.",
    ListAssetsArgs,
    Risk.READ,
)
async def list_assets(ctx: ToolContext, args: ListAssetsArgs) -> dict:
    query = select(Asset).where(Asset.project_id == ctx.project.id, Asset.status != "deleted")
    if args.status:
        query = query.where(Asset.status == args.status)
    rows = (
        await ctx.db.scalars(query.order_by(Asset.created_at.desc()).limit(args.limit))
    ).all()
    total = await ctx.db.scalar(
        select(func.count())
        .select_from(Asset)
        .where(Asset.project_id == ctx.project.id, Asset.status != "deleted")
    )
    return {"assets": [_describe(a) for a in rows], "total_in_project": int(total or 0)}


@tool(
    "get_asset",
    "Fetch full detail for one asset: status, duration, tracks, renditions and any "
    "error. Call this before acting on an asset so decisions use current state.",
    AssetRefArgs,
    Risk.READ,
)
async def get_asset(ctx: ToolContext, args: AssetRefArgs) -> dict:
    asset = await _asset(ctx, args.asset_id)
    detail = _describe(asset)
    detail["tracks"] = [
        {
            "type": t.type,
            "status": t.status,
            "language_code": t.language_code,
            "codec": t.codec,
            "max_height": t.max_height,
        }
        for t in (asset.tracks or [])
    ]
    detail["static_renditions"] = [
        {"name": r.name, "status": r.status, "filesize": r.filesize}
        for r in (asset.static_renditions or [])
    ]
    if asset.error_type:
        detail["error"] = {"type": asset.error_type, "messages": asset.error_messages}
    detail["ai_summary"] = asset.ai_summary
    detail["ai_chapters"] = asset.ai_chapters
    return detail


@tool(
    "create_clip",
    "Cut a new asset from a time range of an existing ready asset. This starts a "
    "transcode and is billable.",
    ClipArgs,
    Risk.EXPENSIVE,
)
async def create_clip(ctx: ToolContext, args: ClipArgs) -> dict:
    source = await _asset(ctx, args.asset_id)
    # Re-validated through the same schema the HTTP API uses, so the agent path cannot
    # create a clip the public API would have rejected.
    body = ClipCreate(
        start_time=args.start_time,
        end_time=args.end_time,
        playback_policy=["public"],
        passthrough=args.passthrough,
    )
    clip = await asset_service.create_clip(ctx.db, ctx.project, source, body)
    return {
        "clip_asset_id": clip.id,
        "status": clip.status,
        "source_asset_id": source.id,
        "start_time": args.start_time,
        "end_time": clip.clip_end,
    }


@tool(
    "generate_thumbnail",
    "Produce a thumbnail URL for an asset at a given timestamp.",
    ThumbnailArgs,
    Risk.READ,
)
async def generate_thumbnail(ctx: ToolContext, args: ThumbnailArgs) -> dict:
    asset = await _asset(ctx, args.asset_id)
    if asset.status != "ready":
        return {"error": f"asset is {asset.status}; thumbnails need a ready asset"}
    if not asset.playback_ids:
        return {"error": "asset has no playback id"}
    if asset.duration and args.time > asset.duration:
        return {
            "error": f"time {args.time}s is beyond the asset duration of {asset.duration:.2f}s"
        }
    playback_id = asset.playback_ids[0].id
    query = f"?time={args.time}" + (f"&width={args.width}" if args.width else "")
    return {
        "url": f"{settings.api_base_url}/v1/stream/{playback_id}/thumbnail.jpg{query}",
        "asset_id": asset.id,
        "time": args.time,
    }


@tool(
    "enable_mp4_downloads",
    "Turn on progressive MP4 renditions for an asset so it can be downloaded or "
    "played offline. This starts encoding work and is billable.",
    Mp4Args,
    Risk.EXPENSIVE,
)
async def enable_mp4_downloads(ctx: ToolContext, args: Mp4Args) -> dict:
    if args.level not in ("standard", "capped-1080p"):
        return {"error": "level must be 'standard' or 'capped-1080p'"}
    asset = await _asset(ctx, args.asset_id)
    if asset.status != "ready":
        return {"error": f"asset is {asset.status}; mp4 support needs a ready asset"}
    if asset.mp4_support == args.level:
        return {"asset_id": asset.id, "mp4_support": args.level, "already_enabled": True}

    asset.mp4_support = args.level
    asset.updated_at = utcnow()
    await ctx.db.flush()
    await jobs.enqueue(
        ctx.db,
        jobs.RENDER_MP4,
        {"asset_id": asset.id, "mp4_support": args.level},
        idempotency_key=f"mp4:{asset.id}:{args.level}",
        project_id=ctx.project.id,
        lease_seconds=7200,
    )
    await events.emit(
        ctx.db,
        project_id=ctx.project.id,
        event_type="video.asset.updated",
        object_type="asset",
        object_id=asset.id,
        data={"mp4_support": args.level, "changed_by": "agent", "run_id": ctx.run_id},
    )
    return {"asset_id": asset.id, "mp4_support": args.level, "encoding_queued": True}


@tool(
    "generate_captions",
    "Generate subtitles for an asset by transcribing its audio. This starts "
    "processing work and is billable.",
    SubtitleArgs,
    Risk.EXPENSIVE,
)
async def generate_captions(ctx: ToolContext, args: SubtitleArgs) -> dict:
    asset = await _asset(ctx, args.asset_id)
    if asset.status != "ready":
        return {"error": f"asset is {asset.status}; captions need a ready asset"}

    existing = next(
        (
            t
            for t in (asset.tracks or [])
            if t.type == "text" and t.language_code == args.language_code
        ),
        None,
    )
    if existing and existing.status == "ready":
        return {
            "asset_id": asset.id,
            "track_id": existing.id,
            "language_code": args.language_code,
            "already_present": True,
        }

    track = existing or AssetTrack(
        asset_id=asset.id,
        type="text",
        text_type="subtitles",
        language_code=args.language_code,
        name=args.name or args.language_code,
        generated=True,
    )
    track.status = "preparing"
    track.generated = True
    if existing is None:
        ctx.db.add(track)
    await ctx.db.flush()

    await jobs.enqueue(
        ctx.db,
        jobs.AI_ANALYZE,
        {"asset_id": asset.id, "track_id": track.id, "task": "captions",
         "language_code": args.language_code},
        idempotency_key=f"captions:{asset.id}:{args.language_code}",
        project_id=ctx.project.id,
        lease_seconds=3600,
    )
    return {
        "asset_id": asset.id,
        "track_id": track.id,
        "language_code": args.language_code,
        "transcription_queued": True,
    }


@tool(
    "save_asset_summary",
    "Store a written summary, and optionally chapter markers, on an asset. Use this "
    "after you have read enough about the asset to describe it.",
    SummaryArgs,
    Risk.WRITE,
)
async def save_asset_summary(ctx: ToolContext, args: SummaryArgs) -> dict:
    asset = await _asset(ctx, args.asset_id)
    asset.ai_summary = args.summary
    if args.chapters is not None:
        # Normalised and bounded here rather than trusted: chapter data is model output.
        cleaned = []
        for chapter in args.chapters[:50]:
            if not isinstance(chapter, dict):
                continue
            try:
                start = float(chapter.get("start", 0))
            except (TypeError, ValueError):
                continue
            if asset.duration and not 0 <= start <= asset.duration:
                continue
            cleaned.append({"start": start, "title": str(chapter.get("title", ""))[:200]})
        asset.ai_chapters = sorted(cleaned, key=lambda c: c["start"])
    asset.updated_at = utcnow()
    await ctx.db.flush()
    await events.emit(
        ctx.db,
        project_id=ctx.project.id,
        event_type="video.asset.updated",
        object_type="asset",
        object_id=asset.id,
        data={"ai_summary": True, "changed_by": "agent", "run_id": ctx.run_id},
    )
    return {
        "asset_id": asset.id,
        "summary_saved": True,
        "chapters": len(asset.ai_chapters or []),
    }


@tool(
    "add_playback_id",
    "Mint an additional playback id for an asset, public or signed. Use 'signed' when "
    "access should require a token.",
    PlaybackArgs,
    Risk.WRITE,
)
async def add_playback_id(ctx: ToolContext, args: PlaybackArgs) -> dict:
    if args.policy not in ("public", "signed"):
        return {"error": "policy must be 'public' or 'signed'"}
    asset = await _asset(ctx, args.asset_id)
    playback = PlaybackId(asset_id=asset.id, policy=args.policy)
    ctx.db.add(playback)
    await ctx.db.flush()
    return {"asset_id": asset.id, "playback_id": playback.id, "policy": args.policy}


@tool(
    "delete_asset",
    "Permanently delete an asset and stop its playback. This cannot be undone.",
    AssetRefArgs,
    Risk.DESTRUCTIVE,
)
async def delete_asset(ctx: ToolContext, args: AssetRefArgs) -> dict:
    asset = await _asset(ctx, args.asset_id)
    await asset_service.soft_delete(ctx.db, asset)
    return {"asset_id": asset.id, "deleted": True}


@tool(
    "project_usage",
    "Report this project's asset counts and metered usage totals.",
    type("NoArgs", (BaseModel,), {"model_config": {"extra": "forbid"}}),
    Risk.READ,
)
async def project_usage(ctx: ToolContext, args: BaseModel) -> dict:
    from app.models import UsageRecord

    by_status = (
        await ctx.db.execute(
            select(Asset.status, func.count())
            .where(Asset.project_id == ctx.project.id)
            .group_by(Asset.status)
        )
    ).all()
    usage = (
        await ctx.db.execute(
            select(UsageRecord.metric, func.sum(UsageRecord.value))
            .where(UsageRecord.project_id == ctx.project.id)
            .group_by(UsageRecord.metric)
        )
    ).all()
    return {
        "project": ctx.project.name,
        "environment": ctx.project.environment,
        "assets_by_status": {status: int(count) for status, count in by_status},
        "usage_totals": {metric: round(float(total or 0), 4) for metric, total in usage},
    }


# ---------------------------------------------------------------- invocation

class ToolError(Exception):
    """A tool could not run as asked. Returned to the model as an observation so it can
    adapt, rather than crashing the run."""


def tool_catalogue() -> list[dict]:
    return [t.json_schema() for t in REGISTRY.values()]


def get_tool(name: str) -> Tool:
    tool_def = REGISTRY.get(name)
    if tool_def is None:
        raise ToolError(
            f"unknown tool {name!r}; available tools are {sorted(REGISTRY)}"
        )
    return tool_def


def validate_args(name: str, raw: dict) -> BaseModel:
    """Validate model-produced arguments before anything executes.

    This is the single enforcement point for argument shape. A `ValidationError` here is
    the system working: the model guessed, and the guess is rejected with a message it
    can use to correct itself.
    """
    tool_def = get_tool(name)
    from pydantic import ValidationError

    try:
        return tool_def.args_schema.model_validate(raw or {})
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ToolError(f"invalid arguments for {name}: {problems}") from exc


async def execute(ctx: ToolContext, name: str, raw_args: dict) -> dict:
    tool_def = get_tool(name)
    args = validate_args(name, raw_args)
    log.info("run %s executing %s risk=%s", ctx.run_id, name, tool_def.risk.value)
    try:
        return await tool_def.handler(ctx, args)
    except ToolError:
        raise
    except Exception as exc:
        # Surfaced as a tool-level failure, not a 500: the run should be able to report
        # what went wrong and continue or stop cleanly.
        log.exception("tool %s failed in run %s", name, ctx.run_id)
        raise ToolError(f"{name} failed: {type(exc).__name__}: {exc}") from exc
