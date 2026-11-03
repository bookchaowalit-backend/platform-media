# 21 — Incident response

repo นี้ไม่มี `RUNBOOK.md`; flow ข้างล่างประกอบจาก alert channel ที่มีจริง (uptime worker → Telegram, Sentry),
ข้อกำหนดใน `docs/runtime.md` (Staged deployment and rollback, Orphan-object sweeper) และ deploy recipe ใน ops record

```mermaid
flowchart TD
  A1["Uptime worker: /readyz not 200"] --> T["Triage"]
  A2["Sentry: new unhandled error in platform-media"] --> T
  A3["sweep exit 1 or 3, or more=yes keeps repeating"] --> T
  A4["Caller reports 401, 409, 503"] --> T

  T --> Q{"Which layer?"}
  Q -->|"/readyz 503"| R1["Check Railway logs and deploy, Neon status, grants, migration history, MEDIA_S3 vars"]
  Q -->|"401 everywhere"| R2["Check identity JWKS reachable, JWT_ISSUER and JWT_AUDIENCE vars"]
  Q -->|"503 object store"| R3["Check provider status and credentials"]
  Q -->|"500 in Sentry"| R4["Read type and stack, reproduce in runtime_tests with Postgres"]
  Q -->|"sweep failures"| R5["Rerun sweep, rows stay for next run"]

  R1 --> F["Fix"]
  R2 --> F
  R3 --> F
  R4 --> F
  R5 --> F
  F --> FX{"Code change?"}
  FX -->|"yes"| PR["Branch, failing test first, PR, CI green, merge"]
  FX -->|"config only"| CFG["Infisical then Railway variables, redeploy"]
  PR --> DEP["git archive main, railway up --ci"]
  CFG --> DEP
  DEP --> VER["/readyz 200, sentry enabled log"]
  VER --> PM["Postmortem: record in repo docs, e.g. docs/runtime.md verification section"]
  F -->|"bad release"| RB["Rollback: previous image or config, keep tables and object bytes"]
  RB --> VER
```

Sources: `docs/runtime.md` (Staged deployment and rollback, Orphan-object sweeper exit codes, Verification sections with RED/GREEN), `src/platform_media/app.py`, `src/platform_media/__main__.py`, `src/platform_media/_sentry.py`, ops record: book-platforms-status (2026-11-03: uptime worker, Sentry, deploy recipe), central [ADR-0002](https://github.com/bookchaowalit/architecture-docs/blob/main/docs/architecture/decisions/ADR-0002-production-baseline-monitoring-backups.md) (Telegram alert path)

ตัวอย่างจริงของ loop นี้ใน git: ชุด PR #4–#16 (2026-11-02/03) ที่แก้ Sentry scrub ทีละ bypass จนเปลี่ยนเป็น
"no free text" และ verification sections ใน `docs/runtime.md` ที่บันทึก RED → GREEN ของแต่ละ finding

## Open questions

- severity levels, on-call และ response time ไม่ได้กำหนด
- ที่เก็บ postmortem มาตรฐาน (repo docs, knowledge store, หรือ Linear) ยังไม่ชัด
- ควรเพิ่ม `docs/RUNBOOK.md` ให้ service นี้หรือไม่
