import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def runtime(monkeypatch):
    from platform_backend_core import Database, MigrationRunner, Principal

    from platform_media import MIGRATIONS, create_app

    dsn = os.environ.get("PLATFORM_TEST_DSN")
    if not dsn:
        pytest.skip("PLATFORM_TEST_DSN required: disposable fixture admin PostgreSQL DSN")
    from platform_backend_core.testing import temporary_database
    with temporary_database(dsn, prefix="media_test_") as isolated:
        MigrationRunner(isolated.admin_dsn, namespace="platform_media").apply(MIGRATIONS)
        isolated.grant_schema()
        monkeypatch.setenv("PLATFORM_TEST_DSN", isolated.runtime_dsn)
        db = Database(isolated.runtime_dsn, max_size=8)
        workspace = uuid4()

        class Verifier:
            def verify(self, authorization):
                from fastapi import HTTPException

                if not authorization:
                    raise HTTPException(401, "authentication required")
                return Principal(
                    uuid4(),
                    workspace if authorization == "Bearer own" else other,
                    frozenset(["files:read", "files:write"]),
                )

        other = uuid4()
        adapter = FakeStore()
        app = create_app(db, Verifier(), object_store=adapter)
        with TestClient(app) as client:
            yield client, db, workspace, adapter


OWN = {"Authorization": "Bearer own", "Idempotency-Key": "fixture-key"}
OTHER = {"Authorization": "Bearer other", "Idempotency-Key": "fixture-key"}


class FakeStore:
    def __init__(self):
        self.objects = {}
        self.deleted = []

    def presign_put(self, key, content_type, size, checksum, expires):
        return {
            "url": "http://127.0.0.1/upload/" + key,
            "headers": {
                "Content-Type": content_type,
                "x-amz-checksum-sha256": checksum,
            },
        }

    def head(self, key):
        found = self.objects.get(key)
        return None if found is None else {k: v for k, v in found.items() if k != "prefix"}

    def read_prefix(self, key, length):
        found = self.objects.get(key)
        return None if found is None else found.get("prefix", b"")[:length]

    def presign_get(self, key, expires):
        return "http://127.0.0.1/download/" + key

    def delete(self, key):
        self.objects.pop(key, None)
        self.deleted.append(key)


BODY = {
    "filename": "sample.png",
    "content_type": "image/png",
    "size": 42,
    "checksum_sha256": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
}
PNG_PREFIX = bytes.fromhex("89504e470d0a1a0a0000000d49484452")
GATE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures/gates/media-content-type.json").read_text()
)


def test_upload_intent_is_bound_idempotent_and_workspace_isolated(runtime):
    c, _db, ws, store = runtime
    first = c.post("/v1/uploads", json=BODY, headers=OWN)
    assert first.status_code == 201, first.text
    value = first.json()
    file_id = value["id"]
    assert value["upload"]["url"].startswith("http://127.0.0.1/upload/" + str(ws) + "/")
    assert c.post("/v1/uploads", json=BODY, headers=OWN).json()["id"] == file_id
    assert (
        c.post("/v1/uploads", json={**BODY, "size": 43}, headers=OWN).status_code == 409
    )
    assert c.post(f"/v1/uploads/{file_id}/complete", headers=OTHER).status_code == 404
    assert c.get(f"/v1/files/{file_id}", headers=OTHER).status_code == 404
    assert c.get("/v1/files", headers=OTHER).json() == []
    assert c.get(f"/v1/files/{file_id}/download", headers=OWN).status_code == 409
    assert c.post(f"/v1/uploads/{file_id}/complete", headers=OWN).status_code == 409
    key = value["upload"]["url"].split("/upload/", 1)[1]
    store.objects[key] = {
        "size": 43,
        "content_type": BODY["content_type"],
        "checksum_sha256": BODY["checksum_sha256"],
        "prefix": PNG_PREFIX,
    }
    assert c.post(f"/v1/uploads/{file_id}/complete", headers=OWN).status_code == 409
    store.objects[key]["size"] = 42
    store.objects[key]["content_type"] = "image/jpeg"
    assert c.post(f"/v1/uploads/{file_id}/complete", headers=OWN).status_code == 409
    store.objects[key]["content_type"] = BODY["content_type"]
    store.objects[key]["checksum_sha256"] = "bad"
    assert c.post(f"/v1/uploads/{file_id}/complete", headers=OWN).status_code == 409
    store.objects[key]["checksum_sha256"] = BODY["checksum_sha256"]
    assert (
        c.post(f"/v1/uploads/{file_id}/complete", headers=OWN).json()["status"]
        == "ready"
    )
    assert c.get(f"/v1/files/{file_id}/download", headers=OWN).status_code == 200
    assert c.delete(f"/v1/files/{file_id}", headers=OTHER).status_code == 404
    assert c.delete(f"/v1/files/{file_id}", headers=OWN).status_code == 204
    assert c.get(f"/v1/files/{file_id}/download", headers=OWN).status_code == 404
    assert store.deleted == [key]


