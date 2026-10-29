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

`python -m platform_media sweep` is a third, explicit operator command for one
`MEDIA_WORKSPACE_ID`; see "Orphan-object sweeper".

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
| `SENTRY_DSN` | Optional error reporting (unset = off); scrubbing in `src/platform_media/_sentry.py` |

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

### Container image

`docker build .` works from a clean checkout with no build arguments and no
pre-staged `wheelhouse/`. The `Dockerfile` is two stages:

1. `build` (`python:3.12-slim` plus `git`): `pip wheel` builds this distribution
   (`platform_media` and `book_media_platform`) and `platform-backend-core` from the
   same reviewed commit CI installs (`21d5f44d26220080cbed43b9807979dc84a8533a`),
   with all transitive dependencies (`boto3`, `uvicorn`, ...).
2. `runtime` (`python:3.12-slim`, no git, no build tools): installs only those
   wheels with `--no-index`, runs as UID/GID `65532`, `PORT=8080`, and keeps
   `CMD ["python", "-m", "platform_media", "serve"]`. Run `migrate` as a separate
   one-off with `DATABASE_MIGRATION_DSN` (same image, command override).

The image serves the media API only. The optional `book_media_platform.graphics`
renderer needs Microsoft Edge and Arial (`find_edge()`, Windows process cleanup via
PowerShell); neither is installed, so rendering does not work in this image. The
graphics package still imports and its CLI `--help` runs. Renderer acceptance stays
with the manual Windows `graphics-renderer.yml` workflow (`docs/ci-renderer-gates.md`).

`.dockerignore` keeps `.git`, `.venv`, tests, build output, `.runtime`, `data`,
`logs` and `.env*` out of the build context. CI job `image` in `shared-runtime.yml`
runs `docker build` on every push and pull request, then checks that the image
imports `boto3`, the core, `platform_media.app`/`adapters` and
`book_media_platform.graphics`, that `python -m platform_media --help` runs, and
that it runs as a non-root user. It never pushes or deploys.
`tests/test_build_hygiene.py` (part of `scripts/check.sh`) fails if the Dockerfile
copies a git-ignored path, stops being multi-stage, pins a core commit other than
the workflow's, runs the final stage as root or changes the CMD, if CI stops
building the image, or if any workflow `uses:` is not pinned to a 40-hex commit SHA
(tags go in a trailing comment). Upgrading the core means changing the commit in
the workflow, the Dockerfile and `[tool.uv.sources]` together; the tests fail on
any mismatch.

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
implemented. Pending intents stop completing after their upload expiry. Run
`python -m platform_media sweep` (see "Orphan-object sweeper") for abandoned
object cleanup, add the bucket lifecycle rule described there, and monitor
pending/deleting backlog. Do not apply unconditional object-age expiry to ready
objects. Already issued URLs cannot be revoked individually; PUT URLs can remain
valid after deletion and recreate unreferenced bytes until expiry, requiring
orphan cleanup. The API continues hiding the tombstone.

## Orphan-object sweeper

`python -m platform_media sweep` is an explicit operator command, like `migrate`.
Startup and `serve` never sweep. One run handles one bounded batch for exactly one
workspace and exits:

| Name | Purpose |
|---|---|
| `DATABASE_URL` | Runtime role DSN (the API role; no extra grant) |
| `MEDIA_WORKSPACE_ID` | Required UUID; the only workspace the run reads (RLS `app.workspace_id`) |
| `MEDIA_S3_ENDPOINT`, `MEDIA_S3_BUCKET`, `MEDIA_S3_ACCESS_KEY`, `MEDIA_S3_SECRET_KEY`, `MEDIA_S3_REGION` | Same object-store configuration as `serve`; all four required |
| `MEDIA_SWEEP_BATCH` | Rows per run, default 100, 1..1000 |

There is no cross-tenant scan and no caller-selected workspace: an external
trusted scheduler runs one invocation per workspace it is configured for (the same
model as `COMMUNICATION_WORKSPACE_ID` in `platform-communication`).

A row is a candidate when `swept_at IS NULL`, its status is `deleted`, `deleting`
or `pending` (an intent that never completed), and `expires_at` is at least
`SWEEP_GRACE_SECONDS` (900 s) in the past. The grace covers clock skew between the
API host that signed the PUT and the provider that checks its expiry. After that
point no presigned PUT for the key is valid, so the deletion is final. `ready`
rows are never candidates, whatever their (intent) `expires_at`.

For each candidate, oldest expiry first, the sweeper:

