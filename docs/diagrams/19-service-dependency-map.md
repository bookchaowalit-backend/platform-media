# 19 — Service dependency map (runtime)

```mermaid
flowchart LR
  subgraph IN["Inbound"]
    CALL["Workspace callers"]
    HUB["Remote MCP hub cell platform-media"]
    UPT["platform-uptime-worker"]
  end
  MEDIA["platform-media API on Railway"]
  subgraph OUT["Outbound, hard dependencies"]
    PG[("Neon PostgreSQL platform-media")]
    JW["platform-identity JWKS"]
    S3[("S3-compatible bucket")]
  end
  subgraph SOFT["Outbound, soft"]
    SEN["Sentry"]
  end
  subgraph BUILD["Build time"]
    CORE["platform-backend-core at 21d5f44 from GitHub"]
    PYPI["PyPI: fastapi via core, uvicorn, boto3, sentry-sdk"]
    BASE["Docker Hub python:3.12-slim"]
  end
  CALL --> MEDIA
  HUB --> MEDIA
  UPT -->|"/readyz"| MEDIA
  MEDIA --> PG
  MEDIA --> JW
  MEDIA --> S3
  MEDIA -.-> SEN
  CORE --> MEDIA
  PYPI --> MEDIA
  BASE --> MEDIA
```

| Dependency | Failure effect (from code) |
|---|---|
| Neon PostgreSQL | `/readyz` 503; API requests fail (core pool waits up to 20 s for Neon resume) |
| Identity JWKS | token verification fails → 401 for all v1 routes; keys cleared on failed refresh |
| Object store | config missing → 503 on uploads/complete/download/delete and `/readyz` 503; provider error → 503 per call; `/readyz` does not call the store |
| Sentry | no effect on requests; no-op when DSN unset |
| Build: core Git, PyPI, base image | image build fails; running service unaffected |

Sources: `pyproject.toml`, `Dockerfile`, `src/platform_media/app.py`, `src/platform_media/adapters.py`, `src/platform_media/_sentry.py`, platform-backend-core `21d5f44` `auth.py`, `db.py`, `docs/runtime.md`, ops record: book-platforms-status (2026-11-03: Neon project, identity issuer, uptime worker, MCP hub)

ไม่มี service-to-service call ขาออกไปยัง platform service อื่น (security, observability ที่ `contract.json` ประกาศไว้ยังไม่ได้ wire)

## Open questions

- consumer ขาเข้าจริงนอกจาก hub และ uptime worker มีใครบ้าง
- ถ้าเข้าผ่าน edge gateway, gateway เป็น dependency ขาเข้าอีกตัว (ยังไม่ยืนยันสำหรับ media)
