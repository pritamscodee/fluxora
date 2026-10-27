"""AI-backed processing jobs: transcription, summaries and chapters.

These run on the media worker rather than the agent tool bus, because they are long,
CPU- and API-bound jobs on a single asset — the same shape as a transcode. The agent
queues them and reports that they are in flight; it does not wait on them.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Asset, AssetTrack, Job
from app.services import events, media, storage

log = logging.getLogger("fluxora.worker.ai")

# Whisper rejects anything over 25 MB; 16 kHz mono PCM is about 32 kB/s, so this is
# roughly 13 minutes of audio. Longer assets are truncated rather than failed.
MAX_TRANSCRIBE_SECONDS = 780.0


class AIUnavailable(RuntimeError):
    pass


def _client():
    if not settings.openai_api_key:
        raise AIUnavailable(
            "OPENAI_API_KEY is not set; transcription and AI summaries are unavailable"
        )
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=settings.openai_api_key, timeout=600.0)


def _vtt_timestamp(seconds: float) -> str:
    hours, rem = divmod(max(seconds, 0.0), 3600)
    minutes, secs = divmod(rem, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{secs:06.3f}"


def segments_to_vtt(segments: list[dict]) -> str:
    lines = ["WEBVTT", ""]
    for index, seg in enumerate(segments, start=1):
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        lines += [
            str(index),
            f"{_vtt_timestamp(float(seg.get('start', 0)))} --> "
            f"{_vtt_timestamp(float(seg.get('end', 0)))}",
            text,
            "",
        ]
    return "\n".join(lines)


async def analyze_asset(db: AsyncSession, job: Job) -> dict:
    """Dispatch on `task`: captions, summary, or both."""
    asset = await db.scalar(select(Asset).where(Asset.id == job.payload["asset_id"]))
    if asset is None or asset.status != "ready":
        return {"skipped": "asset is not ready"}

    task = job.payload.get("task", "captions")
    if task == "captions":
        return await _captions(db, job, asset)
    if task == "summary":
        return await _summary(db, job, asset)
    return {"skipped": f"unknown ai task {task!r}"}


async def _captions(db: AsyncSession, job: Job, asset: Asset) -> dict:
    track = await db.scalar(
        select(AssetTrack).where(AssetTrack.id == job.payload.get("track_id", ""))
    )
    if track is None:
        return {"skipped": "track row is gone"}

    async def fail(message: str) -> dict:
        track.status = "errored"
        track.error_messages = [message]
        await db.flush()
        await events.emit(
            db,
            project_id=asset.project_id,
            event_type="video.asset.track.errored",
            object_type="asset",
            object_id=asset.id,
            data={"track_id": track.id, "errors": [message]},
        )
        await db.commit()
        return {"errored": message}

    try:
        client = _client()
    except AIUnavailable as exc:
        return await fail(str(exc))

    store = storage.get_storage()
    with TemporaryDirectory(dir=settings.media_scratch_dir) as tmp:
        scratch = Path(tmp)
        try:
            source = await store.download(asset.source_key, scratch / "source")
            audio = await media.extract_audio_wav(
                source, scratch / "audio.wav", max_seconds=MAX_TRANSCRIBE_SECONDS
            )
        except Exception as exc:
            return await fail(f"could not extract audio: {type(exc).__name__}: {exc}")

        try:
            with audio.open("rb") as handle:
                response = await client.audio.transcriptions.create(
                    model="whisper-1",
                    file=handle,
                    response_format="verbose_json",
                    language=job.payload.get("language_code") or None,
                )
        except Exception as exc:
            return await fail(f"transcription failed: {type(exc).__name__}: {exc}")

        payload = response if isinstance(response, dict) else response.model_dump()
        segments = payload.get("segments") or []
        if not segments:
            text = (payload.get("text") or "").strip()
            if not text:
                return await fail("the transcription came back empty")
            segments = [{"start": 0.0, "end": asset.duration or 0.0, "text": text}]

        vtt = segments_to_vtt(segments)
        key = storage.track_key(asset.project_id, asset.id, track.id, "vtt")
        await store.put_bytes(key, vtt.encode(), "text/vtt")

    track.object_key = key
    track.status = "ready"
    track.generated = True
    track.error_messages = None
    await db.flush()
    await events.emit(
        db,
        project_id=asset.project_id,
        event_type="video.asset.track.ready",
        object_type="asset",
        object_id=asset.id,
        data={
            "track_id": track.id,
            "language_code": track.language_code,
            "generated": True,
        },
    )
    await db.commit()
    return {"track_id": track.id, "segments": len(segments)}


async def _summary(db: AsyncSession, job: Job, asset: Asset) -> dict:
    """Summarise an asset from its transcript. Needs captions to exist first."""
    track = await db.scalar(
        select(AssetTrack).where(
            AssetTrack.asset_id == asset.id,
            AssetTrack.type == "text",
            AssetTrack.status == "ready",
        )
    )
    if track is None or not track.object_key:
        return {"skipped": "no transcript is available to summarise"}

    try:
        client = _client()
    except AIUnavailable as exc:
        return {"errored": str(exc)}

    transcript = (await storage.get_storage().get_bytes(track.object_key)).decode(
        errors="replace"
    )
    # Strip cue numbers and timecodes so the model sees prose, not WEBVTT scaffolding.
    body = "\n".join(
        line
        for line in transcript.splitlines()
        if line.strip() and "-->" not in line and not line.strip().isdigit()
        and line.strip() != "WEBVTT"
    )[:40000]

    try:
        completion = await client.chat.completions.create(
            model=settings.openai_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Summarise this video transcript. Respond with JSON: "
                        '{"summary": "<=120 words", "chapters": '
                        '[{"start": <seconds>, "title": "short title"}]}. '
                        "Use at most 8 chapters. Base everything on the transcript only."
                    ),
                },
                {"role": "user", "content": body},
            ],
        )
        parsed = json.loads(completion.choices[0].message.content or "{}")
    except Exception as exc:
        log.warning("summary failed for asset %s: %s", asset.id, exc)
        return {"errored": f"{type(exc).__name__}: {exc}"}

    asset.ai_summary = str(parsed.get("summary", ""))[:4000]
    chapters = []
    for chapter in (parsed.get("chapters") or [])[:8]:
        try:
            start = float(chapter.get("start", 0))
        except (TypeError, ValueError):
            continue
        if asset.duration and not 0 <= start <= asset.duration:
            continue
        chapters.append({"start": start, "title": str(chapter.get("title", ""))[:200]})
    asset.ai_chapters = sorted(chapters, key=lambda c: c["start"])
    await db.flush()
    await events.emit(
        db,
        project_id=asset.project_id,
        event_type="video.asset.updated",
        object_type="asset",
        object_id=asset.id,
        data={"ai_summary": True, "chapters": len(asset.ai_chapters)},
    )
    await db.commit()
    return {"asset_id": asset.id, "chapters": len(asset.ai_chapters)}