def test_expiry_limits_and_no_caller_authority(runtime):
    c, db, ws, _store = runtime
    for body in [
        {**BODY, "key": "other/object"},
        {**BODY, "endpoint": "http://evil"},
        {**BODY, "size": 0},
        {**BODY, "size": 100000001},
        {**BODY, "content_type": "text/html"},
        {**BODY, "checksum_sha256": "bad"},
        {**BODY, "expires_in": 3601},
    ]:
        assert c.post("/v1/uploads", json=body, headers=OWN).status_code == 422
    assert c.get("/v1/files?limit=101", headers=OWN).status_code == 422
    assert c.post("/v1/uploads", json=BODY).status_code == 401
    item = c.post("/v1/uploads", json=BODY, headers=OWN).json()
    with db.transaction(ws) as conn:
        conn.execute(
            "UPDATE media_files SET expires_at = now()-interval '1 second' WHERE id=%s",
            (item["id"],),
        )
    assert c.post(f"/v1/uploads/{item['id']}/complete", headers=OWN).status_code == 410


def test_concurrent_uploads_and_force_rls(runtime):
    from concurrent.futures import ThreadPoolExecutor

    c, db, ws, _store = runtime
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(lambda _: c.post("/v1/uploads", json=BODY, headers=OWN), range(12))
        )
    assert all(r.status_code in (200, 201) for r in results)
    assert len({r.json()["id"] for r in results}) == 1
    with db.transaction(uuid4()) as conn:
        assert conn.execute("SELECT count(*) FROM media_files").fetchone()[0] == 0
    with db.transaction(ws) as conn:
        assert conn.execute("SELECT count(*) FROM media_files").fetchone()[0] == 1
        assert not conn.execute(
            "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user"
        ).fetchone()[0]
        assert conn.execute(
            "SELECT relforcerowsecurity FROM pg_class WHERE oid='media_files'::regclass"
        ).fetchone()[0]


def test_missing_store_fails_closed():
    from platform_backend_core import Principal

    from platform_media import create_app

    class V:
        def verify(self, auth):
            return Principal(uuid4(), uuid4(), frozenset(["files:write"]))

    with TestClient(create_app(DisabledDatabase(), V())) as c:
        assert c.post("/v1/uploads", json=BODY, headers=OWN).status_code == 503


class DisabledDatabase:
    def close(self):
        pass

    def ready(self):
        return True


def test_readiness_requires_provider_and_migrated_rls_schema():
    from platform_media import create_app

    with TestClient(create_app(DisabledDatabase(), object())) as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/readyz").status_code == 503


