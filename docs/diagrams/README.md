# Core 22 SDLC diagram pack — platform-media

ชุดภาพ Core 22 ตาม SDLC Diagram Catalog v2 วาดจากโค้ดและ config ใน repo นี้ (ณ `origin/main` `f3b4a72`, 2026-11-03)
platform-media เป็น backend platform service ของ Book Platform: media API (FastAPI + PostgreSQL FORCE RLS + S3-compatible
presigned upload) บน Railway serverless และ Graphics V1A CLI ที่รันบนเครื่อง local

ข้อมูล hosting ที่ไม่อยู่ใน repo อ้างจาก `ops record: book-platforms-status (2026-11-03)` เฉพาะบรรทัดที่ระบุ media
ทุกไฟล์มี `Sources:` ใต้ภาพ และ "Open questions" สำหรับสิ่งที่ยืนยันจากโค้ดไม่ได้
เอกสารเดิมที่ reuse: [`docs/runtime.md`](../runtime.md), [`docs/CONTRACT-SURFACE.md`](../CONTRACT-SURFACE.md),
[`docs/graphics-v1a.md`](../graphics-v1a.md), [`docs/ci-renderer-gates.md`](../ci-renderer-gates.md), [`docs/UPGRADE-PLAN.md`](../UPGRADE-PLAN.md)

| # | Item | Status | File | Sources read |
|---|---|---|---|---|
| 01 | Business process As-Is / To-Be | done | [01-business-process.md](01-business-process.md) | `app.py`, `sweeper.py`, graphics `cli.py`/`service.py`, `PRODUCT.md`, `UPGRADE-PLAN.md`, `contract.json` |
| 02 | Use cases | done | [02-use-cases.md](02-use-cases.md) | `app.py` scopes, core `auth.py`/`http.py`, `__main__.py`, ops record |
| 03 | User flows (API call flows) | done | [03-user-flows.md](03-user-flows.md) | `app.py`, `__main__.py`, `docs/runtime.md` |
| 04 | Traceability (RTM) | done | [04-traceability.md](04-traceability.md) | `contract.json`, `PRODUCT.md`, `docs/runtime.md`, `tests/`, `runtime_tests/`, workflows |
| 05 | C4 Context | done | [05-c4-context.md](05-c4-context.md) | `app.py`, `adapters.py`, `_sentry.py`, `contract.json`, ops record |
| 06 | C4 Container | done | [06-c4-container.md](06-c4-container.md) | `Dockerfile`, `__main__.py`, `migrations.py`, `sweeper.py`, graphics |
| 07 | C4 Component | done | [07-c4-component.md](07-c4-component.md) | `src/platform_media/*`, core `21d5f44`, `book_media_platform/graphics/*` |
| 08 | ADRs | done (5) | [08-adr/](08-adr/) | `pyproject.toml`, `Dockerfile`, `app.py`, `migrations.py`, `_sentry.py`, git log, ops record |
| 09 | ERD | done | [09-erd.md](09-erd.md) | `migrations.py`, core `migrations.py`, `adapters.py` |
| 10 | Sequences | done (4) | [10-sequences.md](10-sequences.md) | `app.py`, `adapters.py`, `sweeper.py`, core `http.py`/`auth.py`/`db.py` |
| 11 | State machines | done (3) | [11-state-machines.md](11-state-machines.md) | `migrations.py`, `app.py`, `sweeper.py`, graphics `service.py` |
| 12 | API & integrations | done (reuses `docs/runtime.md`) | [12-api-integration.md](12-api-integration.md) | `app.py`, core `http.py`, `__main__.py`, `adapters.py`, `contract.json` |
| 13 | Threat model | done | [13-threat-model.md](13-threat-model.md) | core `auth.py`/`http.py`/`db.py`, `app.py`, `adapters.py`, `_sentry.py`, `docs/runtime.md` |
| 14 | Backup & DR | done (gaps listed) | [14-backup-dr.md](14-backup-dr.md) | `docs/runtime.md`, `migrations.py`, ops record, central ADR-0002 |
| 15 | CI/CD pipeline | done | [15-cicd-pipeline.md](15-cicd-pipeline.md) | `.github/workflows/*.yml`, `scripts/check.sh`, `docs/ci-renderer-gates.md` |
| 16 | Deployment | done | [16-deployment.md](16-deployment.md) | `Dockerfile`, `__main__.py`, `_sentry.py`, `docs/runtime.md`, ops record |
| 17 | Test coverage matrix | done | [17-test-coverage-matrix.md](17-test-coverage-matrix.md) | `tests/*.py`, `runtime_tests/*.py`, `shared-runtime.yml` |
| 18 | Git & release workflow | done | [18-git-release-workflow.md](18-git-release-workflow.md) | `git log --first-parent`, `gh pr list`, ops record |
| 19 | Service dependency map | done | [19-service-dependency-map.md](19-service-dependency-map.md) | `pyproject.toml`, `Dockerfile`, `app.py`, `adapters.py`, core, ops record |
| 20 | Observability | done | [20-observability.md](20-observability.md) | core `http.py`, `_sentry.py`, `sweeper.py`, ops record, central ADR-0002 |
| 21 | Incident response | done | [21-incident-response.md](21-incident-response.md) | `docs/runtime.md`, `_sentry.py`, ops record, central ADR-0002 |
| 22 | DORA | done (2 partial, 2 proxy, 1 not computable) | [22-dora.md](22-dora.md) | `git log`, `gh pr list`, `gh run list`, ops record |

สรุปสถาปัตยกรรมระดับ docpack: [`DOCPACK/02-architecture.md`](../../DOCPACK/02-architecture.md) (2026-11-04)

## ADR list

- [ADR-0001](08-adr/ADR-0001-fastapi-on-shared-platform-backend-core.md) FastAPI บน platform-backend-core ที่ pin `21d5f44`
- [ADR-0002](08-adr/ADR-0002-presigned-direct-upload-with-server-verification.md) Presigned direct upload + server verification
- [ADR-0003](08-adr/ADR-0003-postgres-force-rls-per-workspace.md) PostgreSQL FORCE RLS ต่อ workspace
- [ADR-0004](08-adr/ADR-0004-railway-serverless-cli-deploy.md) Railway serverless Singapore, CLI deploy
- [ADR-0005](08-adr/ADR-0005-sentry-errors-only-no-free-text.md) Sentry errors only, no free text

## Validation

```text
node ~/work/core22/tools/validate.mjs docs/diagrams/*.md docs/diagrams/08-adr/*.md
```
