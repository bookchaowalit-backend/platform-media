"""Orphan-object sweeper for one explicitly configured workspace.

A presigned PUT cannot be revoked, so bytes can land at an intent's key after the
last DELETE/complete call and before the link expires. Once the link has expired
(plus a clock-skew grace), nothing can write that key again, so the sweeper deletes
the object of every intent that is deleted, stuck in deleting, or expired without
completing, and stamps ``swept_at`` so the row is not retried. Ready files are never
touched. No transaction is held across a store call; the mark is conditional on the
row being unchanged, so a concurrent DELETE/complete is re-checked on the next run.
Logs carry counts only (no keys, workspace ids or provider messages)."""

import logging
from dataclasses import dataclass
from uuid import UUID

LOG = logging.getLogger("platform_media.sweeper")

# Presigned PUT expiry is computed from the API host clock and checked by the
# provider's clock; wait this long past expires_at before treating a link as dead.
SWEEP_GRACE_SECONDS = 900
DEFAULT_BATCH = 100
MAX_BATCH = 1000

# Table privileges the sweeper uses; a subset of app.READINESS_PERMISSIONS.
SWEEP_PERMISSIONS = {"media_files": ("SELECT", "UPDATE")}


@dataclass(frozen=True)
class SweepResult:
    swept: int  # object deleted (or already missing) and row marked
    failed: int  # store delete failed; row left for the next run
    skipped: int  # row changed during the delete; re-checked next run
    more: bool  # batch was full; run again to continue


def sweep(
    database,
    store,
    workspace: UUID,
    *,
    batch_size: int = DEFAULT_BATCH,
    grace_seconds: int = SWEEP_GRACE_SECONDS,
) -> SweepResult:
    if type(batch_size) is not int or not 1 <= batch_size <= MAX_BATCH:
        raise ValueError(f"batch size must be 1..{MAX_BATCH}")
    if type(grace_seconds) is not int or grace_seconds < 0:
        raise ValueError("grace must be a non-negative integer")
    with database.transaction(workspace) as conn:
        candidates = conn.execute(
            "SELECT id, object_key, status, expires_at FROM media_files"
            " WHERE workspace_id=%s AND swept_at IS NULL"
            " AND status IN ('pending','deleting','deleted')"
            " AND expires_at <= now() - %s * interval '1 second'"
            " ORDER BY expires_at, id LIMIT %s",
            (workspace, grace_seconds, batch_size + 1),
        ).fetchall()
    more = len(candidates) > batch_size
    swept = failed = skipped = 0
    for identifier, object_key, status, expires_at in candidates[:batch_size]:
        try:
            store.delete(object_key)
        except Exception:  # noqa: BLE001 - provider errors are counted, never logged
            failed += 1
            continue
        with database.transaction(workspace) as conn:
            marked = conn.execute(
                "UPDATE media_files SET swept_at=now(),"
                " deleted_at=CASE WHEN status='deleting' THEN coalesce(deleted_at, now()) ELSE deleted_at END,"
                " status=CASE WHEN status='deleting' THEN 'deleted' ELSE status END"
                " WHERE id=%s AND swept_at IS NULL AND status=%s AND object_key=%s AND expires_at=%s"
                " RETURNING id",
                (identifier, status, object_key, expires_at),
            ).fetchone()
        if marked is None:
            skipped += 1
        else:
            swept += 1
    if failed:
        LOG.warning("media sweep: %d object deletions failed; rows left for the next run", failed)
    LOG.info("media sweep: swept=%d failed=%d skipped=%d more=%s", swept, failed, skipped, more)
    return SweepResult(swept, failed, skipped, more)
