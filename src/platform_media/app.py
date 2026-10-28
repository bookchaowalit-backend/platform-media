import base64
import hashlib
import json
import logging
from contextlib import contextmanager
from uuid import UUID, uuid4

from fastapi import Depends, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from platform_backend_core import create_app as core_app
from platform_backend_core import require_scope
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .adapters import ObjectStore

LOG = logging.getLogger("platform_media")

# Effective privileges used by runtime SQL; append-only effects need INSERT only.
READINESS_PERMISSIONS = {
    "media_files": ("SELECT", "INSERT", "UPDATE"),
}

# Content-type gate (fixtures/gates/media-content-type.json): completion reads only
# this many leading bytes and requires them to match the declared type.
SIGNATURE_BYTES = 16


def _png(prefix):
    return prefix.startswith(b"\x89PNG\r\n\x1a\n")


def _jpeg(prefix):
    return prefix.startswith(b"\xff\xd8\xff")


def _webp(prefix):
    return prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP"


def _pdf(prefix):
    return prefix.startswith(b"%PDF-")


def _text(prefix):
    return b"\x00" not in prefix


def _any(prefix):
    return True


ALLOWED_CONTENT_TYPES = {
    "image/png": _png,
    "image/jpeg": _jpeg,
    "image/webp": _webp,
    "application/pdf": _pdf,
    "text/plain": _text,
    "application/octet-stream": _any,
}

class Upload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    filename: str = Field(min_length=1, max_length=255, pattern=r"^[^\x00-\x1f/\\]+$")
    content_type: str
    size: int = Field(ge=1, le=100000000)
    checksum_sha256: str
    expires_in: int = Field(default=900, ge=60, le=3600)

    @field_validator("content_type")
    @classmethod
    def safe_type(cls, value):
        if value not in ALLOWED_CONTENT_TYPES:
            raise ValueError("unsupported content type")
        return value

    @field_validator("checksum_sha256")
    @classmethod
    def checksum(cls, value):
        try:
            if len(base64.b64decode(value, validate=True)) != 32:
                raise ValueError()
        except ValueError:  # binascii.Error is a ValueError
            raise ValueError("SHA256 must be 32 bytes encoded as base64") from None
        return value


def public(row):
    return {
        key: row[key]
        for key in (
            "id",
            "filename",
            "content_type",
            "size",
            "checksum_sha256",
            "status",
            "created_at",
            "expires_at",
        )
    }


@contextmanager
def transaction(database, workspace):
    with database.transaction(workspace) as conn:
        original = conn.row_factory
        conn.row_factory = dict_row
        try:
            yield conn
        finally:
            conn.row_factory = original


