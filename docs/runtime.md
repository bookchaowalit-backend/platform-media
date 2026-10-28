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
Adapters implement `ObjectStore` in `platform_media.adapters`
(`presign_put`, `head`, `read_prefix`, `presign_get`, `delete`). Omitting the
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
bytes, Content-Type and provider-computed ChecksumSHA256. It then reads the
first 16 bytes and requires the declared type's signature (see the content-type
gate below). Missing objects or mismatches return 409 and leave the intent
`pending`; provider failure returns 503. Only matching bytes become `ready`.
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
`read_prefix` issues one ranged GetObject (`Range: bytes=0-15`); the runtime
credentials therefore need GetObject as well as HeadObject/DeleteObject and
presigning. Bytes never stream through the API beyond that 16-byte range.

## Content-type validation gate

The `content-type validation` migration gate (`contract.json`,
`repository-catalog/registries/platforms.yaml`) is enforced at completion, not
only at intent creation. A signed PUT binds the declared Content-Type header,
but not what the bytes are, so a caller could otherwise store HTML or script
bytes under `image/png` or `application/pdf`. Rules (first 16 bytes):

| Declared type | Required leading bytes |
|---|---|
| `image/png` | `89 50 4E 47 0D 0A 1A 0A` |
| `image/jpeg` | `FF D8 FF` |
| `image/webp` | `RIFF` at 0..3 and `WEBP` at 8..11 |
| `application/pdf` | `%PDF-` |
| `text/plain` | no NUL byte |
| `application/octet-stream` | unchecked (opaque by declaration) |

Synthetic accept/reject cases are in `fixtures/gates/media-content-type.json`;
`runtime_tests/test_api.py` runs every case through the real API and checks the
fixture covers exactly the allow-list. Signature checks do not prove the whole
file is well-formed and do not sanitise PDFs; downstream renderers must still
treat stored files as untrusted.

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

## Effective runtime grants

Readiness checks schema USAGE and migration-history SELECT, then checks every
required business-table privilege individually before reporting ready. Missing
grants return 503 without changing provider, migration or FORCE RLS gates.

- `media_files`: SELECT, INSERT, UPDATE.

No DELETE/TRUNCATE grant is required by this readiness gate.
Real PostgreSQL LOGIN-role regressions revoke and restore each required grant
and verify that the minimal runtime grant set remains ready.

## Verification 2026-10-29 (branch `media-runtime`)

PR #1 (`codex/shared-infrastructure-20261015`) rebased onto `main` `d493a77`
(shared CI v1.1.1: Ruff, `scripts/check.sh`, security scans incl. gitleaks;
re-pinned schema). The one conflict, `.github/workflows/check.yml`, keeps
`main`'s reusable-workflow version; the renderer-skip comment from the PR lives
in `docs/ci-renderer-gates.md` and `shared-runtime.yml`.

Changes beyond the rebase:

- Ruff 0.17.1 (the CI pin) clean: `[tool.ruff.lint.flake8-bugbear]
  extend-immutable-calls` for FastAPI markers and `require_scope`, sorted
  imports, the checksum validator catches `ValueError` only, remaining blind
  excepts carry a `noqa: BLE001` reason (errors are redacted to 503/exit 1).
- Content-type gate above: `ObjectStore.read_prefix`, S3 ranged GetObject,
  fixture `fixtures/gates/media-content-type.json` (13 synthetic cases).

Environment: Python 3.12, PostgreSQL 16 (local `pgserver`; CI uses the
`postgres:16` service), `platform-backend-core` at `d00e0119`, no network
calls, no credentials. `PLATFORM_TEST_DSN` pointed at the disposable local
server.

RED (tests written first, before `read_prefix` or the signature table existed):
`python -m pytest -q runtime_tests` -> 4 failed, 15 passed. The API case
`png-declared-html-bytes` completed with 200 (`AssertionError:
png-declared-html-bytes`), the fail-closed test got 200 instead of 503, the
fixture test hit `ImportError: ALLOWED_CONTENT_TYPES`, and the S3 test hit
`AttributeError: read_prefix`.

GREEN after the change:

- `python -m pytest -q runtime_tests`: 19 passed.
- `BOOK_MEDIA_SKIP_RENDERER_TESTS=1 python -m pytest -q -rs`: 94 passed,
  6 skipped (5 Edge renderer cases by flag, 1 Windows-only), 2 subtests.
