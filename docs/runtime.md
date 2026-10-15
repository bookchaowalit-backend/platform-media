# Media API runtime (implemented-local)

Correlation: `SHARED-INFRA-2026-10-15`. This is a local pilot; no deploy,
provider certification or production acceptance is claimed. The existing
`book_media_platform.graphics` CLI has not changed.

## Install and start

Python >=3.12 and PostgreSQL >=16. Install a reviewed, immutable
`platform-backend-core` wheel or Git revision, then install this distribution
with `pip install -e '.[test]'`. Local development may use `pip install -e
../platform-backend-core`; deployed artifacts must pin a released wheel/revision.

`python -m platform_media migrate` applies `MIGRATIONS` from
`platform_media.migrations`, namespace `platform_media`. It requires the explicit
`DATABASE_MIGRATION_DSN`; startup never migrates. Use a separate migrator role
that owns the schema. Grant the runtime role only schema USAGE, SELECT on
`platform_schema_migrations`, and SELECT/INSERT/UPDATE on `media_files`.
Runtime roles must be NOSUPERUSER NOBYPASSRLS; the database wrapper rejects
privileged roles. Tenant tables ENABLE and FORCE RLS even for their owner.

`python -m platform_media serve` listens on `PORT` (default 8080), exposes
`/healthz`, `/readyz`, `/metrics` and `/docs`. Required configuration:

| Name | Purpose |
|---|---|
| `DATABASE_URL` | Runtime role DSN, injected by the deployment secret store |
| `DATABASE_POOL_MAX` | Pool bound, default 8; include all API replicas in DB connection budget |
| `JWT_ISSUER`, `JWT_AUDIENCE` | Exact trusted workspace token issuer and audience |
| `JWT_PUBLIC_KEY` or `JWT_JWKS_URL` | Explicit PEM or HTTPS JWKS trust configuration |
| `MEDIA_S3_ENDPOINT`, `MEDIA_S3_BUCKET` | Server-owned HTTPS S3-compatible destination |
| `MEDIA_S3_ACCESS_KEY`, `MEDIA_S3_SECRET_KEY` | Explicit injected credentials; no credential-file or instance-profile discovery |
| `MEDIA_S3_REGION` | Default `auto` for R2 |

An incomplete object-store configuration disables uploads/downloads/deletes
with 503. Invalid complete configuration fails startup. `/readyz` requires an
adapter, matching migration checksums and FORCE RLS tables; it does not call a
remote provider. Missing tables/migrations produce 503. Bucket networking,
permissions and compatibility must be verified separately in approved staging.

Factory: `platform_media.create_app(database, verifier, object_store=None)`.
Adapters implement `ObjectStore` in `platform_media.adapters`. Omitting the
adapter loads only the explicit environment configuration above. No hub path
or other service's data model is imported.

## Versioned API

All tenant authority comes from a verified JWT `ws` UUID. Body/header workspace
selectors are unsupported. `files:write` allows create, complete, delete;
`files:read` allows list, metadata and download. Wildcard/owner shortcuts are
unsupported. Authentication errors are 401, missing scopes 403, inaccessible
files 404. APIs bound bodies at 1 MiB and requests at 30 seconds.

`POST /v1/uploads` requires `Idempotency-Key` (1..128 characters):

```json
{"filename":"image.png","content_type":"image/png","size":42,"checksum_sha256":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=","expires_in":900}
```

`checksum_sha256` is the actual 32-byte SHA256 encoded as base64, not a hex
digest. `size` is 1..100,000,000 bytes. `expires_in` is 60..3600 seconds. Allowed
types are image/png, image/jpeg, image/webp, application/pdf, text/plain and
application/octet-stream. Filenames are display metadata; slashes/control
characters are rejected. Unknown fields (including endpoint/key/workspace)
are rejected with 422. The object key is generated as `<verified-ws>/<uuid>`.

New intent returns 201 with `id`, safe metadata, `status=pending`, expiry and
`upload={url,headers}`. PUT bytes directly using **all** supplied signed headers.
Replaying an identical key/payload returns the same intent (200); a changed
canonical payload returns 409. The key remains reserved after deletion. At most 1000 unexpired pending uploads
are allowed per workspace; excess new intents return 429 while existing-key
replays continue working.
An expired pending intent returns 410; create a new intent/key to retry.
The response does not log its signed URL.

