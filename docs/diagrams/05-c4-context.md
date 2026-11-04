# 05 — C4 Context

```mermaid
C4Context
  title System Context: platform-media
  Person(caller, "Workspace caller", "App, agent or service with a media-api JWT")
  Person(operator, "Owner / operator", "Runs migrate, sweep, CLI deploys")
  System(media, "platform-media", "Media API: signed upload, file metadata, object storage; Graphics V1A CLI")
  System_Ext(identity, "platform-identity", "Issuer and JWKS for RS256 workspace tokens")
  System_Ext(store, "S3-compatible object store", "Bucket behind MEDIA_S3_ENDPOINT, region default auto (R2)")
  SystemDb_Ext(neon, "Neon PostgreSQL", "Project platform-media")
  System_Ext(sentry, "Sentry", "Project platform-media, errors only")
  System_Ext(uptime, "platform-uptime-worker", "Hourly /readyz probe")
  System_Ext(hub, "Remote MCP hub", "Cell platform-media")
  System_Ext(gw, "Edge gateway", "gateway.bookchaowalit.com /v1/platform-media/*, token pass-through")
  Rel(caller, gw, "HTTPS JSON API", "Bearer JWT")
  Rel(gw, media, "Proxies with the same token", "HTTPS")
  Rel(caller, media, "HTTPS JSON API, direct URL", "Bearer JWT")
  Rel(caller, store, "PUT and GET bytes", "presigned URL")
  Rel(hub, media, "Calls API", "client_credentials token")
  Rel(media, identity, "Fetches JWKS", "HTTPS")
  Rel(media, store, "Presign, HEAD, ranged GET, DELETE", "boto3 S3v4")
  Rel(media, neon, "SQL with RLS", "psycopg")
  Rel(media, sentry, "Error events without free text", "HTTPS")
  Rel(uptime, media, "GET /readyz", "HTTPS")
  Rel(operator, media, "migrate, sweep, railway up", "CLI")
```

Sources: `src/platform_media/app.py`, `src/platform_media/adapters.py`, `src/platform_media/config.py`, `src/platform_media/_sentry.py`, `src/platform_media/__main__.py`, `docs/runtime.md`, `contract.json` (`depends_on`), ops record: book-platforms-status (2026-11-03: Railway service, Neon project, Sentry project, uptime worker, MCP hub cell, identity issuer), platform-api-gateway `routes.json` + `README.md` @ `4a31ff5` (route `platform-media` → `https://media-api-production-91a5.up.railway.app`; added 2026-11-04)

`contract.json` ประกาศ `depends_on: identity, security, observability` แต่ในโค้ดวันนี้มีการเรียกจริงแค่ identity (JWKS)
ไม่มีการเรียก security หรือ observability service

## Open questions

- gateway มี route `platform-media` แล้ว (`routes.json` @ `4a31ff5`) แต่ README ของ gateway บอกว่า direct URL ยังใช้ได้จนกว่าจะบันทึก cutover และ media ไม่เช็ค `X-Gateway-Assertion`: caller ตัวไหนย้ายมา gateway แล้ว และจะปิด direct URL เมื่อไร
- object store จริงคือ Cloudflare R2 หรือ provider อื่น: โค้ดรับ endpoint ใดก็ได้ที่เป็น HTTPS, default region `auto` ชี้ไปทาง R2 แต่ไม่มีบันทึกยืนยัน
- dependency `security` และ `observability` ใน contract จะ wire เมื่อไร
