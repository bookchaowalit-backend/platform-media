# platform-media

`Media` platform boundary for the Book Platform portfolio.

## Scope

This repository owns the `media` capability: signed upload, file metadata,
object storage and image processing. The repository contains a separately packaged FastAPI/PostgreSQL media API and
a local deterministic Graphics V1A pilot. The API is implemented-local;
production activation and provider compatibility certification remain pending.

## Graphics V1A

Template Batch accepts a `graphics.job.v1` JSON job whose records are inline,
loaded from JSON, or loaded from CSV. It writes editable SVG and PNG previews
for quote cards, product cards and announcements in square, portrait and story
sizes. The ordinary path uses local layout rules only (`model_calls=0`). See
[`docs/graphics-v1a.md`](docs/graphics-v1a.md) for the job format, limits and
example.

Run a sample from this repository:

```bash
python -m book_media_platform.graphics build \
  --job examples/graphics/quote-cards-job.json \
  --output .runtime/graphics-output
```

The local renderer requires Microsoft Edge Headless and an installed supported
font. Set `BOOK_MEDIA_EDGE_PATH` only when Edge is installed outside the normal
search paths, and `BOOK_MEDIA_FONT_DIRS` (absolute directories separated by the
platform path separator) when a supported font lives outside the operating
system font folders. The renderer runs without background networking and accepts
only generated SVG with embedded local raster assets.

## Boundary

- Owner: `bookchaowalit-backend`
- Repository: `platform-media`
- Target remote: `https://github.com/bookchaowalit-backend/platform-media.git` (published; runtime not activated)
- Contract: `book-platform.contract.v1`
- Status: `pilot`
- Data owner: the platform boundary identified in `contract.json`

The platform communicates through versioned API or event contracts. Consumers
must not import another platform's database, migration, or private runtime
module. `solo-empire` remains the control plane and compatibility adapter until
parity and rollback evidence permit a cutover.

## Local verification

Run from this repository:

```bash
bash scripts/check.sh
```

The check validates `contract.json` against
[`schema/book-platform.contract.v1.schema.json`](schema/book-platform.contract.v1.schema.json),
confirms that the README and
[`docs/CONTRACT-SURFACE.md`](docs/CONTRACT-SURFACE.md) agree with the
contract, and runs the product test suite. Contract tests use a stand-in font
through `BOOK_MEDIA_FONT_DIRS`, so they do not depend on host fonts; renderer
tests skip when Edge or Arial is unavailable. GitHub Actions runs the same
command on every push and pull request (`.github/workflows/check.yml`).
Planned work is tracked in [`docs/UPGRADE-PLAN.md`](docs/UPGRADE-PLAN.md).

`scripts/check_schema_pin.py` fails when the vendored schema no longer
matches the sha256 pinned in `schema/book-platform.contract.v1.schema.json.sha256`
(the canonical copy lives in `bookchaowalit-backend-core/contracts/`; change it
there first, then copy the schema and its pin here). Pass
`--canonical ../platform-backend-core` to also compare with a local
checkout. `python3 scripts/check_registry_alignment.py --solo-empire ../solo-empire`
compares this contract with the parent platform registry (local only; read-only).

The check does not claim deployment, provider connectivity, data migration, or
production readiness.

## Migration gate

Before activating a remote or changing a consumer, add sanitized fixtures for
success, duplicate delivery, timeout and provider failure; prove tenant/privacy
isolation; compare the old and new contract; and rehearse rollback on a
disposable state store. Record the evidence in the parent platform registry.

## Shared API runtime

Stage: **implemented-local**, correlation `SHARED-INFRA-2026-10-15`.
The HTTP API and explicit migrations use the separately installed
`platform-backend-core` distribution. The hub is not a runtime dependency.
See [API, configuration and release runbook](docs/runtime.md) and
[local verification evidence](docs/verification-2026-10-15.md).

Install a reviewed core wheel/revision first, then `pip install -e '.[test]'`.
Run `python -m platform_media migrate` explicitly with `DATABASE_MIGRATION_DSN`,
then `python -m platform_media serve` with `DATABASE_URL` and JWT configuration.
Run `python -m pytest` with a disposable non-superuser `PLATFORM_TEST_DSN`
to exercise the complete original and new suite. Database tests explicitly skip
when that DSN is absent. These skips do not establish durability or isolation.