1. deletes the object at the row's key with no transaction open. A missing object
   counts as deleted; the S3 adapter also treats a `404`/`NoSuchKey` reply as
   success;
2. sets `swept_at=now()` only if the row still has the same status, key and expiry.
   A `deleting` row (a DELETE whose store call failed) also becomes `deleted` with
   `deleted_at`, finishing that deletion. `pending` rows keep their status, and
   `complete` still answers 410.

A store error leaves the row unmarked for the next run. A row changed by a
concurrent DELETE during step 1 is counted `skipped` and re-checked next run.
Concurrent sweeps of the same workspace are safe: deletes are idempotent and the
mark is conditional. Logs and output carry counts only, never keys, workspace ids,
the DSN or provider messages. Output is one line, `swept=N failed=N skipped=N
more=yes|no`. `more=yes` means the batch was full, so run again.

| Exit | Meaning |
|---|---|
| 0 | Batch done with no store failures |
| 1 | Database unavailable or unexpected failure (no detail printed) |
| 2 | Usage/configuration error: missing/invalid `DATABASE_URL`, `MEDIA_WORKSPACE_ID`, `MEDIA_SWEEP_BATCH` or `MEDIA_S3_*` |
| 3 | Batch done, but some object deletions failed; those rows are retried next run |

Migration `002-media-sweep` adds the nullable `media_files.swept_at` and a partial
index on `(workspace_id, expires_at, id) WHERE swept_at IS NULL AND status <>
'ready'`. Readiness keeps comparing the full migration history, so a runtime
without `002` reports 503. The sweeper uses only SELECT and UPDATE on
`media_files` (`sweeper.SWEEP_PERMISSIONS`, a subset of the readiness grant set),
so no DELETE/TRUNCATE grant is added. The store credentials need DeleteObject,
which the API already uses.

Library use: `platform_media.sweeper.sweep(database, store, workspace,
batch_size=100) -> SweepResult(swept, failed, skipped, more)`.

### Bucket lifecycle rule (deploy-time step, not applied here)

The bucket owner configures this once per bucket during deployment, through the
provider console or its lifecycle API. Nothing in this repository calls a cloud API.

- **Abort incomplete multipart uploads after 1 day, bucket-wide.** The API only
  issues single presigned PUTs, so any multipart upload is a stray. Removing its
  parts never touches a stored object.
- **Do not expire objects under the API's `<workspace>/` keys.** Ready files share
  that layout, and the docs above forbid unconditional age expiry of ready
  objects. API orphans are removed by `sweep`, which can tell them apart.
- **Expire only a dedicated non-API prefix**, if one is used (for example
  `scratch/` for manual operations or staging smoke objects), say after 7 days.

S3 `PutBucketLifecycleConfiguration` body (R2 accepts the same rule shape through
its S3 API, or set the equivalent in the dashboard):

```json
{
  "Rules": [
    {"ID": "abort-incomplete-multipart", "Status": "Enabled",
     "Filter": {"Prefix": ""},
     "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}},
    {"ID": "expire-scratch", "Status": "Enabled",
     "Filter": {"Prefix": "scratch/"},
     "Expiration": {"Days": 7}}
  ]
}
```

Schedule `sweep` per workspace at an interval at least as long as the grace period
(for example hourly). Alert on exit 1 or 3, and on `more=yes` persisting across runs.

## Staged deployment and rollback

Build the image with `docker build .` (see "Container image"; the core commit is
pinned in the `Dockerfile`, there is no unpinned core default). Run non-root,
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

- `media_files`: SELECT, INSERT, UPDATE. The `sweep` command uses SELECT and
  UPDATE only (`sweeper.SWEEP_PERMISSIONS`); no grant was added for it.

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
`postgres:16` service), `platform-backend-core` at `21d5f44d`, no network
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
  before `expires_at` stays unreferenced until `python -m platform_media sweep`
  removes it after the link expires (see "Orphan-object sweeper", added later
  the same day). Overwriting bytes
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
`21d5f44d`, Stubber only, no network or credentials):

- `python -m pytest -q runtime_tests`: 25 passed.
- `BOOK_MEDIA_SKIP_RENDERER_TESTS=1 python -m pytest -q -rs`: 100 passed,
  6 skipped (renderer flag / Windows-only), 2 subtests.
- `BOOK_MEDIA_SKIP_RENDERER_TESTS=1 bash scripts/check.sh`: 81 tests OK
  (6 skipped).