def test_actual_s3_adapter_binds_conditional_put_length_type_checksum_and_expiry():
    from urllib.parse import parse_qs, urlsplit

    from botocore.stub import Stubber

    from platform_media.adapters import S3ObjectStore

    store = S3ObjectStore(
        endpoint="https://objects.example.invalid",
        bucket="synthetic",
        access_key="synthetic-access",
        secret_key="synthetic-secret",
    )
    key = str(uuid4()) + "/" + str(uuid4())
    value = store.presign_put(key, "image/png", 42, BODY["checksum_sha256"], 120)
    query = parse_qs(urlsplit(value["url"]).query)
    assert query["X-Amz-Expires"] == ["120"]
    signed = query["X-Amz-SignedHeaders"][0]
    for header in (
        "content-length",
        "content-type",
        "if-none-match",
        "x-amz-checksum-sha256",
    ):
        assert header in signed
    assert value["headers"]["If-None-Match"] == "*"
    with Stubber(store.client) as stub:
        stub.add_response(
            "head_object",
            {
                "ContentLength": 42,
                "ContentType": "image/png",
                "ChecksumSHA256": BODY["checksum_sha256"],
            },
            {"Bucket": "synthetic", "Key": key, "ChecksumMode": "ENABLED"},
        )
        assert store.head(key) == {
            "size": 42,
            "content_type": "image/png",
            "checksum_sha256": BODY["checksum_sha256"],
        }
        stub.add_client_error(
            "head_object",
            service_error_code="404",
            http_status_code=404,
            expected_params={
                "Bucket": "synthetic",
                "Key": key,
                "ChecksumMode": "ENABLED",
            },
        )
        assert store.head(key) is None
    with pytest.raises(ValueError):
        S3ObjectStore(
            endpoint="http://objects.example.invalid",
            bucket="synthetic",
            access_key="fixture",
            secret_key="fixture",
        )


def test_authorization_is_from_real_verified_jwt_and_concrete_scope(runtime):
    import time

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from platform_backend_core import TokenVerifier

    c, _db, ws, _store = runtime
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    c.app.state.verifier = TokenVerifier(
        issuer="https://identity.example.invalid",
        audience="media-test",
        public_key=key.public_key(),
    )
    now = int(time.time())
    claims = {
        "sub": str(uuid4()),
        "ws": str(ws),
        "iat": now,
        "exp": now + 300,
        "jti": str(uuid4()),
        "iss": "https://identity.example.invalid",
        "aud": "media-test",
        "scope": "files:read",
    }
    token = jwt.encode(claims, key, algorithm="RS256")
    assert (
        c.post(
            "/v1/uploads",
            json=BODY,
            headers={**OWN, "Authorization": "Bearer " + token},
        ).status_code
        == 403
    )
    assert (
        c.get("/v1/files", headers={"Authorization": "Bearer " + token}).status_code
        == 200
    )
    assert (
        c.get("/v1/files", headers={"Authorization": "Bearer forged"}).status_code
        == 401
    )
    assert c.get("/readyz").status_code == 200


def test_two_independent_api_pools_share_intent_and_rls(runtime, caplog):
    from concurrent.futures import ThreadPoolExecutor

    from platform_backend_core import Database

    from platform_media import create_app

    c, _db, _ws, store = runtime
    second = Database(os.environ["PLATFORM_TEST_DSN"], max_size=4)
    with TestClient(
        create_app(second, c.app.state.verifier, object_store=store)
    ) as replica:
        caplog.set_level("INFO", logger="platform_backend_core.http")
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(
                pool.map(
                    lambda n: (c if n % 2 else replica).post(
                        "/v1/uploads", json=BODY, headers=OWN
                    ),
                    range(12),
                )
            )
        assert all(r.status_code in (200, 201) for r in results)
        identifier = results[0].json()["id"]
        assert {r.json()["id"] for r in results} == {identifier}
        assert replica.get(f"/v1/files/{identifier}", headers=OWN).status_code == 200
        assert replica.get(f"/v1/files/{identifier}", headers=OTHER).status_code == 404
        assert "/upload/" not in caplog.text
        assert "Bearer own" not in caplog.text
        assert replica.get("/readyz").status_code == 200


def test_pending_upload_queue_is_bounded_per_workspace_and_replay_still_works(runtime):
    c, db, ws, _store = runtime
    first = c.post("/v1/uploads", json=BODY, headers=OWN).json()
    with db.transaction(ws) as conn:
        conn.execute(
            """INSERT INTO media_files(id,workspace_id,actor_id,idempotency_key,fingerprint,object_key,filename,content_type,size,checksum_sha256,status,expires_at)
        SELECT gen_random_uuid(),%s,%s,'bulk-'||n,'fixture','bulk/'||%s||'/'||n,'bulk.png','image/png',42,%s,'pending',now()+interval '600 seconds' FROM generate_series(1,999) n""",
            (ws, uuid4(), str(ws), BODY["checksum_sha256"]),
        )
    assert (
        c.post(
            "/v1/uploads", json=BODY, headers={**OWN, "Idempotency-Key": "over-quota"}
        ).status_code
        == 429
    )
    assert c.post("/v1/uploads", json=BODY, headers=OWN).json()["id"] == first["id"]
    assert c.post("/v1/uploads", json=BODY, headers=OTHER).status_code == 201


