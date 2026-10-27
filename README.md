# Fluxora

An API-first video platform: direct uploads, FFmpeg transcoding to adaptive-bitrate HLS,
signed playback, webhooks, usage metering, and an agent that operates the whole thing
through guard-railed tool calls.

Built as an original implementation of the capabilities described in Mux's public
documentation. See [docs/FEATURE_MATRIX.md](docs/FEATURE_MATRIX.md) for what was modelled
and what was deliberately left out, and [docs/API_CONTRACTS.md](docs/API_CONTRACTS.md) for
the API surface.

---

## What works today

Upload a file and get back an adaptive HLS stream, verified end to end against a live
stack (Neon Postgres, S3 storage in Docker, a separate worker process):

```
 1. health ok, database reachable
 5. uploaded 4,065,109 bytes directly to object storage
 8. asset ready: 5.00s 16:9 HD, 2 tracks
 9. master manifest serves 4 renditions
12. thumbnail rendered (19,003 bytes)
13. static mp4 renditions: ['high', 'low', 'medium']
15. asset deleted and playback stopped immediately
```

Reproduce it with `python -m scripts.smoke`.

---

## Architecture

```
            ┌────────────┐     presigned PUT      ┌──────────────┐
  client ──▶│  FastAPI   │───────────────────────▶│  S3 storage  │
            │    API     │                        │ (MinIO/R2)   │
            └─────┬──────┘                        └──────▲───────┘
                  │ jobs table                            │
            ┌─────▼──────┐    ffmpeg: probe, ABR ladder,  │
            │   worker   │────HLS, MP4, thumbs, clips ────┘
            └─────┬──────┘
                  │ events ──▶ signed webhooks (HMAC, retried)
            ┌─────▼──────┐
            │    Neon    │  assets, jobs, events, usage, agent runs
            └────────────┘

  agent:  LangGraph ──▶ guardrails ──▶ approval ──▶ BullMQ/Redis ──▶ tool worker
```

Four processes: the API, the media worker, the agent tool worker, and the docs site.
Design decisions and their trade-offs are recorded in
[ADR-0001](docs/ADR-0001-architecture.md) (core architecture) and
[ADR-0002](docs/ADR-0002-object-storage.md) (object storage).

---

## Running it

### Prerequisites

FFmpeg on `PATH`, Python 3.12+, Node 20+, Docker.

### 1. Infrastructure

```bash
docker compose up -d
```

Starts an S3-compatible store on `localhost:9000` and Redis on `localhost:6379`.
SeaweedFS is the default because MinIO's images are no longer publicly pullable; to use
MinIO once you have registry access, `docker compose --profile minio up -d` instead — the
application configuration is identical either way.

### 2. Configuration

```bash
cp .env.example .env
```

The defaults work against the compose stack. Fill in `DATABASE_URL` for Neon (paste the
connection string as Neon gives it to you — the libpq-style `?sslmode=require` parameters
are normalised for asyncpg automatically) and `OPENAI_API_KEY` to enable the agent.

### 3. Database

```bash
cd backend
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt
.venv/Scripts/python -m alembic upgrade head
```

### 4. Processes

```bash
.venv/Scripts/python -m uvicorn app.main:app --reload
```

```bash
.venv/Scripts/python -m app.workers.runner
```

```bash
.venv/Scripts/python -m app.workers.agent_worker
```

```bash
cd frontend && npm install && npm run dev
```

API reference at http://localhost:8000/docs; the project documentation site at
http://localhost:4321.

---

## Trying the API

```bash
curl -X POST localhost:8000/v1/auth/register \
  -H 'content-type: application/json' \
  -d '{"email":"you@example.com","password":"a-long-enough-password"}'
```

Create a project and an API key with the returned access token, then:

```bash
curl -u "$KEY_ID:$SECRET" -X POST localhost:8000/v1/video/uploads \
  -H 'content-type: application/json' \
  -d '{"new_asset_settings":{"playback_policy":["public"],"mp4_support":"standard"}}'
```

`PUT` your file to the returned `url`, then poll `GET /v1/video/uploads/{id}` until it
reports `asset_created`. Once the asset is `ready`, it plays at
`/v1/stream/{playback_id}.m3u8`.

Dashboard routes use a bearer token; video API routes use HTTP Basic with the API key.
The two are deliberately not interchangeable.

---

## The agent

The agent turns a request like *"clip the first 10 seconds of my newest video"* into a
plan of tool calls, and runs it.

```
guard_input ──▶ plan ──▶ policy ──▶ [await approval] ──▶ execute ──▶ respond
```

**Guardrails.** Two independent layers. Guardrails AI screens natural language —
instruction-override attempts and out-of-scope requests on the way in, credential leakage
on the way out. Separately, deterministic policy checks screen *structure*: every step
must name a registered tool and pass that tool's Pydantic schema, plans are capped, and
any `expensive` or `destructive` tool forces the whole run through human approval. The
second layer is plain Python on purpose — a guardrail you can uninstall is not a
guardrail.

**Approval.** `create_clip`, `enable_mp4_downloads`, `generate_captions` and
`delete_asset` never run unattended. The run parks at `awaiting_approval` and nothing has
happened yet; approving re-screens the stored plan before executing it, so approval
authorises one specific plan rather than a blank cheque.

**Task bus.** Every tool call is dispatched as a BullMQ job on Redis — five attempts with
exponential backoff, then the dead-letter queue, inspectable and replayable at
`GET /v1/projects/{id}/agent/dead-letters`. Deterministic failures (unknown tool, bad
arguments, missing project) are dead-lettered on the first attempt rather than retried
four more times to the same conclusion.

**Tracing.** Set `LANGSMITH_API_KEY` and `LANGSMITH_TRACING=true`.

The model never emits code or shell commands, and FFmpeg is always invoked as an argv
list, never through a shell.

Optional Guardrails AI Hub validators (`detect_pii`, `toxic_language`,
`detect_jailbreak`) can be added by name via `GUARDRAILS_INPUT_VALIDATOR_NAMES` once
installed with `guardrails hub install`. They are optional; the local validators work
with no network access.

---

## Tests

```bash
cd backend
.venv/Scripts/python -m pytest -q                        # 57 tests
FLUXORA_TEST_STORAGE=s3 .venv/Scripts/python -m pytest -q # same suite, real S3
```

The suite runs against SQLite plus local-disk storage by default, and unchanged against
the Docker S3 service, which is how the presigned-upload path gets covered over a real
socket. Agent tests fake the LLM but use a real BullMQ worker against Redis, so retries
and dead-lettering are genuinely exercised.

`python -m scripts.smoke` goes further: a live API, a separate worker process, Neon, and
real object storage.

---

## Repository layout

```
backend/
  app/
    api/v1/      routes: auth, projects, uploads, assets, delivery, webhooks, agent
    agents/      LangGraph graph, tools, guardrails, BullMQ bus, LLM factory
    core/        config, db, security, errors, telemetry
    models/      SQLAlchemy models
    schemas/     Pydantic request and response models
    services/    assets, uploads, storage, media (ffmpeg), jobs, events
    workers/     media worker, agent tool worker, job handlers
  migrations/    Alembic
  tests/
  scripts/       smoke test
frontend/        Astro documentation site
docs/            feature matrix, API contracts, ADRs
```
