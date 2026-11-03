# 03 — User flows (API call flows per caller)

service นี้ไม่มี UI: "user flow" คือลำดับการเรียก API ของ caller แต่ละแบบ ตาม router ใน `src/platform_media/app.py`

## Flow A: uploader (`files:write`) — upload ไฟล์จนพร้อมใช้

```mermaid
flowchart TD
  A1["Compute SHA256 of file, base64 encode"] --> A2["POST /v1/uploads with Idempotency-Key and JSON body"]
  A2 -->|"201 or 200"| A3["Read upload.url and upload.headers"]
  A2 -->|"409"| AX1["Payload differs for same key: use a new key"]
  A2 -->|"410"| AX2["Intent expired: new key"]
  A2 -->|"429"| AX3["1000 pending uploads in workspace: wait"]
  A2 -->|"503"| AX4["Store not configured or unavailable: retry"]
  A3 --> A4["PUT bytes to url with all signed headers"]
  A4 --> A5["POST /v1/uploads/id/complete"]
  A5 -->|"200 status=ready"| A6["Done"]
  A5 -->|"409 metadata mismatch, object missing"| A7["PUT the same bytes with all signed headers, complete again"]
  A5 -->|"409 existing object mismatch or wrong content"| A9["DELETE /v1/files/id, then new intent with a new Idempotency-Key and corrected metadata and checksum"]
  A5 -->|"410"| AX2
  A5 -->|"503"| A8["Retry complete"]
  A7 --> A5
  A9 --> A1
  A8 --> A5
```

## Flow B: reader (`files:read`) — หาและดาวน์โหลดไฟล์

```mermaid
flowchart TD
  B1["GET /v1/files?limit=50&offset=0"] --> B2["Pick id"]
  B2 --> B3["GET /v1/files/id"]
  B3 -->|"404"| BX["Not in this workspace or deleted"]
  B3 --> B4["GET /v1/files/id/download"]
  B4 -->|"200 url, expires_in=300"| B5["GET url from object store within 300 s"]
  B4 -->|"409"| B6["File still pending or deleting"]
  B4 -->|"503"| B7["Retry"]
```

## Flow C: deleter (`files:write`)

```mermaid
flowchart TD
  C1["DELETE /v1/files/id"] -->|"204"| C2["Done, tombstone kept"]
  C1 -->|"404"| C3["Unknown or other workspace"]
  C1 -->|"503 object deletion pending"| C4["Replay DELETE, safe"]
  C4 --> C1
```

## Flow D: operator (CLI)

```mermaid
flowchart TD
  D1["python -m platform_media migrate with DATABASE_MIGRATION_DSN"] --> D2["Create runtime role NOSUPERUSER NOBYPASSRLS with readiness grants"]
  D2 --> D3["python -m platform_media serve"]
  D3 --> D4["GET /readyz returns 200"]
  D4 --> D5["Per workspace: python -m platform_media sweep"]
  D5 -->|"exit 0, more=yes"| D5
  D5 -->|"exit 0, more=no"| D6["Done until next schedule"]
  D5 -->|"exit 1 or 3"| D7["Alert and investigate"]
```

Sources: `src/platform_media/app.py`, `src/platform_media/__main__.py`, `docs/runtime.md` (Versioned API, Orphan-object sweeper, Staged deployment), ops record: book-platforms-status (2026-11-03, "Database roles" recipe)

## Open questions

- ไม่มี client SDK หรือ consumer ใน repo: flow ข้างบนมาจากสัญญา API ไม่ใช่จาก app ที่เรียกจริง
- Browser upload ต้องตั้ง bucket CORS ซึ่ง ops record ระบุว่ายังรอ app origins จาก owner
- ตาราง schedule ของ `sweep` ต่อ workspace ยังไม่มี (owner follow-up ใน ops record)