def _stage(c, store, content_type, idempotency_key, prefix):
    body = {**BODY, "content_type": content_type}
    created = c.post(
        "/v1/uploads", json=body, headers={**OWN, "Idempotency-Key": idempotency_key}
    )
    assert created.status_code == 201, created.text
    key = created.json()["upload"]["url"].split("/upload/", 1)[1]
    store.objects[key] = {
        "size": body["size"],
        "content_type": content_type,
        "checksum_sha256": body["checksum_sha256"],
        "prefix": prefix,
    }
    return created.json()["id"], key


def test_content_type_gate_fixture_covers_every_allowed_type():
    from platform_media.app import ALLOWED_CONTENT_TYPES, SIGNATURE_BYTES

    assert GATE["prefix_bytes"] == SIGNATURE_BYTES
    assert {case["content_type"] for case in GATE["cases"]} == set(ALLOWED_CONTENT_TYPES)
    assert {case["accept"] for case in GATE["cases"]} == {True, False}


def test_completion_requires_stored_bytes_to_match_declared_content_type(runtime):
    c, _db, _ws, store = runtime
    for case in GATE["cases"]:
        file_id, _key = _stage(
            c, store, case["content_type"], "gate-" + case["id"], bytes.fromhex(case["prefix_hex"])
        )
        done = c.post(f"/v1/uploads/{file_id}/complete", headers=OWN)
        status = c.get(f"/v1/files/{file_id}", headers=OWN).json()["status"]
        if case["accept"]:
            assert done.status_code == 200, case["id"]
            assert status == "ready", case["id"]
        else:
            assert done.status_code == 409, case["id"]
            assert done.json()["detail"] == "stored object content does not match content type"
            assert status == "pending", case["id"]
            assert c.get(f"/v1/files/{file_id}/download", headers=OWN).status_code == 409


def test_content_check_fails_closed_on_missing_or_unreadable_bytes(runtime):
    c, _db, _ws, store = runtime
    file_id, _key = _stage(c, store, "image/png", "gate-unreadable", PNG_PREFIX)
    original = store.read_prefix

    def unavailable(key, length):
        raise OSError("provider down")

    store.read_prefix = unavailable
    assert c.post(f"/v1/uploads/{file_id}/complete", headers=OWN).status_code == 503
    store.read_prefix = lambda key, length: None
    assert c.post(f"/v1/uploads/{file_id}/complete", headers=OWN).status_code == 409
    store.read_prefix = original
    assert c.get(f"/v1/files/{file_id}", headers=OWN).json()["status"] == "pending"
    assert c.post(f"/v1/uploads/{file_id}/complete", headers=OWN).json()["status"] == "ready"


def test_s3_adapter_reads_only_a_bounded_range_for_the_content_check():
    import io

    from botocore.response import StreamingBody
    from botocore.stub import Stubber

    from platform_media.adapters import S3ObjectStore

    store = S3ObjectStore(
        endpoint="https://objects.example.invalid",
        bucket="synthetic",
        access_key="synthetic-access",
        secret_key="synthetic-secret",
    )
    key = str(uuid4()) + "/" + str(uuid4())
    with Stubber(store.client) as stub:
        stub.add_response(
            "get_object",
            {"Body": StreamingBody(io.BytesIO(PNG_PREFIX), len(PNG_PREFIX))},
            {"Bucket": "synthetic", "Key": key, "Range": "bytes=0-15"},
        )
        assert store.read_prefix(key, 16) == PNG_PREFIX
        stub.add_client_error(
            "get_object",
            service_error_code="NoSuchKey",
            http_status_code=404,
            expected_params={"Bucket": "synthetic", "Key": key, "Range": "bytes=0-15"},
        )
        assert store.read_prefix(key, 16) is None
