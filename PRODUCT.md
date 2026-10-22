# Product

<!-- impeccable:product-schema 1 -->

## Platform

contract (Book Platform boundary, `book-platform.contract.v1`)

## Users

- The owner and the agents that change the Book Platform: they read this boundary before
  touching the `media` capability anywhere else.
- Other platform repositories and cells, which may depend on this boundary only through its
  versioned API or event contracts, never its database or private modules.

## Product Purpose

Own the `media` capability (signed upload, file metadata, object storage and image processing) as a written, checked contract, so the capability can move
out of the `solo-empire` control plane later without consumers noticing.

Success: the contract stays valid against the pinned shared schema, the registry entry matches
it, and every gate in `fixtures/gates/` passes in CI.

## Positioning

A boundary contract with offline evidence, not a running service. `book_media_platform/graphics`: a CSV/JSON job to SVG quote-card renderer with QA, a job lock and golden tests; Edge renderer tests run locally only.

## Operating Context

- Status: `scaffolded`; DOCPACK verdict: `merge`. The runtime is not activated; the parent
  registry has no activated production implementation.
- CI: shared `ci-python` (Ruff and `scripts/check.sh` on Python 3.11 and 3.12) and the shared
  security workflow, on GitHub-hosted runners (public repository).
- Sentry does not apply: nothing runs in production. Add it in the change that activates a
  runtime.
- The open `codex/shared-infrastructure-20261015` pull request (decision D8) is outside this
  document.

## Capabilities and Constraints

- `scripts/check.py`: validates `contract.json` against the vendored schema;
  `scripts/check_schema_pin.py`: the vendored schema matches the pinned canonical hash.
- No production provider integration, database writer, credential or customer payload.
- Out of scope: running the capability; it stays in its current implementation until parity
  and rollback evidence permit a cutover.

## Measures

- `scripts/check.sh` green on `main` (target: always).
- Drift between `contract.json` and the registry entry (target: 0).
- [ต้องยืนยัน] The cutover criteria for activating the runtime.