- `BOOK_MEDIA_SKIP_RENDERER_TESTS=1 bash scripts/check.sh`: 81 tests OK
  (6 skipped), schema pin OK.
- `ruff check .` (0.17.1): clean. `gitleaks git` 8.30.1 over full history: no
  leaks.

Not done here (deliberately): retention classes or automatic expiry (the
`retention policy` gate needs an owner decision; ready objects stay until
DELETE as documented above), `access-control parity` against a legacy
implementation (none exists to compare with), real R2/S3 compatibility
(staging gate), deploy.

## Follow-ups 2026-10-29 (branch `media-followups`)

Two low-severity review findings on `main` at `77029d5`.

**F1a: content read bound to the verified object version.** Completion did
HEAD, then a separate ranged GetObject, with nothing tying the two together.
On a store that does not enforce `If-None-Match: *`, the object could be
replaced between the calls, so the metadata check and the signature check
could see different objects. Now `head` returns the ETag, and `read_prefix(key,
16, if_match=etag)` sends GetObject `IfMatch`. A different version gives 412
`PreconditionFailed`, which the adapter maps to `None`, so completion returns
409 and the intent stays `pending`. A HEAD with no ETag also fails closed (409).
No new permission is needed (still GetObject).

**F1b: late PUT after delete (orphan bytes).** A presigned PUT cannot be
revoked. It stays valid until the intent's `expires_at` (at most 3600 s after
the intent), so after DELETE it can recreate the object. This is now bounded
by that window and uses the existing tombstone row, with no migration:

- `DELETE` on an already `deleted` row deletes the object key again while
  `now() < expires_at` (503 on provider error, safe to retry). After the
  window it returns 204 without calling the store.
- `complete` on a `deleted` intent inside the window deletes the object key
  as best effort (a failure is logged and the next DELETE replay retries),
  then returns 404. It never resurrects the row.
- Residual: a late PUT that arrives after the final DELETE/complete call and
  before `expires_at` stays unreferenced. A bucket lifecycle rule or approved
  sweeper must still remove objects under `<workspace>/<id>` whose row is
  `deleted` (see "Content-type validation gate" above). Overwriting bytes
  after completion still depends on the provider enforcing `If-None-Match: *`.
  That is a staging compatibility gate.

**F2: no transaction across store calls.** `complete` held `SELECT ... FOR
UPDATE` across HEAD and GetObject. A slow store could pass the 10 s
`idle_in_transaction_session_timeout` and return 500 instead of 503, and it
blocked DELETE in the meantime. It now runs in three phases: (1) read the
intent and commit, (2) HEAD plus the If-Match range read with no transaction
open, (3) re-lock and set `ready` only if the row is still `pending` with the
same object key, size, type, checksum and expiry. If a concurrent DELETE won,
the result is 404 (plus a purge inside the window). If the row is `deleting`,
the result is 409. If another completion already finished, its row is
returned. If the intent expired, the result is 410.

RED (tests written first against `77029d5` source): `python -m pytest -q
runtime_tests` -> 7 failed, 18 passed:

- the S3 head test was missing the `etag` key, and the S3 If-Match test hit
  `TypeError: read_prefix() got an unexpected keyword argument 'if_match'`;
- the swapped-object test and the no-ETag test both got 200 instead of 409;
- the no-lock probe recorded `['LockNotAvailable']` (`FOR UPDATE NOWAIT` from
  a second connection while `complete` was inside HEAD);
- the concurrent-delete test recorded `['LockNotAvailable']` under a 2 s
  `lock_timeout`;
- the late-PUT test found the recreated object still in the store after
  complete/DELETE replay.

GREEN after the change (Python 3.12, local `pgserver` PostgreSQL 16, core
`d00e0119`, Stubber only, no network or credentials):

- `python -m pytest -q runtime_tests`: 25 passed.
- `BOOK_MEDIA_SKIP_RENDERER_TESTS=1 python -m pytest -q -rs`: 100 passed,
  6 skipped (renderer flag / Windows-only), 2 subtests.
- `BOOK_MEDIA_SKIP_RENDERER_TESTS=1 bash scripts/check.sh`: 81 tests OK
  (6 skipped).
- `ruff check .` (0.17.1 and 0.17.3): clean. Test fixtures use only synthetic
  keys (`synthetic-access`, `"synthetic-etag-1"`). gitleaks runs in the CI
  `security` job; it is not installed locally.
