# Fluxora Feature Matrix

Derived from publicly documented Mux behavior (mux.com/features, /docs, /docs/api-reference)
on 2026-10-27. Fluxora is an original implementation with equivalent core functionality.
No proprietary code, branding, or internal implementation detail is reproduced.

Legend: `MVP` = phase 1-4 scope, `L8R` = phase 5-6 scope, `OUT` = deliberately out of scope.

## Ingest

| Capability | Observed (public docs) | Fluxora | Scope |
|---|---|---|---|
| Direct upload via authenticated URL | `POST /video/uploads` returns a one-time PUT URL | R2 presigned PUT, single-use, TTL-bounded | MVP |
| Pull ingest from a URL | asset `input[].url` | background fetch job -> R2 | MVP |
| Upload cancel | `PUT /video/uploads/{id}/cancel` | status -> `cancelled`, presign revoked | MVP |
| Upload timeout | `timeout` seconds, status -> `timed_out` | reaper job | MVP |
| Multi-codec source support | "hundreds of codecs" | whatever ffmpeg/libav decodes | MVP |
| Test assets | non-billed, watermarked, duration-capped | `test: true` -> 10s cap | MVP |
| RTMP / SRT live ingest | persistent stream key | RTMP only (nginx-rtmp or ffmpeg listener) | L8R |
| DRM | — | not attempted | OUT |

## Processing

| Capability | Observed | Fluxora | Scope |
|---|---|---|---|
| Metadata extraction | duration, aspect ratio, max resolution, tracks | `ffprobe` -> `assets` + `asset_tracks` | MVP |
| Adaptive bitrate ladder | "smart" ladder chosen per source | deterministic ladder capped at source height | MVP |
| HLS packaging | `.m3u8` master + variants | ffmpeg `hls` muxer, fMP4 segments | MVP |
| Thumbnails | `/{playback_id}/thumbnail.jpg?time=` | ffmpeg frame extract, on-demand + cached | MVP |
| Storyboard / sprite | `/storyboard.vtt` + `.jpg` | tile montage + WEBVTT | L8R |
| Animated GIF | `/animated.gif` | ffmpeg palettegen/paletteuse | L8R |
| Audio normalization | "consistent and pleasant" loudness | EBU R128 two-pass `loudnorm` | MVP |
| Static MP4 renditions | `mp4_support: standard` | 270p/480p/720p/1080p progressive MP4 | MVP |
| Master access | highest-quality download | temporary signed URL to source object | MVP |
| Clipping | Program Date Time / start+end offsets | `-ss/-to` copy-or-reencode into a new asset | MVP |
| Subtitles / captions | uploaded text tracks + auto-generated | uploaded VTT/SRT; auto via AI phase | MVP / L8R |
| Watermarking | image overlay | ffmpeg `overlay` filter | L8R |
| Just-in-time encoding | playable seconds after upload | not attempted; ladder is pre-rendered | OUT |

## Delivery

| Capability | Observed | Fluxora | Scope |
|---|---|---|---|
| Playback IDs | opaque id decoupled from asset id | `playback_ids` row, `public` or `signed` | MVP |
| Public playback | `https://stream.../{id}.m3u8` | `/v1/stream/{playback_id}.m3u8` | MVP |
| Signed playback | JWT, `aud` per resource kind | RS256 JWT, `aud` in `v`/`t`/`g`/`s`, `kid` -> signing key | MVP |
| Playback restrictions | referrer / user-agent allowlists | `playback_restrictions` checked at manifest time | L8R |
| Multi-CDN | redundant delivery | single origin + cache headers | OUT |
| LL-HLS low latency | ~5s | standard-latency HLS only | OUT |
| Simulcast to third parties | Facebook/YouTube/Twitch targets | not attempted | OUT |

## Platform

| Capability | Observed | Fluxora | Scope |
|---|---|---|---|
| Environments per project | dev/prod key separation | `projects.environment` | MVP |
| API keys | id + secret, Basic auth | `fx_<key_id>` + secret, Argon2-hashed | MVP |
| Webhooks | signed POST, retried | HMAC-SHA256 `Fluxora-Signature`, exp. backoff | MVP |
| Event log | per-event id, replayable | `events` table + redeliver endpoint | MVP |
| Usage / metering | per-asset minutes, delivery | `usage_records` daily rollup | L8R |
| Engagement analytics | views, unique viewers, playing time | `video_views` beacon + rollups | L8R |
| AI chapters / summaries / translations | "Mux Robots" | Claude + LangGraph agent tools | L8R |

## Webhook event names

Fluxora mirrors Mux's dotted naming so integrations read naturally. Implemented in MVP:

```
video.upload.created          video.upload.asset_created    video.upload.cancelled
video.upload.errored          video.asset.created           video.asset.ready
video.asset.errored           video.asset.updated           video.asset.deleted
video.asset.track.created     video.asset.track.ready       video.asset.track.errored
video.asset.static_rendition.ready
```

Phase 5 adds the live-stream family (`video.live_stream.{created,connected,active,
disconnected,idle,recording,disabled,deleted}`).