def create_app(database, verifier, object_store: ObjectStore | None = None):
    if object_store is None:
        from .config import configured_store

        object_store = configured_store()
    app = core_app("platform-media", database, verifier)
    app.state.object_store = object_store

    app.router.routes[:] = [
        r for r in app.router.routes if getattr(r, "path", None) != "/readyz"
    ]

    @app.get("/readyz", include_in_schema=False)
    def readiness():
        try:
            if object_store is None:
                raise ValueError("provider unavailable")
            from .migrations import MIGRATIONS

            with transaction(database, UUID(int=0)) as conn:
                allowed = conn.execute(
                    "SELECT has_schema_privilege(current_user,'public','USAGE') AS allowed"
                ).fetchone()["allowed"]
                if not allowed:
                    raise ValueError("schema privilege unavailable")
                permissions = {"platform_schema_migrations": ("SELECT",), **READINESS_PERMISSIONS}
                for table, privileges in permissions.items():
                    for privilege in privileges:
                        allowed = conn.execute(
                            "SELECT has_table_privilege(current_user,%s,%s) AS allowed",
                            ("public." + table, privilege),
                        ).fetchone()["allowed"]
                        if not allowed:
                            raise ValueError("runtime privilege unavailable")
                history = conn.execute(
                    "SELECT version,checksum FROM platform_schema_migrations WHERE namespace=%s",
                    ("platform_media",),
                ).fetchall()
                if {r["version"]: r["checksum"] for r in history} != {
                    m.version: m.checksum for m in MIGRATIONS
                }:
                    raise ValueError("migration mismatch")
                rows = conn.execute(
                    "SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=ANY(%s::regclass[])",
                    (["media_files"],),
                ).fetchall()
                if len(rows) != 1 or not all(
                    r["relrowsecurity"] and r["relforcerowsecurity"] for r in rows
                ):
                    raise ValueError("RLS unavailable")
            return {"status": "ready"}
        except Exception:  # noqa: BLE001 - any probe failure is reported as unavailable without detail
            return JSONResponse({"status": "unavailable"}, status_code=503)

    def configured():
        if object_store is None:
            raise HTTPException(503, "object store not configured")
        return object_store

    def fetch(conn, identifier, lock=False):
        row = conn.execute(
            "SELECT * FROM media_files WHERE id=%s" + (" FOR UPDATE" if lock else ""),
            (identifier,),
        ).fetchone()
        if row is None or row["status"] == "deleted":
            raise HTTPException(404, "file not found")
        return row

    @app.post("/v1/uploads")
    def upload(
        body: Upload,
        response: Response,
        principal=Depends(require_scope("files:write")),
        key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
        store=Depends(configured),
    ):
        fingerprint = hashlib.sha256(
            json.dumps(
                body.model_dump(), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        identifier = uuid4()
        with transaction(database, principal.workspace) as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                ("media.uploads:" + str(principal.workspace),),
            )
            existing = conn.execute(
                "SELECT id FROM media_files WHERE workspace_id=%s AND idempotency_key=%s",
                (principal.workspace, key),
            ).fetchone()
            if existing is None:
                pending = conn.execute(
                    "SELECT count(*) AS n FROM media_files WHERE workspace_id=%s AND status='pending' AND expires_at>now()",
                    (principal.workspace,),
                ).fetchone()["n"]
                if pending >= 1000:
                    raise HTTPException(429, "pending upload quota reached")
            conn.execute(
                "INSERT INTO media_files(id,workspace_id,actor_id,idempotency_key,fingerprint,object_key,filename,content_type,size,checksum_sha256,status,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',now() + %s * interval '1 second') ON CONFLICT(workspace_id,idempotency_key) DO NOTHING",
                (
                    identifier,
                    principal.workspace,
                    principal.actor,
                    key,
                    fingerprint,
                    f"{principal.workspace}/{identifier}",
                    body.filename,
                    body.content_type,
                    body.size,
                    body.checksum_sha256,
                    body.expires_in,
                ),
            )
            row = conn.execute(
                "SELECT *, greatest(0,extract(epoch from expires_at-now()))::int AS remaining FROM media_files WHERE workspace_id=%s AND idempotency_key=%s",
                (principal.workspace, key),
            ).fetchone()
            if row["fingerprint"] != fingerprint:
                raise HTTPException(409, "idempotency key payload conflict")
            response.status_code = 201 if row["id"] == identifier else 200
            value = public(row)
            if row["status"] == "pending":
                if row["remaining"] <= 0:
                    raise HTTPException(410, "upload intent expired")
                try:
                    value["upload"] = store.presign_put(
                        row["object_key"],
                        row["content_type"],
                        row["size"],
                        row["checksum_sha256"],
                        min(row["remaining"], 3600),
                    )
                except Exception:  # noqa: BLE001 - provider errors are redacted to 503
                    raise HTTPException(503, "object store unavailable") from None
            return value

    def purge_late_object(store, key):
        # A presigned PUT cannot be revoked and stays valid until the intent's
        # expires_at (at most 3600 s), so it can recreate bytes after DELETE.
        # Callers only invoke this while that window is open; best effort here,
        # a replayed DELETE retries and reports 503 on failure.
        try:
            store.delete(key)
        except Exception:  # noqa: BLE001 - best effort; a replayed DELETE retries
            LOG.warning("late-object purge failed; DELETE replay will retry")

    @app.post("/v1/uploads/{identifier}/complete")
    def complete(
        identifier: UUID,
        principal=Depends(require_scope("files:write")),
        store=Depends(configured),
    ):
        # Phase 1: read the intent and release the transaction before any network
        # call, so a slow store cannot hold a row lock or idle in transaction.
        with transaction(database, principal.workspace) as conn:
            row = conn.execute(
                "SELECT *, now() >= expires_at AS expired FROM media_files WHERE id=%s",
                (identifier,),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "file not found")
        if row["status"] == "deleted":
            if not row["expired"]:
                purge_late_object(store, row["object_key"])
            raise HTTPException(404, "file not found")
        if row["status"] == "ready":
            return public(row)
        if row["status"] != "pending":
            raise HTTPException(409, "file is being deleted")
        if row["expired"]:
            raise HTTPException(410, "upload intent expired")
        # Phase 2: verify the stored object without a transaction.
        try:
            actual = store.head(row["object_key"])
        except Exception:  # noqa: BLE001 - provider errors are redacted to 503
            raise HTTPException(503, "object store unavailable") from None
        expected = {
            "size": row["size"],
            "content_type": row["content_type"],
            "checksum_sha256": row["checksum_sha256"],
        }
        if (
            actual is None
            or not actual.get("etag")
            or any(actual.get(k) != v for k, v in expected.items())
        ):
            raise HTTPException(409, "stored object metadata mismatch")
        try:
            # Bound the content read to the version just verified by HEAD.
            prefix = store.read_prefix(
                row["object_key"], SIGNATURE_BYTES, if_match=actual["etag"]
            )
        except Exception:  # noqa: BLE001 - provider errors are redacted to 503
            raise HTTPException(503, "object store unavailable") from None
        if prefix is None or not ALLOWED_CONTENT_TYPES[row["content_type"]](prefix):
            raise HTTPException(409, "stored object content does not match content type")
        # Phase 3: re-lock and finalize only if the intent is unchanged and pending.
        bound = ("object_key", "size", "content_type", "checksum_sha256", "expires_at")
        with transaction(database, principal.workspace) as conn:
            current = conn.execute(
                "SELECT *, now() >= expires_at AS expired FROM media_files WHERE id=%s FOR UPDATE",
                (identifier,),
            ).fetchone()
            if current is None:
                raise HTTPException(404, "file not found")
            if current["status"] == "ready":
                return public(current)
            if current["status"] == "pending" and all(
                current[k] == row[k] for k in bound
            ):
                if current["expired"]:
                    raise HTTPException(410, "upload intent expired")
                return public(
                    conn.execute(
                        "UPDATE media_files SET status='ready',completed_at=now() WHERE id=%s AND status='pending' RETURNING *",
                        (identifier,),
                    ).fetchone()
                )
        if current["status"] == "deleted":
            if not current["expired"]:
                purge_late_object(store, current["object_key"])
            raise HTTPException(404, "file not found")
        raise HTTPException(409, "file is being deleted")

    @app.get("/v1/files")
    def files(
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=10000),
        principal=Depends(require_scope("files:read")),
    ):
        with transaction(database, principal.workspace) as conn:
            return [
                public(row)
                for row in conn.execute(
                    "SELECT * FROM media_files WHERE status <> 'deleted' ORDER BY created_at DESC,id LIMIT %s OFFSET %s",
                    (limit, offset),
                ).fetchall()
            ]

    @app.get("/v1/files/{identifier}")
    def metadata(identifier: UUID, principal=Depends(require_scope("files:read"))):
        with transaction(database, principal.workspace) as conn:
            return public(fetch(conn, identifier))

    @app.get("/v1/files/{identifier}/download")
    def download(
        identifier: UUID,
        principal=Depends(require_scope("files:read")),
        store=Depends(configured),
    ):
        with transaction(database, principal.workspace) as conn:
            row = fetch(conn, identifier)
            if row["status"] != "ready":
                raise HTTPException(409, "file is not ready")
            try:
                url = store.presign_get(row["object_key"], 300)
            except Exception:  # noqa: BLE001 - provider errors are redacted to 503
                raise HTTPException(503, "object store unavailable") from None
            return {"url": url, "expires_in": 300}

    @app.delete("/v1/files/{identifier}", status_code=204)
    def delete(
        identifier: UUID,
        principal=Depends(require_scope("files:write")),
        store=Depends(configured),
    ):
        with transaction(database, principal.workspace) as conn:
            row = conn.execute(
                "SELECT *, now() < expires_at AS upload_window_open FROM media_files WHERE id=%s FOR UPDATE",
                (identifier,),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "file not found")
            already_deleted = row["status"] == "deleted"
            if already_deleted and not row["upload_window_open"]:
                # No presigned PUT for this intent can still be valid: nothing to purge.
                return Response(status_code=204)
            if not already_deleted:
                conn.execute(
                    "UPDATE media_files SET status='deleting' WHERE id=%s", (identifier,)
                )
        try:
            # On a replay inside the upload window this removes bytes a late
            # presigned PUT recreated after the first deletion.
            store.delete(row["object_key"])
        except Exception:  # noqa: BLE001 - provider errors are redacted to 503
            raise HTTPException(503, "object deletion pending; retry safely") from None
        if already_deleted:
            return Response(status_code=204)
        with transaction(database, principal.workspace) as conn:
            conn.execute(
                "UPDATE media_files SET status='deleted',deleted_at=now() WHERE id=%s",
                (identifier,),
            )
        return Response(status_code=204)

    return app
