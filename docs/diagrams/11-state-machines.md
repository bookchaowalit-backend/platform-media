# 11 — State machines

## `media_files.status`

`status` มี CHECK constraint `pending | ready | deleting | deleted` ส่วน "expired" ไม่ใช่ status จริง:
เป็นเงื่อนไข `now() >= expires_at` ที่ทำให้ complete ตอบ 410 และทำให้ sweep เก็บ object ได้หลัง grace 900 s

```mermaid
stateDiagram-v2
  [*] --> pending: POST /v1/uploads 201
  pending --> pending: complete 409 mismatch, or 410 when expired
  pending --> ready: complete, HEAD and signature match, not expired
  pending --> deleting: DELETE commits deleting
  ready --> deleting: DELETE commits deleting
  deleting --> deleted: store delete ok, DELETE sets deleted_at
  deleting --> deleting: store delete fails, DELETE returns 503
  deleting --> deleted: sweep after expiry plus grace
  deleted --> deleted: DELETE replay in upload window purges late object
  deleted --> [*]: tombstone kept, hidden from reads
  ready --> [*]: kept until DELETE, no automatic expiry
```

Sweep ไม่เปลี่ยน status ของ `pending` และ `deleted`: แค่ลบ object และตั้ง `swept_at` (row นั้นจะไม่ถูกเลือกอีก)

Sources: `src/platform_media/migrations.py` (CHECK on status, `swept_at`), `src/platform_media/app.py` (`upload`, `complete`, `delete`, `fetch`), `src/platform_media/sweeper.py`, `docs/runtime.md` (Orphan-object sweeper)

## Graphics job result

```mermaid
stateDiagram-v2
  [*] --> validating: build --job
  validating --> rejected: GraphicsError, exit 2
  validating --> waiting_lock: valid job
  waiting_lock --> rejected: lock busy over 15 s
  waiting_lock --> rendering: lock held, heartbeat every 30 s
  rendering --> rejected: QA fails, nothing committed
  rendering --> accepted: atomic commit to output/job_id
  waiting_lock --> replay: output exists and hashes verify
  waiting_lock --> rejected: same job_id, different inputs
  accepted --> [*]
  replay --> [*]
  rejected --> [*]
```

## Graphics job lock

```mermaid
stateDiagram-v2
  [*] --> free
  free --> held: O_EXCL create with pid, host, created_at
  held --> held: heartbeat refreshes mtime every 30 s
  held --> free: holder releases, unlinks only its own lock
  held --> stale: pid gone on this host, or no heartbeat 10 min
  stale --> reclaiming: contender takes lock.reclaim guard
  reclaiming --> held: re-check under guard, new holder
  reclaiming --> held: lock was live, spared
```

Sources: `book_media_platform/graphics/service.py` (`_JobLock`, `_lock_is_stale`, `_acquire_reclaim_guard`, `render_batch`, `_replay_result`), `book_media_platform/graphics/cli.py`, `docs/CONTRACT-SURFACE.md` (Behaviour notes), `tests/test_job_lock.py`
