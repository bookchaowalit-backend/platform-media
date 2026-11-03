# 06 — C4 Container

```mermaid
C4Container
  title Containers: platform-media
  Person(caller, "Workspace caller", "Holds files:read and or files:write")
  Person(operator, "Operator", "CLI with DSN and S3 env")
  System_Boundary(sys, "platform-media") {
    Container(api, "media API", "Python 3.12, FastAPI, uvicorn, platform-backend-core 21d5f44", "python -m platform_media serve on PORT 8080, concurrency 64")
    Container(migrate, "migrate command", "Python CLI", "Applies MIGRATIONS 001-media and 002-media-sweep with DATABASE_MIGRATION_DSN")
    Container(sweep, "sweep command", "Python CLI", "One bounded batch per MEDIA_WORKSPACE_ID")
    Container(gfx, "Graphics V1A CLI", "Python, headless Microsoft Edge", "Local only, not in the image")
    ContainerDb(db, "media_files table", "PostgreSQL 16+ on Neon", "FORCE RLS on app.workspace_id")
  }
  System_Ext(store, "Object store bucket", "S3-compatible")
  System_Ext(identity, "platform-identity JWKS", "RS256 keys")
  System_Ext(sentry, "Sentry", "errors only")
  ContainerDb_Ext(out, "Local output directory", "SVG, PNG, manifest.json, .locks")
  Rel(caller, api, "HTTPS JSON", "Bearer JWT")
  Rel(caller, store, "PUT or GET bytes", "presigned URL")
  Rel(api, db, "SELECT, INSERT, UPDATE as runtime role", "psycopg pool max 8")
  Rel(api, store, "presign, HEAD, ranged GET, DELETE", "boto3")
  Rel(api, identity, "JWKS refresh at most every 60 s", "HTTPS")
  Rel(api, sentry, "scrubbed events", "sentry-sdk")
  Rel(operator, migrate, "runs")
  Rel(operator, sweep, "runs")
  Rel(migrate, db, "DDL as owner role")
  Rel(sweep, db, "SELECT, UPDATE")
  Rel(sweep, store, "DeleteObject")
  Rel(operator, gfx, "build or benchmark")
  Rel(gfx, out, "atomic commit")
```

Sources: `Dockerfile`, `pyproject.toml`, `src/platform_media/__main__.py`, `src/platform_media/app.py`, `src/platform_media/migrations.py`, `src/platform_media/sweeper.py`, `src/platform_media/adapters.py`, `book_media_platform/graphics/cli.py`, `book_media_platform/graphics/renderer.py`, `docs/runtime.md` (Container image), platform-backend-core `21d5f44` `db.py`, `auth.py`

ใน image มีแค่ `serve` เป็น `CMD`; `migrate` และ `sweep` ใช้ image เดียวกันแบบ command override (`docs/runtime.md`)
Graphics package ถูก install ใน image ด้วย แต่ render ไม่ได้เพราะไม่มี Edge / Arial

## Open questions

- `sweep` ยังไม่มี scheduler (Railway cron / GH Actions) ตาม ops record: ใครจะเป็น trusted scheduler
- `migrate` บน production รันจากเครื่อง owner หรือจาก one-off Railway run: ไม่มีบันทึกใน repo