`POST /v1/uploads/{id}/complete` locks the intent, rejects expired/unknown or
other-workspace intents, then HEADs the server-owned key and compares actual
bytes, Content-Type and provider-computed ChecksumSHA256. Missing objects or
mismatches return 409, provider failure 503. Only matching bytes become `ready`.
A repeated completion of a ready intent is idempotent.

`GET /v1/files?limit=50&offset=0` lists safe metadata, ordered by creation/id.
Limit is 1..100, offset 0..10000. `GET /v1/files/{id}` returns metadata.
`GET /v1/files/{id}/download` authorizes a ready file before producing a signed
GET valid for 300 seconds; pending/deleting files return 409.
`DELETE /v1/files/{id}` first commits `deleting`, making new downloads impossible,
then deletes the object and commits a `deleted` tombstone (204). Storage failure
returns 503 and a safe repeated DELETE resumes deletion. Tenant metadata and
idempotency reservations are retained for audit; deleted files are hidden.

## S3/R2 compatibility and retention

The real boto3 adapter signs Content-Length, Content-Type, ChecksumSHA256 and
`If-None-Match: *`. Conditional creation prevents a still-valid upload URL
from replacing ready bytes. Configure bucket CORS for the exact application
origins, PUT/GET/HEAD and the signed headers. A provider must enforce checksum
validation, conditional PUT and return computed SHA256 on HEAD; absence fails
completion closed. Do not weaken verification to caller-set object metadata.
Actual R2/S3 compatibility has not been exercised here and is a staging gate.

Ready objects remain until explicit DELETE; automatic ready-file expiry is not
implemented. Pending intents stop completing after their upload expiry. Configure
a bucket lifecycle/approved sweeper for abandoned object cleanup and monitor
pending/deleting backlog. Do not apply unconditional object-age expiry to ready
objects. Already issued URLs cannot be revoked individually; PUT URLs can remain
valid after deletion and recreate unreferenced bytes until expiry, requiring
orphan cleanup. The API continues hiding the tombstone.

## Staged deployment and rollback

Build `Dockerfile` with `CORE_REQUIREMENT` set to a reviewed immutable core
artifact; the image deliberately has no unpinned core default. Run non-root,
with one bounded process per container, concurrency 64, pool max 8 by default,
request timeout 30s, provider connect/read 3s/5s and one retry. Supply private
DB network access and outbound HTTPS only to the configured object endpoint/JWKS.
Protect `/metrics` with platform ingress controls. Disable reverse-proxy access
logging of headers/bodies/query strings; keep generated route/correlation logs.

Before deployment: back up the service DB and metadata, migrate explicitly,
start one staged replica, verify `/readyz`, prove browser PUT headers/checksum,
completion, tenant isolation and deletion, then scale while respecting the DB
connection budget. Preserve object bytes during rollback. Roll back to the
previous image/config without dropping tables; new writes require compatibility
assessment. Production cutover needs independent review and owner approval.

## Verification

Run `bash scripts/check.sh` for the original complete graphics/contract suite,
then `python -m pytest` with `PLATFORM_TEST_DSN` pointing at a disposable fixture-admin DB. New tests use real PostgreSQL and an injected
local object-store fixture, plus the actual boto3 signer and SDK Stubber without
network. Do not set this DSN to production. Graphics renderer tests explicitly
skip if Edge/Arial are unavailable; report those separately.

The program-standard `PLATFORM_TEST_DSN` is an explicitly supplied disposable
**fixture admin** DSN with CREATE DATABASE/ROLE privileges. Each runtime fixture
uses `platform_backend_core.testing.temporary_database` to create a unique
database and NOSUPERUSER NOBYPASSRLS role, applies migrations as its admin, grants
the runtime schema, then connects the application only as that restricted role.
The fixture drops its database and role at teardown. No manual database/role
preprovisioning is required. A second API pool uses that same restricted DSN.
