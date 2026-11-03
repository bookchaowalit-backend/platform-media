# 07 — C4 Component

## media API

```mermaid
C4Component
  title Components: media API container
  Container_Boundary(api, "media API") {
    Component(boundary, "core _Boundary middleware", "platform-backend-core", "1 MiB body cap, 30 s timeout, redacted 500, route metrics, correlation log")
    Component(capture, "CaptureErrors", "_sentry.py", "Innermost ASGI layer, reports then re-raises")
    Component(scope, "require_scope", "core http.py", "401 or 403 from TokenVerifier")
    Component(verifier, "TokenVerifier and _JWKS", "core auth.py", "RS256, issuer, audience, ws claim")
    Component(routes, "v1 routes", "app.py", "uploads, complete, files, download, delete")
    Component(ready, "readiness", "app.py", "store present, grants, migration checksums, FORCE RLS")
    Component(gate, "content-type gate", "app.py ALLOWED_CONTENT_TYPES", "16-byte signature check")
    Component(adapter, "S3ObjectStore", "adapters.py", "boto3 client, 3 s connect, 5 s read, 1 attempt")
    Component(cfg, "configured_store", "config.py", "MEDIA_S3_* env")
    Component(dbw, "Database", "core db.py", "pool, rejects SUPERUSER or BYPASSRLS, sets app.workspace_id")
    Component(scrub, "init_sentry and _scrub_event", "_sentry.py", "drops messages, breadcrumbs, request detail")
  }
  ContainerDb(db, "media_files", "PostgreSQL")
  System_Ext(store, "Object store")
  System_Ext(jwks, "Identity JWKS")
  Rel(boundary, capture, "wraps")
  Rel(capture, routes, "wraps")
  Rel(routes, scope, "Depends")
  Rel(scope, verifier, "verify")
  Rel(verifier, jwks, "HTTPS")
  Rel(routes, gate, "on complete")
  Rel(routes, adapter, "store calls outside transactions")
  Rel(cfg, adapter, "builds")
  Rel(routes, dbw, "transaction per workspace")
  Rel(ready, dbw, "privilege and RLS queries")
  Rel(dbw, db, "SQL")
  Rel(adapter, store, "S3 API")
```

Sources: `src/platform_media/app.py`, `src/platform_media/adapters.py`, `src/platform_media/config.py`, `src/platform_media/_sentry.py`, `src/platform_media/__init__.py`, platform-backend-core `21d5f44` `src/platform_backend_core/http.py`, `auth.py`, `db.py`

## Graphics V1A

```mermaid
flowchart LR
  CLI["cli.py main: build or benchmark"] --> SVC["service.py render_batch"]
  CLI --> BEN["benchmark.py run_acceptance_benchmark"]
  BEN --> SVC
  SVC --> IN["inputs.py load_job, _read_records, _load_asset"]
  SVC --> LOCK["service.py _JobLock with heartbeat and reclaim guard"]
  SVC --> TPL["templates.py compile_graphic, fit_text"]
  TPL --> MOD["models.py GraphicJob, GraphicIR, nodes"]
  SVC --> SVG["svg.py compile_svg"]
  SVC --> REN["renderer.py EdgeRenderer, find_edge"]
  REN --> FON["fonts.py require_font, BOOK_MEDIA_FONT_DIRS"]
  SVC --> QA["qa.py validate_svg, inspect_png, verify_embedded_image"]
  SVC --> COMMIT["service.py _commit_output_directory, manifest.json"]
```

Sources: `book_media_platform/graphics/cli.py`, `service.py`, `benchmark.py`, `inputs.py`, `templates.py`, `models.py`, `svg.py`, `renderer.py`, `fonts.py`, `qa.py`