- `ruff check .` (0.17.1 and 0.17.3): clean. Test fixtures use only synthetic
  keys (`synthetic-access`, `"synthetic-etag-1"`). gitleaks runs in the CI
  `security` job; it is not installed locally.

## Build hygiene 2026-10-29 (branch `build-hygiene`)

Ported from `platform-developer` `8047108`. Two low-severity findings on `main` `916df6e`:

1. `shared-runtime.yml` and `graphics-renderer.yml` used `actions/checkout@v4` and
   `actions/setup-python@v5` by tag. Both are pinned to commit SHAs (`11d5960a...` =
   v4, `a26af69b...` = v5, lightweight tags resolved with `gh api .../git/ref/tags/...`),
   tag in a comment. `check.yml` already called the SHA-pinned reusable workflows.
2. The single-stage `Dockerfile` needed a `CORE_REQUIREMENT` build argument (no
   default), so `docker build .` from a clone failed, and CI never built it. It is now
   the multi-stage build under "Container image", UID `10001` became `65532:65532`
   like the other platform services, and CI builds it.

RED (`tests/test_build_hygiene.py` written first, run on the old files):
`python3 -m unittest tests.test_build_hygiene` 5 of 8 failed (unpinned actions in
`shared-runtime.yml` and `graphics-renderer.yml`; one `FROM`; no core commit in the
Dockerfile; no `docker build` step; no image import check for `boto3` and
`platform_media.app`). The checker self-test, the clean-checkout COPY
check (no `wheelhouse/` here) and the existing non-root `USER` passed.

GREEN: `BOOK_MEDIA_SKIP_RENDERER_TESTS=1 bash scripts/check.sh` and `ruff check .`
pass; the image build is verified only by the CI `image` job (no local Docker).
Nothing was pushed to a registry or deployed.

## Orphan-object sweeper 2026-10-29 (branch `media-sweeper`)

Closes the residual from "Follow-ups 2026-10-29" (F1b): "an upload that lands
after the last DELETE or complete call, but before the link expires, is still
left behind". Design and operation are under "Orphan-object sweeper" above:
`python -m platform_media sweep`, one explicit `MEDIA_WORKSPACE_ID` per run,
batch 1..1000 (default 100), 900 s grace after `expires_at`, migration
`002-media-sweep` (`swept_at` plus a partial index), idempotent S3 delete
(`404`/`NoSuchKey` count as success), counts-only output, exit codes 0/1/2/3, and
a documented bucket lifecycle rule that is not applied from here.

RED (`runtime_tests/test_sweeper.py` written first against `main` `0f87511`):
`python -m pytest -q runtime_tests` gave 10 failed, 25 passed:

- seven tests hit `ModuleNotFoundError: No module named 'platform_media.sweeper'`;
- the CLI test got `invalid choice: 'sweep' (choose from serve, migrate)`;
- the readiness test still got 200 (not 503) after removing the `002-media-sweep`
  history row, because no such migration existed;
- the S3 adapter test raised `botocore.errorfactory.NoSuchKey` from `delete`.

One test helper indexed tuple rows by name. It was fixed during GREEN; the
assertions did not change.

GREEN (Python 3.12, local `pgserver` PostgreSQL 16, core `21d5f44d`, in-memory
store and botocore Stubber only, no network or credentials):

- `python -m pytest -q runtime_tests`: 35 passed (10 new).
- `BOOK_MEDIA_SKIP_RENDERER_TESTS=1 python -m pytest -q -rs`: 118 passed,
  6 skipped (renderer flag / Windows-only), 2 subtests.
- `BOOK_MEDIA_SKIP_RENDERER_TESTS=1 bash scripts/check.sh`: 89 tests OK
  (6 skipped), including `tests/test_build_hygiene.py`.
- `ruff check .` (0.17.1 and 0.17.3): clean.

The new tests cover: only deleted, expired-pending and stuck-deleting rows past the
grace are swept, and ready, open-link or in-grace rows are not; swept rows are
not retried; a missing object counts as swept; a store error leaves the row for the
next run, with logs free of keys, ids and provider text; batch bounds and
`more`; other-workspace rows stay untouched; no row lock is held during the store
call, and a row changed concurrently is skipped then swept next run; CLI exit codes
0/1/2/3 with counts-only output; the sweep runs under the least SELECT/INSERT/UPDATE
grant set; readiness needs `002`; the S3 delete is idempotent.

Not done: scheduling (owner/platform decision), real R2/S3 lifecycle configuration
(deploy-time owner step), deploy.
