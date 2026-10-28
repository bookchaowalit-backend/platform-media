"""Orphan-object sweeper: `python -m platform_media sweep` (docs/runtime.md, 2026-10-29).

Real PostgreSQL through the restricted runtime role, an injected in-memory store and
the real boto3 client under botocore Stubber. No network, no credentials."""

import logging
import os
import sys
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from test_api import BODY, PNG_PREFIX, FakeStore

PAST = -7200  # seconds from now: well beyond the sweep grace period


class Store(FakeStore):
    def __init__(self):
        super().__init__()
        self.fail = set()  # keys whose delete raises
        self.on_delete = None  # test hook: runs before the delete takes effect

    def delete(self, key):
        if self.on_delete is not None:
            self.on_delete(key)
        if key in self.fail:
            raise RuntimeError("synthetic provider failure")
        super().delete(key)


@pytest.fixture
def media(monkeypatch):
    from platform_backend_core import Database, MigrationRunner, Principal
    from platform_backend_core.testing import temporary_database

    from platform_media import MIGRATIONS, create_app

    dsn = os.environ.get("PLATFORM_TEST_DSN")
    if not dsn:
        pytest.skip("PLATFORM_TEST_DSN required: disposable fixture admin PostgreSQL DSN")
    with temporary_database(dsn, prefix="media_sweep_") as isolated:
        MigrationRunner(isolated.admin_dsn, namespace="platform_media").apply(MIGRATIONS)
        isolated.grant_schema()
        db = Database(isolated.runtime_dsn, max_size=4)
        ws, other = uuid4(), uuid4()

        class Verifier:
            def verify(self, authorization):
                from fastapi import HTTPException

                if not authorization:
                    raise HTTPException(401, "authentication required")
                return Principal(
                    uuid4(),
                    ws if authorization == "Bearer own" else other,
                    frozenset(["files:read", "files:write"]),
                )

        store = Store()
        app = create_app(db, Verifier(), object_store=store)
        try:
            with TestClient(app) as client:
                yield client, db, ws, other, store, isolated
        finally:
            db.close()


def _intent(c, name, owner="own"):
    created = c.post(
        "/v1/uploads",
        json=BODY,
        headers={"Authorization": "Bearer " + owner, "Idempotency-Key": name},
    )
    assert created.status_code == 201, created.text
    key = created.json()["upload"]["url"].split("/upload/", 1)[1]
    return created.json()["id"], key


def _put(store, key):
    store.objects[key] = {
        "size": BODY["size"],
        "content_type": BODY["content_type"],
        "checksum_sha256": BODY["checksum_sha256"],
        "prefix": PNG_PREFIX,
    }


def _expire(db, ws, file_id, seconds=PAST):
    with db.transaction(ws) as conn:
        conn.execute(
            "UPDATE media_files SET expires_at = now() + %s * interval '1 second' WHERE id=%s",
            (seconds, file_id),
        )


def _row(db, ws, file_id):
    with db.transaction(ws) as conn:
        status, swept, has_deleted_at = conn.execute(
            "SELECT status, swept_at IS NOT NULL, deleted_at IS NOT NULL FROM media_files WHERE id=%s",
            (file_id,),
        ).fetchone()
    return {"status": status, "swept": swept, "has_deleted_at": has_deleted_at}


AUTH = {"Authorization": "Bearer own"}


def test_sweep_removes_orphans_only_after_the_upload_link_expired(media):
    from platform_media.sweeper import sweep

    c, db, ws, _other, store, _ = media
    # Ready file with an old intent expiry: never swept.
    ready, ready_key = _intent(c, "ready")
    _put(store, ready_key)
    assert c.post(f"/v1/uploads/{ready}/complete", headers=AUTH).status_code == 200
    _expire(db, ws, ready)
    # Expired, never completed, bytes were uploaded: orphan.
    abandoned, abandoned_key = _intent(c, "abandoned")
    _put(store, abandoned_key)
    _expire(db, ws, abandoned)
    # Deleted, then a late PUT recreated the bytes before the link expired: orphan.
    late, late_key = _intent(c, "late")
    assert c.delete(f"/v1/files/{late}", headers=AUTH).status_code == 204
    _put(store, late_key)
    _expire(db, ws, late)
    # Link still valid (or within the clock-skew grace): left alone for now.
    open_pending, open_pending_key = _intent(c, "open-pending")
    _put(store, open_pending_key)
    open_deleted, open_deleted_key = _intent(c, "open-deleted")
    assert c.delete(f"/v1/files/{open_deleted}", headers=AUTH).status_code == 204
    _put(store, open_deleted_key)
    grace, grace_key = _intent(c, "grace")
    _put(store, grace_key)
    _expire(db, ws, grace, -60)
    store.deleted.clear()

    result = sweep(db, store, ws)

    assert (result.swept, result.failed, result.skipped, result.more) == (2, 0, 0, False)
    assert sorted(store.deleted) == sorted([abandoned_key, late_key])
    for key in (ready_key, open_pending_key, open_deleted_key, grace_key):
        assert key in store.objects
    assert _row(db, ws, abandoned)["swept"] and _row(db, ws, late)["swept"]
    assert _row(db, ws, abandoned)["status"] == "pending"
    assert _row(db, ws, late)["status"] == "deleted"
    for file_id in (ready, open_pending, open_deleted, grace):
        assert not _row(db, ws, file_id)["swept"]
    # A swept row is not retried forever.
    store.deleted.clear()
    again = sweep(db, store, ws)
    assert (again.swept, again.failed, again.skipped, again.more) == (0, 0, 0, False)
    assert store.deleted == []
    assert c.get(f"/v1/files/{ready}", headers=AUTH).json()["status"] == "ready"
    assert c.get(f"/v1/files/{late}", headers=AUTH).status_code == 404
    assert c.post(f"/v1/uploads/{abandoned}/complete", headers=AUTH).status_code == 410


