# ADR-0002: Object storage via an S3-compatible API

Status: accepted · Date: 2026-10-27 · Supersedes the R2-specific part of ADR-0001

## Context

ADR-0001 named Cloudflare R2 as the blob store. Development needs a local equivalent, and
the obvious choice was MinIO in Docker. Two things then became clear:

1. **MinIO's images are no longer publicly pullable.** `minio/minio` on Docker Hub and
   `quay.io/minio/minio` both refuse anonymous pulls (verified 2026-10-27: Docker Hub
   returns "pull access denied … repository does not exist", quay.io returns 401 on the
   manifest HEAD and on its tags API). A default `docker compose up` therefore cannot
   bring up MinIO on a clean machine.

2. **The store does not need to be MinIO.** R2, AWS S3, MinIO and SeaweedFS all speak the
   same S3 API. What the pipeline actually depends on is narrow: SigV4 signing, presigned
   PUT and GET, `head_object`, prefix listing, and bulk delete.

## Decision

**One `S3Storage` backend for every environment**, selected by `S3_ENDPOINT` plus
credentials. Path-style addressing and SigV4 are the defaults, because MinIO and SeaweedFS
require them and R2 accepts them. There is no R2-specific, MinIO-specific, or
vendor-specific code path.

**SeaweedFS is the default development container; MinIO is a compose profile.** Both
publish the S3 API on `localhost:9000` with the same credentials, so application
configuration is byte-identical and the app cannot tell which is running:

```
docker compose up -d                   # SeaweedFS, works on a clean machine
docker compose --profile minio up -d   # MinIO, once you have registry access
```

**Buckets are created on first use.** `S3Storage.ensure_bucket()` runs at construction,
tolerating both "already exists" and a lost creation race, because a fresh container starts
with no buckets and the first upload would otherwise fail with `NoSuchBucket`.

**Presigned PUTs do not sign `Content-Type`.** Including it in the signature forces the
client to reproduce the header byte-for-byte, which browsers do not do reliably; the
mismatch shows up as an opaque 403 at upload time.

**`S3_PUBLIC_ENDPOINT` exists for split-horizon deployments.** A presigned URL's signature
covers the host, so one signed against an in-cluster hostname breaks the moment a browser
uses it. When set, a second client signs client-facing URLs against the public origin.

## Consequences

- Swapping to R2 or AWS S3 in production is a configuration change, not a code change.
- The dev store is not byte-for-byte the same implementation as production. Mitigated by
  the test suite running unchanged against either (`FLUXORA_TEST_STORAGE=s3`), which
  covers the presigned-upload path over a real socket rather than through a filesystem
  shim.
- SeaweedFS's S3 gateway is a smaller surface than MinIO's; if a test ever needs a MinIO
  behaviour specifically, the `minio` profile is already defined.
- A local filesystem backend remains as a third fallback when no credentials are present
  at all, so the full upload-to-playback path runs with no infrastructure whatsoever.

## Verified behaviour

Against the compose service, the backend was confirmed to: auto-create the bucket; store
and read back exact bytes; overwrite in place on a repeated write (which is what makes job
retries converge rather than duplicate); report `None` for a missing key; complete a
presigned PUT and GET round trip; refuse an unsigned GET with 403, keeping the bucket
private as ADR-0001 decision 4 requires; and remove a whole prefix recursively.
