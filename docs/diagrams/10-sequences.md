# 10 — Sequences

## 1. Create upload intent (`POST /v1/uploads`)

```mermaid
sequenceDiagram
  autonumber
  participant C as Caller
  participant B as "core _Boundary"
  participant A as "media API upload()"
  participant V as TokenVerifier
  participant DB as "Postgres media_files"
  participant S as "S3ObjectStore"
  C->>B: POST /v1/uploads, Bearer JWT, Idempotency-Key, JSON body
  B->>A: body under 1 MiB, 30 s budget
  A->>V: require_scope files:write
  V-->>A: Principal actor, workspace, scopes
  A->>A: Upload model strict validation, fingerprint sha256 of body
  A->>DB: BEGIN, set app.workspace_id, pg_advisory_xact_lock per workspace
  A->>DB: SELECT existing key, if new count pending, 429 if 1000 or more
  A->>DB: INSERT pending ON CONFLICT DO NOTHING
  A->>DB: SELECT row with remaining seconds
  alt fingerprint differs
    A-->>C: 409 idempotency key payload conflict
  else pending and not expired
    A->>S: presign_put key, type, size, checksum, If-None-Match
    S-->>A: url and signed headers, local signing, no network
    A-->>C: 201 new or 200 replay, with upload url and headers
  else expired
    A-->>C: 410 upload intent expired
  end
```

## 2. Complete upload (`POST /v1/uploads/{id}/complete`)

```mermaid
sequenceDiagram
  autonumber
  participant C as Caller
  participant A as "media API complete()"
  participant DB as "Postgres media_files"
  participant S as "Object store"
  C->>A: POST complete, Bearer JWT files:write
  A->>DB: Phase 1 read row and expired flag, commit
  alt deleted
    A->>S: purge late object if window open, best effort
    A-->>C: 404
  else ready
    A-->>C: 200 existing metadata
  else deleting
    A-->>C: 409 file is being deleted
  else pending but expired
    A-->>C: 410 upload intent expired
  else pending
    A->>S: Phase 2 HEAD with ChecksumMode ENABLED
    S-->>A: size, content type, SHA256, ETag
    A->>A: compare with row, 409 on mismatch or no ETag
    A->>S: GET Range bytes 0-15 with If-Match ETag
    S-->>A: 16 bytes, or 412 mapped to None
    A->>A: signature check for declared type, 409 on mismatch
    A->>DB: Phase 3 SELECT FOR UPDATE, row unchanged and pending
    A->>DB: UPDATE status ready, completed_at now
    A-->>C: 200 status ready
  end
```

## 3. Delete file (`DELETE /v1/files/{id}`)

```mermaid
sequenceDiagram
  autonumber
  participant C as Caller
  participant A as "media API delete()"
  participant DB as "Postgres media_files"
  participant S as "Object store"
  C->>A: DELETE, Bearer JWT files:write
  A->>DB: SELECT FOR UPDATE with upload_window_open
  alt not found
    A-->>C: 404
  else already deleted and window closed
    A-->>C: 204, no store call
  else not yet deleted
    A->>DB: UPDATE status deleting, commit
  end
  A->>S: delete_object, missing key counts as deleted
  alt store error
    A-->>C: 503 object deletion pending, retry safely
  else ok
    A->>DB: UPDATE status deleted, deleted_at now
    A-->>C: 204
  end
```

## 4. Orphan sweep (`python -m platform_media sweep`)

```mermaid
sequenceDiagram
  autonumber
  participant O as "Operator or scheduler"
  participant W as sweeper.sweep
  participant DB as "Postgres media_files"
  participant S as "Object store"
  O->>W: DATABASE_URL, MEDIA_WORKSPACE_ID, MEDIA_S3_*, MEDIA_SWEEP_BATCH
  W->>DB: SELECT candidates, swept_at null, status pending or deleting or deleted, expires_at older than 900 s, limit batch+1
  loop each candidate, no transaction open
    W->>S: delete_object
    alt store error
      W->>W: failed += 1
    else deleted
      W->>DB: UPDATE swept_at, deleting becomes deleted, only if row unchanged
      W->>W: swept or skipped
    end
  end
  W-->>O: swept=N failed=N skipped=N more=yes or no, exit 0 or 3
```

Sources: `src/platform_media/app.py` (`upload`, `complete`, `delete`, `purge_late_object`), `src/platform_media/adapters.py`, `src/platform_media/sweeper.py`, `src/platform_media/__main__.py` (`sweep_command`), platform-backend-core `21d5f44` `http.py` (`_Boundary`, `require_scope`), `auth.py`, `db.py`, `docs/runtime.md`