def test_sweep_treats_a_missing_object_as_already_removed(media):
    from platform_media.sweeper import sweep

    c, db, ws, _other, store, _ = media
    file_id, key = _intent(c, "never-uploaded")
    _expire(db, ws, file_id)
    result = sweep(db, store, ws)
    assert (result.swept, result.failed) == (1, 0)
    assert store.deleted == [key]
    assert _row(db, ws, file_id)["swept"]


def test_store_errors_leave_rows_for_the_next_run_with_redacted_logs(media, caplog):
    from platform_media.sweeper import sweep

    c, db, ws, _other, store, _ = media
    good, good_key = _intent(c, "good")
    bad, bad_key = _intent(c, "bad")
    for file_id in (good, bad):
        _expire(db, ws, file_id)
    store.fail.add(bad_key)
    with caplog.at_level(logging.DEBUG):
        result = sweep(db, store, ws)
    assert (result.swept, result.failed, result.skipped) == (1, 1, 0)
    assert _row(db, ws, good)["swept"] and not _row(db, ws, bad)["swept"]
    logged = caplog.text
    for secret in (str(ws), good_key, bad_key, good, bad, "synthetic provider failure"):
        assert secret not in logged
    store.fail.clear()
    retry = sweep(db, store, ws)
    assert (retry.swept, retry.failed) == (1, 0)
    assert _row(db, ws, bad)["swept"]


def test_sweep_is_bounded_and_scoped_to_the_configured_workspace(media):
    from platform_media.sweeper import sweep

    c, db, ws, other, store, _ = media
    mine = [_intent(c, f"mine-{i}") for i in range(5)]
    theirs = [_intent(c, f"theirs-{i}", owner="other") for i in range(2)]
    for file_id, _key in mine:
        _expire(db, ws, file_id)
    for file_id, _key in theirs:
        _expire(db, other, file_id)
    first = sweep(db, store, ws, batch_size=2)
    assert (first.swept, first.more) == (2, True)
    second = sweep(db, store, ws, batch_size=2)
    third = sweep(db, store, ws, batch_size=2)
    assert (second.swept, second.more, third.swept, third.more) == (2, True, 1, False)
    assert sorted(store.deleted) == sorted(k for _i, k in mine)
    for file_id, _key in theirs:
        assert not _row(db, other, file_id)["swept"]
    with pytest.raises(ValueError):
        sweep(db, store, ws, batch_size=0)
    with pytest.raises(ValueError):
        sweep(db, store, ws, batch_size=1001)


def test_sweep_finishes_a_stuck_deletion(media):
    from platform_media.sweeper import sweep

    c, db, ws, _other, store, _ = media
    file_id, key = _intent(c, "stuck")
    _put(store, key)
    store.fail.add(key)
    assert c.delete(f"/v1/files/{file_id}", headers=AUTH).status_code == 503
    assert _row(db, ws, file_id)["status"] == "deleting"
    store.fail.clear()
    _expire(db, ws, file_id)
    result = sweep(db, store, ws)
    assert (result.swept, result.failed) == (1, 0)
    row = _row(db, ws, file_id)
    assert (row["status"], row["swept"], row["has_deleted_at"]) == ("deleted", True, True)
    assert key not in store.objects
    assert c.get(f"/v1/files/{file_id}", headers=AUTH).status_code == 404


def test_sweep_holds_no_row_lock_during_delete_and_skips_rows_that_changed(media):
    from platform_media.sweeper import sweep

    c, db, ws, _other, store, _ = media
    file_id, key = _intent(c, "changed")
    _expire(db, ws, file_id)
    observed = []

    def concurrent_delete(_key):
        try:
            with db.transaction(ws) as conn:
                conn.execute("SET LOCAL lock_timeout = '2s'")
                conn.execute(
                    "SELECT id FROM media_files WHERE id=%s FOR UPDATE NOWAIT", (file_id,)
                )
                conn.execute(
                    "UPDATE media_files SET status='deleted',deleted_at=now() WHERE id=%s",
                    (file_id,),
                )
            observed.append("unlocked")
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion below
            observed.append(type(exc).__name__)

    store.on_delete = concurrent_delete
    result = sweep(db, store, ws)
    assert observed == ["unlocked"]
    assert (result.swept, result.failed, result.skipped) == (0, 0, 1)
    assert not _row(db, ws, file_id)["swept"]
    store.on_delete = None
    retry = sweep(db, store, ws)
    assert (retry.swept, retry.skipped) == (1, 0)
    assert store.deleted == [key, key]


