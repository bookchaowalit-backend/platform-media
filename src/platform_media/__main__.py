import argparse
import os
from uuid import UUID

from platform_backend_core import Database, MigrationRunner, TokenVerifier

from . import MIGRATIONS, create_app


def main():
    parser = argparse.ArgumentParser(
        description="Staged platform_media runtime; no automatic migrations"
    )
    parser.add_argument("command", choices=["serve", "migrate", "sweep"])
    args = parser.parse_args()
    if args.command == "sweep":
        return sweep_command(parser)
    if args.command == "migrate":
        dsn = os.environ.get("DATABASE_MIGRATION_DSN")
        if not dsn:
            parser.error("DATABASE_MIGRATION_DSN required")
        try:
            applied = MigrationRunner(dsn, namespace="platform_media").apply(MIGRATIONS)
        except Exception:  # noqa: BLE001 - migration errors are reported without the DSN
            parser.exit(
                1, "migration failed; inspect database access and migration history\n"
            )
        print(f"applied {len(applied)} migrations")
        return
    dsn = os.environ.get("DATABASE_URL")
    issuer = os.environ.get("JWT_ISSUER")
    audience = os.environ.get("JWT_AUDIENCE")
    pem = os.environ.get("JWT_PUBLIC_KEY")
    jwks = os.environ.get("JWT_JWKS_URL")
    if not dsn or not issuer or not audience or not (pem or jwks):
        parser.error(
            "DATABASE_URL, JWT_ISSUER, JWT_AUDIENCE and JWT_PUBLIC_KEY or JWT_JWKS_URL required"
        )
    try:
        verifier = TokenVerifier(
            issuer=issuer, audience=audience, public_key=pem, jwks_url=jwks
        )
        database = Database(dsn, max_size=int(os.environ.get("DATABASE_POOL_MAX", "8")))
        app = create_app(database, verifier)
    except Exception:  # noqa: BLE001 - startup errors are reported without configuration values
        parser.exit(1, "invalid service configuration\n")
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        access_log=False,
        limit_concurrency=64,
        timeout_keep_alive=5,
    )


def sweep_command(parser):
    """One bounded orphan-object sweep of MEDIA_WORKSPACE_ID (docs/runtime.md).

    Exit codes: 0 done (run again while more=yes), 1 database unavailable or
    unexpected failure, 2 usage/configuration error, 3 done with store failures
    (those rows stay for the next run)."""
    from . import config
    from .sweeper import DEFAULT_BATCH, MAX_BATCH, sweep

    dsn = os.environ.get("DATABASE_URL")
    raw_workspace = os.environ.get("MEDIA_WORKSPACE_ID")
    if not dsn or not raw_workspace:
        parser.error("DATABASE_URL and MEDIA_WORKSPACE_ID required")
    try:
        workspace = UUID(raw_workspace)
    except ValueError:
        parser.error("MEDIA_WORKSPACE_ID must be a UUID")
    raw_batch = os.environ.get("MEDIA_SWEEP_BATCH", str(DEFAULT_BATCH))
    if not raw_batch.isdecimal() or not 1 <= int(raw_batch) <= MAX_BATCH:
        parser.error(f"MEDIA_SWEEP_BATCH must be 1..{MAX_BATCH}")
    try:
        store = config.configured_store()
    except Exception:  # noqa: BLE001 - configuration values are not echoed
        parser.error("invalid MEDIA_S3_* object-store configuration")
    if store is None:
        parser.error(
            "MEDIA_S3_ENDPOINT, MEDIA_S3_BUCKET, MEDIA_S3_ACCESS_KEY and MEDIA_S3_SECRET_KEY required"
        )
    try:
        database = Database(dsn, max_size=1)
        try:
            result = sweep(database, store, workspace, batch_size=int(raw_batch))
        finally:
            database.close()
    except Exception:  # noqa: BLE001 - reported without the DSN, keys or provider detail
        parser.exit(1, "sweep failed; inspect database access\n")
    print(
        f"swept={result.swept} failed={result.failed} skipped={result.skipped}"
        f" more={'yes' if result.more else 'no'}"
    )
    if result.failed:
        parser.exit(3, "some object deletions failed; rows are left for the next run\n")


if __name__ == "__main__":
    main()
