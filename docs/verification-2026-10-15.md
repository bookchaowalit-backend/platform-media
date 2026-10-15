# Local verification: 2026-10-15

Correlation: `SHARED-INFRA-2026-10-15`. Stage **implemented-local**; contract
status `pilot`. No push, deploy, production cutover, live provider call, charge,
external message or credential discovery was performed.

Environment: Python 3.14.4, PostgreSQL 18, FastAPI 0.143.1, psycopg 3.3.6,
psycopg-pool 3.3.3, pytest 9.1.1, HTTPX 0.28.1. Provider libraries: boto3
1.43.114 and Stripe 14.4.1. Tests use a dedicated service database and a
NOSUPERUSER NOBYPASSRLS role. Every DB test asserts real FORCE RLS behavior;
workspaces are randomly generated. The shared core was loaded from its local
checkout for this development run; release artifact pinning belongs to the
parent integration/release step.

Commands run from this repository (interpreter in the isolated shared-infra
venv, `PYTHONPATH` containing this `src` and the sibling core `src`):

```bash
bash scripts/check.sh
python -m pytest -q
python -m pip wheel --no-deps --wheel-dir <disposable-wheel-output> .
git diff --check
```

`PLATFORM_TEST_DSN` was supplied explicitly for the service-owned disposable
DB; no secret-bearing local file was read. Absence of the DSN skips DB tests
and must not be represented as isolation/durability evidence.

## Results

- Original `bash scripts/check.sh`: 81 tests run, 75 passed, 6 skipped.
- Complete `python -m pytest -q`: **84 passed, 6 skipped**, 2 subtests passed.
  Nine new runtime tests passed, including real PostgreSQL cross-workspace
  reads/completion/delete denial; concurrent idempotent creation; changed payload
  conflict; forged completion and byte/type/checksum mismatch; expiry/body/type
  bounds; real JWT and concrete scope; two independent API pools; pending-upload
  quota; actual boto3 presigned headers and HEAD results without network.
- Real S3 signer includes conditional create, length, type and SHA256 in the
  signed header list. Stubber proves provider-computed HEAD checksum binding.
- Pending quota permits at most 1000 unexpired pending uploads per workspace,
  serialized transactionally; replay of an existing key still works at quota.
- Wheel build passed; contents include runtime, migrations and the unchanged
  `book_media_platform.graphics` modules. No graphics module was edited.
- `git diff --check` passed.

## Red / green record

Tests were written before the runtime existed; first run reported missing
packages. After the shared core became available the behavioral run found a
pooled connection row-factory leak (`KeyError: 0`) in the tenant/concurrency
check. Restoring connection state at transaction exit made it pass. An added
readiness test observed HTTP 200 with missing adapter/schema before the service
probe was implemented, then 503 after the fix.

The pending-quota test was run **before** quota enforcement: the 1001st active
intent returned 201 while the assertion expected 429. Transactional per-workspace
advisory locking and quota enforcement then made that exact test pass, followed
by the complete suite above.

## Explicit skips and limitations

Five original renderer integration tests skipped because Microsoft Edge is
missing: headless preview, long product layout, nine golden previews, embedded
asset render and deep output-root render. The sixth skip is the original
Windows renderer process-tree timeout test on this Linux host. These are not
passed rendering checks. One Starlette warning recommends HTTPX2 for future
testclient compatibility; tests currently run on HTTPX 0.28.1.

No real object provider or Docker image was executed. R2/S3 conditional PUT,
computed-checksum HEAD, CORS, lifecycle cleanup and provider permissions require
approved staging evidence. Ready-file automatic retention and orphan sweeping
are not implemented; ready files use explicit authorized deletion. Issued URLs
expire normally and cannot be individually revoked. Production rollout,
backup/restore and cross-service load evidence remain parent release gates.
