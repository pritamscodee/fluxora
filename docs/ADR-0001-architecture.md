# ADR-0001: Core architecture

Status: accepted · Date: 2026-10-27

## Context

Fluxora is an API-first video platform: upload -> transcode -> adaptive playback, with
webhooks, analytics, and agentic automation. Transcoding is minutes-long and CPU-bound;
the API must stay responsive and the whole pipeline must survive worker death.

## Decisions

**1. Separate API process from worker process.** FastAPI handles only fast I/O. Every
ffmpeg invocation runs in a worker polling a Postgres `jobs` table.

**2. Postgres as the queue, not Redis/Celery.** `SELECT … FOR UPDATE SKIP LOCKED` gives
atomic claim semantics with no extra infrastructure, and the job row is the audit trail.
Neon is already a hard dependency. Revisit if sustained throughput exceeds ~100 jobs/s.

**3. Jobs are idempotent and keyed.** Each job carries `idempotency_key`. A handler
rebuilds a rendition unconditionally and writes to a deterministic R2 key, so a retry
after a mid-flight crash overwrites partial output rather than appending a duplicate.
Visibility timeout + `attempts` + exponential backoff; `attempts > max` -> `failed` and a
`*.errored` webhook. An asset flips to `ready` only after a final verification that every
expected R2 object exists.

**4. R2 is the only blob store; it is never public.** Buckets are private. Delivery is
proxied through the API so playback policy, signed-token audience, and analytics beacons
are enforced at one chokepoint. Segments carry long `Cache-Control` because segment URLs
are content-addressed and immutable; manifests carry short TTLs.

**5. Playback IDs are indirection on purpose.** Rotating or revoking access must not
require a new asset id, and the asset id must never appear in a public URL.

**6. Two auth systems.** Dashboard users get HS256 JWTs; machines get HTTP Basic API keys
scoped to one project. Keeping them disjoint means a leaked dashboard token cannot drive
the ingest API and an API key cannot read account billing.

**7. The agent proposes, the backend executes.** LangGraph plans; Claude never emits shell
commands. Every tool is an explicitly registered Python function with a Pydantic argument
schema, and ffmpeg is always invoked as an argv list — never through a shell. Destructive
or expensive tools (`delete_asset`, `create_clip`, `transcode`) are gated behind an
interrupt that requires human approval before the graph resumes.

## Consequences

- Delivery traffic flows through our compute, so egress cost and horizontal scale land on
  the API tier. Mitigated by a CDN in front of `/v1/stream/*` keyed on the signed URL.
- Postgres-as-queue couples worker throughput to DB connections; the worker holds one
  connection per concurrency slot.
- No just-in-time encoding: the full ladder is rendered up front, trading time-to-first-play
  for a far simpler delivery path.
