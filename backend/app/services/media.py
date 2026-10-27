"""FFmpeg/FFprobe orchestration.

Every invocation is an argv list passed to `create_subprocess_exec` — never a shell string.
That is a hard rule: filenames, filter graphs, and (in the agentic phases) model-chosen
parameters all flow in here, and a shell would turn any of them into an injection surface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings

log = logging.getLogger("fluxora.media")


class MediaError(RuntimeError):
    """ffmpeg/ffprobe failed. Carries the tail of stderr, which is where the cause is."""

    def __init__(self, message: str, stderr: str = "", argv: list[str] | None = None):
        self.stderr_tail = stderr[-4000:]
        self.argv = argv or []
        super().__init__(message)


# ---- ABR ladder ----

@dataclass(frozen=True)
class Rung:
    name: str
    height: int
    video_bitrate_k: int
    audio_bitrate_k: int

    @property
    def maxrate_k(self) -> int:
        return int(self.video_bitrate_k * 1.07)

    @property
    def bufsize_k(self) -> int:
        return self.video_bitrate_k * 2


# H.264 High profile targets at ~30fps. Deliberately fixed rather than content-adaptive:
# a predictable ladder is far easier to reason about than a per-asset guess (ADR-0001).
LADDER: tuple[Rung, ...] = (
    Rung("270p", 270, 400, 64),
    Rung("360p", 360, 800, 96),
    Rung("480p", 480, 1400, 128),
    Rung("720p", 720, 2800, 128),
    Rung("1080p", 1080, 5000, 192),
    Rung("1440p", 1440, 8000, 192),
    Rung("2160p", 2160, 14000, 192),
)

MP4_RENDITIONS = {"low": "270p", "medium": "480p", "high": "720p", "highest": "1080p"}


def select_ladder(source_height: int, encoding_tier: str = "smart") -> list[Rung]:
    """Pick rungs at or below the source height — upscaling only burns CPU and bytes.

    `baseline` gives a single rung for cheap/short content; `smart` gives the full ladder
    up to the source. A source smaller than the lowest rung still gets exactly one rung so
    there is always something to play.
    """
    usable = [r for r in LADDER if r.height <= source_height]
    if not usable:
        return [LADDER[0]]
    if encoding_tier == "baseline":
        return [usable[-1]]
    # Keep the top rung plus everything beneath it, capped at 5 so a 4K source does not
    # produce seven renditions nobody requests.
    return usable[-5:]


# ---- Process helpers ----

async def _run(
    argv: list[str], timeout: float = 7200, cwd: str | Path | None = None
) -> tuple[str, str]:
    log.debug("exec %s", " ".join(argv[:12]))
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise MediaError(f"timed out after {timeout}s", argv=argv) from None
    stdout, stderr = out.decode(errors="replace"), err.decode(errors="replace")
    if proc.returncode != 0:
        raise MediaError(f"exited with code {proc.returncode}", stderr, argv)
    return stdout, stderr


def ensure_binaries() -> None:
    for binary in (settings.ffmpeg_path, settings.ffprobe_path):
        if shutil.which(binary) is None and not Path(binary).exists():
            raise MediaError(f"{binary} not found on PATH")


# ---- Probing ----

@dataclass
class ProbeResult:
    duration: float
    width: int
    height: int
    frame_rate: float
    video_codec: str | None
    audio_codec: str | None
    audio_channels: int | None
    audio_sample_rate: int | None
    bitrate: int | None
    has_video: bool
    has_audio: bool
    raw: dict

    @property
    def aspect_ratio(self) -> str:
        if not self.width or not self.height:
            return "16:9"
        divisor = math.gcd(self.width, self.height)
        return f"{self.width // divisor}:{self.height // divisor}"

    @property
    def max_stored_resolution(self) -> str:
        h = self.height
        for label, threshold in (("UHD", 2160), ("QHD", 1440), ("FHD", 1080), ("HD", 720)):
            if h >= threshold:
                return label
        return "SD"


async def probe(path: str | Path) -> ProbeResult:
    stdout, _ = await _run(
        [
            settings.ffprobe_path,
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout=120,
    )
    raw = json.loads(stdout)
    streams = raw.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = raw.get("format", {})

    if video is None and audio is None:
        raise MediaError("input contains no decodable audio or video stream")

    duration = float(fmt.get("duration") or (video or audio or {}).get("duration") or 0.0)
    return ProbeResult(
        duration=duration,
        width=int(video.get("width") or 0) if video else 0,
        height=int(video.get("height") or 0) if video else 0,
        frame_rate=_parse_rate(video.get("avg_frame_rate") if video else None),
        video_codec=video.get("codec_name") if video else None,
        audio_codec=audio.get("codec_name") if audio else None,
        audio_channels=int(audio["channels"]) if audio and audio.get("channels") else None,
        audio_sample_rate=int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
        bitrate=int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
        has_video=video is not None,
        has_audio=audio is not None,
        raw=raw,
    )


def _parse_rate(value: str | None) -> float:
    if not value or "/" not in value:
        try:
            return float(value) if value else 0.0
        except ValueError:
            return 0.0
    num, _, den = value.partition("/")
    try:
        d = float(den)
        return float(num) / d if d else 0.0
    except ValueError:
        return 0.0


# ---- Loudness normalisation ----

_LOUDNORM_KEYS = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")


async def measure_loudness(path: str | Path) -> dict[str, str]:
    """Pass 1 of EBU R128 two-pass normalisation. One-pass loudnorm cannot hit a target
    accurately because it has not seen the whole programme yet."""
    _, stderr = await _run(
        [
            settings.ffmpeg_path, "-nostdin", "-hide_banner",
            "-i", str(path),
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
            "-f", "null", "-",
        ],
        timeout=3600,
    )
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr, re.DOTALL)
    if not match:
        log.warning("loudnorm measurement produced no JSON; skipping normalisation")
        return {}
    measured = json.loads(match.group(0))
    return {k: str(measured[k]) for k in _LOUDNORM_KEYS if k in measured}


def build_loudnorm(measured: dict[str, str]) -> str:
    """Pass 2 filter string. Without measurements this degrades to a dynamic single-pass
    normalisation, which is less accurate but still better than leaving levels alone."""
    if not measured:
        return "loudnorm=I=-16:TP=-1.5:LRA=11"
    opts = ["I=-16", "TP=-1.5", "LRA=11", "linear=true"]
    mapping = {
        "input_i": "measured_I",
        "input_tp": "measured_TP",
        "input_lra": "measured_LRA",
        "input_thresh": "measured_thresh",
        "target_offset": "offset",
    }
    for src, dst in mapping.items():
        if src in measured:
            opts.append(f"{dst}={measured[src]}")
    return "loudnorm=" + ":".join(opts)


# ---- HLS packaging ----

SEGMENT_SECONDS = 6


def build_hls_argv(
    source: Path,
    rungs: list[Rung],
    has_audio: bool,
    loudnorm: str | None = None,
) -> list[str]:
    """One ffmpeg invocation produces the whole ladder, so the source is decoded once
    instead of once per rendition. ffmpeg writes the master playlist itself, which keeps
    CODECS and BANDWIDTH attributes honest.

    Output paths are deliberately flat and relative (run with `cwd=out_dir`): ffmpeg emits
    playlist URIs verbatim, so a nested `%v/` layout both writes the fMP4 init segments
    outside the variant directories and bakes the host OS path separator into the master
    playlist — a backslash there is not a valid HLS URI.
    """
    n = len(rungs)
    graph: list[str] = []

    if n == 1:
        graph.append(f"[0:v]scale=w=trunc(oh*a/2)*2:h={rungs[0].height},format=yuv420p[vo0]")
    else:
        splits = "".join(f"[v{i}]" for i in range(n))
        graph.append(f"[0:v]split={n}{splits}")
        for i, rung in enumerate(rungs):
            graph.append(
                f"[v{i}]scale=w=trunc(oh*a/2)*2:h={rung.height},format=yuv420p[vo{i}]"
            )

    if has_audio:
        chain = f"[0:a]{loudnorm}," if loudnorm else "[0:a]"
        if n == 1:
            graph.append(f"{chain}anull[ao0]" if loudnorm else "[0:a]anull[ao0]")
        else:
            outs = "".join(f"[ao{i}]" for i in range(n))
            graph.append(f"{chain}asplit={n}{outs}" if loudnorm else f"[0:a]asplit={n}{outs}")

    argv = [
        settings.ffmpeg_path, "-nostdin", "-hide_banner", "-y",
        "-i", str(source),
        "-filter_complex", ";".join(graph),
    ]

    for i, rung in enumerate(rungs):
        argv += [
            "-map", f"[vo{i}]",
            f"-c:v:{i}", "libx264",
            f"-b:v:{i}", f"{rung.video_bitrate_k}k",
            f"-maxrate:v:{i}", f"{rung.maxrate_k}k",
            f"-bufsize:v:{i}", f"{rung.bufsize_k}k",
        ]
    if has_audio:
        for i, rung in enumerate(rungs):
            argv += [
                "-map", f"[ao{i}]",
                f"-c:a:{i}", "aac",
                f"-b:a:{i}", f"{rung.audio_bitrate_k}k",
                f"-ac:{i}", "2",
            ]

    # Closed GOPs on a fixed 2s cadence: every rendition has keyframes at identical
    # timestamps, which is what makes mid-stream bitrate switching seamless.
    argv += [
        "-preset", "veryfast",
        "-profile:v", "high",
        "-level", "4.1",
        "-g", "48", "-keyint_min", "48", "-sc_threshold", "0",
        "-r", "24",
        "-f", "hls",
        "-hls_time", str(SEGMENT_SECONDS),
        "-hls_playlist_type", "vod",
        "-hls_segment_type", "fmp4",
        "-hls_flags", "independent_segments",
        "-hls_fmp4_init_filename", "%v_init.mp4",
        "-hls_segment_filename", "%v_segment_%05d.m4s",
        "-master_pl_name", "master.m3u8",
        "-var_stream_map",
        " ".join(
            (f"v:{i},a:{i},name:{r.name}" if has_audio else f"v:{i},name:{r.name}")
            for i, r in enumerate(rungs)
        ),
        "%v_playlist.m3u8",
    ]
    return argv


async def transcode_hls(
    source: Path,
    out_dir: Path,
    rungs: list[Rung],
    has_audio: bool,
    loudnorm: str | None = None,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    argv = build_hls_argv(source.resolve(), rungs, has_audio, loudnorm)
    await _run(argv, cwd=out_dir)
    master = out_dir / "master.m3u8"
    if not master.exists():
        raise MediaError("ffmpeg finished but no master playlist was written")

    # Every rung must have an init segment and at least one media segment, or the asset
    # would reach `ready` while being unplayable.
    for rung in rungs:
        init = out_dir / f"{rung.name}_init.mp4"
        playlist = out_dir / f"{rung.name}_playlist.m3u8"
        if not init.exists() or not playlist.exists():
            raise MediaError(f"rendition {rung.name} is incomplete after transcode")
        if not any(out_dir.glob(f"{rung.name}_segment_*.m4s")):
            raise MediaError(f"rendition {rung.name} produced no media segments")
    return master


# ---- Derived outputs ----

async def extract_thumbnail(
    source: Path,
    dest: Path,
    time_offset: float = 0.0,
    width: int | None = None,
    height: int | None = None,
    fit_mode: str = "preserve",
) -> Path:
    """`-ss` before `-i` seeks by index rather than decoding to the offset, which is what
    makes arbitrary-time thumbnails cheap enough to generate on request."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    filters = []
    if width or height:
        w, h = width or -2, height or -2
        if fit_mode == "crop" and width and height:
            filters.append(f"scale={w}:{h}:force_original_aspect_ratio=increase")
            filters.append(f"crop={w}:{h}")
        elif fit_mode == "pad" and width and height:
            filters.append(f"scale={w}:{h}:force_original_aspect_ratio=decrease")
            filters.append(f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black")
        else:
            filters.append(f"scale={w}:{h}")

    argv = [
        settings.ffmpeg_path, "-nostdin", "-hide_banner", "-y",
        "-ss", f"{max(time_offset, 0):.3f}",
        "-i", str(source),
        "-frames:v", "1",
    ]
    if filters:
        argv += ["-vf", ",".join(filters)]
    argv += ["-q:v", "3", str(dest)]
    await _run(argv, timeout=300)
    if not dest.exists():
        raise MediaError(f"no frame could be decoded at {time_offset}s")
    return dest


async def extract_gif(
    source: Path, dest: Path, start: float, end: float, width: int = 320, fps: int = 15
) -> Path:
    """Two-pass palette generation; a single pass with the default 216-colour web palette
    produces visibly banded output."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    duration = max(min(end - start, 10.0), 0.1)
    vf = f"fps={fps},scale={width}:-1:flags=lanczos"
    await _run(
        [
            settings.ffmpeg_path, "-nostdin", "-hide_banner", "-y",
            "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source),
            "-vf", f"{vf},split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer",
            "-loop", "0",
            str(dest),
        ],
        timeout=600,
    )
    return dest


async def render_mp4(source: Path, dest: Path, rung: Rung) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    await _run(
        [
            settings.ffmpeg_path, "-nostdin", "-hide_banner", "-y",
            "-i", str(source),
            "-vf", f"scale=w=trunc(oh*a/2)*2:h={rung.height},format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", f"{rung.video_bitrate_k}k",
            "-maxrate", f"{rung.maxrate_k}k", "-bufsize", f"{rung.bufsize_k}k",
            "-profile:v", "high", "-level", "4.1",
            "-c:a", "aac", "-b:a", f"{rung.audio_bitrate_k}k", "-ac", "2",
            # Moves the moov atom to the front so the file starts playing before it is
            # fully downloaded.
            "-movflags", "+faststart",
            str(dest),
        ]
    )
    return dest


async def cut_clip(source: Path, dest: Path, start: float, end: float) -> Path:
    """Re-encodes rather than stream-copying. Stream copy can only cut on a keyframe, so a
    copy-based clip silently slides the in-point by up to a GOP; frame accuracy is worth
    the CPU."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    await _run(
        [
            settings.ffmpeg_path, "-nostdin", "-hide_banner", "-y",
            "-ss", f"{start:.3f}",
            "-i", str(source),
            "-t", f"{max(end - start, 0.1):.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart",
            str(dest),
        ]
    )
    return dest