def _cli(monkeypatch, capsys, env, store):
    from platform_media import __main__ as cli
    from platform_media import config

    for name in ("DATABASE_URL", "MEDIA_WORKSPACE_ID", "MEDIA_SWEEP_BATCH"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(config, "configured_store", lambda: store)
    monkeypatch.setattr(sys, "argv", ["platform_media", "sweep"])
    try:
        cli.main()
        code = 0
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_sweep_command_configuration_output_and_exit_codes(media, monkeypatch, capsys):
    c, db, ws, _other, store, isolated = media
    base = {"DATABASE_URL": isolated.runtime_dsn, "MEDIA_WORKSPACE_ID": str(ws)}
    # 2: usage/configuration errors, nothing touched.
    for env in (
        {"DATABASE_URL": isolated.runtime_dsn},
        {**base, "MEDIA_WORKSPACE_ID": "not-a-uuid"},
        {"MEDIA_WORKSPACE_ID": str(ws)},
        {**base, "MEDIA_SWEEP_BATCH": "0"},
        {**base, "MEDIA_SWEEP_BATCH": "1001"},
        {**base, "MEDIA_SWEEP_BATCH": "ten"},
    ):
        code, out = _cli(monkeypatch, capsys, env, store)
        assert code == 2, (env, out)
        assert isolated.runtime_dsn not in out
    code, out = _cli(monkeypatch, capsys, base, None)
    assert code == 2 and "MEDIA_S3" in out
    # 0: success, counts only.
    file_id, key = _intent(c, "cli")
    _expire(db, ws, file_id)
    code, out = _cli(monkeypatch, capsys, base, store)
    assert code == 0, out
    assert out.strip() == "swept=1 failed=0 skipped=0 more=no"
    assert str(ws) not in out and key not in out
    # 3: completed with store failures; the row stays for the next run.
    failing, failing_key = _intent(c, "cli-fail")
    _expire(db, ws, failing)
    store.fail.add(failing_key)
    code, out = _cli(monkeypatch, capsys, base, store)
    assert code == 3, out
    assert out.strip().splitlines()[0] == "swept=0 failed=1 skipped=0 more=no"
    assert failing_key not in out
    # 1: database unavailable, reported without the DSN.
    bad_dsn = "host=127.0.0.1 port=1 dbname=unreachable user=nobody connect_timeout=1"
    code, out = _cli(monkeypatch, capsys, {**base, "DATABASE_URL": bad_dsn}, store)
    assert code == 1, out
    assert "unreachable" not in out and "127.0.0.1" not in out


def test_sweep_needs_no_destructive_table_grant(media):
    import psycopg
    from psycopg import sql

    from platform_media.app import READINESS_PERMISSIONS
    from platform_media.sweeper import SWEEP_PERMISSIONS, sweep

    c, db, ws, _other, store, isolated = media
    for table, privileges in SWEEP_PERMISSIONS.items():
        assert set(privileges) <= set(READINESS_PERMISSIONS[table])
    with psycopg.connect(isolated.admin_dsn) as conn:
        conn.execute(
            sql.SQL("REVOKE ALL ON media_files FROM {}").format(
                sql.Identifier(isolated.runtime_role)
            )
        )
        conn.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE ON media_files TO {}").format(
                sql.Identifier(isolated.runtime_role)
            )
        )
    file_id, _key = _intent(c, "least-grant")
    _expire(db, ws, file_id)
    assert sweep(db, store, ws).swept == 1


def test_readiness_requires_the_sweep_migration(media):
    import psycopg

    c, *_rest, isolated = media
    assert c.get("/readyz").status_code == 200
    with psycopg.connect(isolated.admin_dsn) as conn:
        conn.execute(
            "DELETE FROM platform_schema_migrations WHERE namespace='platform_media' AND version='002-media-sweep'"
        )
    assert c.get("/readyz").status_code == 503


def test_s3_adapter_delete_is_idempotent_and_surfaces_other_errors():
    from botocore.stub import Stubber

    from platform_media.adapters import S3ObjectStore

    store = S3ObjectStore(
        endpoint="https://objects.example.invalid",
        bucket="synthetic",
        access_key="synthetic-access",
        secret_key="synthetic-secret",
    )
    key = f"{UUID(int=1)}/{UUID(int=2)}"
    params = {"Bucket": "synthetic", "Key": key}
    with Stubber(store.client) as stub:
        stub.add_response("delete_object", {}, params)
        assert store.delete(key) is None
        stub.add_client_error(
            "delete_object",
            service_error_code="NoSuchKey",
            http_status_code=404,
            expected_params=params,
        )
        assert store.delete(key) is None
        stub.add_client_error(
            "delete_object",
            service_error_code="InternalError",
            http_status_code=500,
            expected_params=params,
        )
        with pytest.raises(Exception, match="InternalError"):
            store.delete(key)
        stub.assert_no_pending_responses()
