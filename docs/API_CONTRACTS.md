# Fluxora API Contracts

Base URL: `https://api.fluxora.dev` (local: `http://localhost:8000`)
All resource routes are under `/v1`. Request and response bodies are JSON.

## Conventions

Mux-equivalent envelope: every success response wraps the payload in `data`.

```json
{ "data": { "id": "…" } }
{ "data": [ { "id": "…" } ] }
```

Errors use a stable shape; `type` is machine-readable, `messages` is human-readable.

```json
{ "error": { "type": "invalid_parameters", "messages": ["input.url is required"] } }
```

| HTTP | `error.type` |
|---|---|
| 400 | `invalid_parameters` |
| 401 | `unauthorized` |
| 403 | `forbidden` |
| 404 | `not_found` |
| 409 | `conflict` |
| 422 | `invalid_parameters` |
| 429 | `rate_limited` |
| 500 | `internal_error` |

List endpoints take `limit` (1..100, default 25) and `page` (1-based) and return
`X-Total-Count`. Timestamps are RFC 3339 UTC strings. Durations are float seconds.

## Authentication

Two separate credential systems, deliberately not interchangeable.

**Dashboard (human) — JWT bearer.** `POST /v1/auth/register`, `POST /v1/auth/login`
return `{access_token, refresh_token}`. HS256, 30-minute access, 30-day rotating refresh.
Used only by the Next.js dashboard.

**API (machine) — HTTP Basic.** `Authorization: Basic base64(key_id:secret)`, matching
Mux's scheme so existing client habits transfer. The secret is shown once at creation and
stored Argon2id-hashed. Every API-key request resolves to exactly one project, so no
request needs to name a project.

## Resources

### Projects — dashboard auth
```
POST   /v1/projects                 {name, environment}
GET    /v1/projects
GET    /v1/projects/{id}
PATCH  /v1/projects/{id}            {name}
DELETE /v1/projects/{id}
POST   /v1/projects/{id}/api-keys   {name} -> {key_id, secret (once), …}
GET    /v1/projects/{id}/api-keys
DELETE /v1/projects/{id}/api-keys/{key_id}
```

### Direct uploads — API auth
```
POST   /v1/video/uploads            {new_asset_settings, cors_origin, timeout, test}
GET    /v1/video/uploads
GET    /v1/video/uploads/{id}
PUT    /v1/video/uploads/{id}/cancel
```
`POST` returns `{id, url, timeout, status: "waiting", …}`. `url` is a presigned R2 PUT,
single-use, expiring at `timeout` (default 3600s, max 604800). The client PUTs raw bytes
to it, then notifies `PUT /v1/video/uploads/{id}/complete` (or the reconcile job notices
the object). Status machine:

```
waiting ──object landed──> asset_created ──> (asset lifecycle)
   ├──cancel──> cancelled
   ├──timeout──> timed_out
   └──bad input──> errored
```

### Assets — API auth
```
POST   /v1/video/assets             {input[], playback_policy[], mp4_support, …}
GET    /v1/video/assets
GET    /v1/video/assets/{id}
PATCH  /v1/video/assets/{id}        {passthrough, mp4_support, master_access}
DELETE /v1/video/assets/{id}
POST   /v1/video/assets/{id}/playback-ids      {policy}
DELETE /v1/video/assets/{id}/playback-ids/{pid}
POST   /v1/video/assets/{id}/tracks            {url, type, language_code, …}
DELETE /v1/video/assets/{id}/tracks/{tid}
POST   /v1/video/assets/{id}/clip              {start_time, end_time, …} -> new asset
GET    /v1/video/assets/{id}/input-info
```
Asset status: `preparing -> ready | errored`; `deleted` is a tombstone. An asset reaches
`ready` only when the HLS master manifest and every ladder rung are durable in R2.

### Playback IDs
```
GET    /v1/video/playback-ids/{id}   -> {object: {type: "asset", id}}
```

### Delivery — unauthenticated, or signed JWT
```
GET /v1/stream/{playback_id}.m3u8                 master manifest
GET /v1/stream/{playback_id}/{rendition}.m3u8     variant manifest
GET /v1/stream/{playback_id}/{rendition}/{seg}    media segment
GET /v1/stream/{playback_id}/thumbnail.{jpg|png}  ?time=&width=&height=&fit_mode=
GET /v1/stream/{playback_id}/animated.gif         ?start=&end=&width=&fps=
GET /v1/stream/{playback_id}/storyboard.{vtt|jpg}
GET /v1/stream/{playback_id}/{rendition}.mp4      static rendition
```
`signed` playback requires `?token=<JWT>`: RS256, `kid` selects the signing key,
`sub` = playback id, `aud` = `v` (video), `t` (thumbnail), `g` (gif), `s` (storyboard),
`exp` enforced. Audience mismatch is 403, not 404, so misconfiguration is debuggable.

### Live streams — API auth (phase 5)
```
POST   /v1/video/live-streams        {playback_policy[], new_asset_settings, latency_mode,
                                      reconnect_window, reduced_latency}
GET    /v1/video/live-streams
GET    /v1/video/live-streams/{id}
PATCH  /v1/video/live-streams/{id}
DELETE /v1/video/live-streams/{id}
POST   /v1/video/live-streams/{id}/reset-stream-key
PUT    /v1/video/live-streams/{id}/{enable|disable}
POST   /v1/video/live-streams/{id}/complete
```
Ingest: `rtmp://ingest.fluxora.dev/live` + stream key.
Status: `idle -> active -> idle`, or `disabled`.

### Webhooks — dashboard auth
```
POST   /v1/webhooks                 {url, events[]}
GET    /v1/webhooks
PATCH  /v1/webhooks/{id}            {url, events[], active}
DELETE /v1/webhooks/{id}
GET    /v1/webhooks/{id}/deliveries
POST   /v1/events/{event_id}/redeliver
```
Delivery envelope:
```json
{
  "type": "video.asset.ready",
  "id": "evt_…",
  "created_at": "2026-10-27T12:00:00Z",
  "object": { "type": "asset", "id": "…" },
  "environment": { "name": "Production", "id": "…" },
  "data": { }
}
```
Signature header `Fluxora-Signature: t=<unix>,v1=<hex>` where
`v1 = HMAC_SHA256(secret, "<t>.<raw_body>")`. Receivers must compare in constant time and
reject `|now - t| > 300s` to stop replay. Retries at 1m, 5m, 30m, 2h, 6h, 24h; a `2xx`
or 6 failures ends the attempt chain.

### Data / analytics (phase 5)
```
POST /v1/data/beacon                 player telemetry ingest
GET  /v1/data/video-views            ?asset_id=&timeframe[]=
GET  /v1/data/metrics/{metric}       views|unique_viewers|playing_time|startup_time
GET  /v1/data/usage                  ?timeframe[]=  encoding/storage/delivery minutes
```

### Agent (phase 6)
```
POST /v1/agent/runs                  {prompt, asset_id?} -> {run_id, plan, requires_approval}
GET  /v1/agent/runs/{id}
POST /v1/agent/runs/{id}/approve     {approved: bool}
GET  /v1/agent/runs/{id}/events      SSE trace stream
```

## Idempotency

Mutating API-key endpoints accept `Idempotency-Key`. The key, request fingerprint, and
response are stored for 24h; a replay with the same key returns the stored response, and
the same key with a different body is `409 conflict`. Internal processing jobs carry their
own `idempotency_key` so a retried worker converges instead of duplicating renditions.