async def build_storyboard(
    source: Path, dest_image: Path, duration: float, columns: int = 5, rows: int = 5,
    tile_width: int = 240,
) -> tuple[Path, str]:
    """Sprite sheet plus the WEBVTT that maps playhead ranges onto its tiles."""
    dest_image.parent.mkdir(parents=True, exist_ok=True)
    tiles = columns * rows
    interval = max(duration / tiles, 0.1)
    await _run(
        [
            settings.ffmpeg_path, "-nostdin", "-hide_banner", "-y",
            "-i", str(source),
            "-vf", f"fps=1/{interval:.4f},scale={tile_width}:-2,tile={columns}x{rows}",
            "-frames:v", "1", "-q:v", "4",
            str(dest_image),
        ],
        timeout=1800,
    )
    probe_tile = await probe(dest_image)
    tile_h = probe_tile.height // rows if probe_tile.height else 135
    tile_w = probe_tile.width // columns if probe_tile.width else tile_width

    lines = ["WEBVTT", ""]
    for index in range(tiles):
        start, stop = index * interval, min((index + 1) * interval, duration)
        if start >= duration:
            break
        x, y = (index % columns) * tile_w, (index // columns) * tile_h
        lines += [
            f"{_vtt_time(start)} --> {_vtt_time(stop)}",
            f"storyboard.jpg#xywh={x},{y},{tile_w},{tile_h}",
            "",
        ]
    return dest_image, "\n".join(lines)


def _vtt_time(seconds: float) -> str:
    hours, rem = divmod(max(seconds, 0), 3600)
    minutes, secs = divmod(rem, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{secs:06.3f}"


async def extract_audio_wav(source: Path, dest: Path, max_seconds: float | None = None) -> Path:
    """16 kHz mono PCM — the shape speech-to-text models expect."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    argv = [settings.ffmpeg_path, "-nostdin", "-hide_banner", "-y", "-i", str(source)]
    if max_seconds:
        argv += ["-t", f"{max_seconds:.3f}"]
    argv += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dest)]
    await _run(argv, timeout=3600)
    return dest
