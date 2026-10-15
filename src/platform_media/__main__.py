import argparse
import os
from pathlib import Path
from platform_backend_core import Database, MigrationRunner, TokenVerifier
from . import create_app, MIGRATIONS


def main():
    parser = argparse.ArgumentParser(
        description="Staged platform_media runtime; no automatic migrations"
    )
    parser.add_argument("command", choices=["serve", "migrate"])
    args = parser.parse_args()
    if args.command == "migrate":
        dsn = os.environ.get("DATABASE_MIGRATION_DSN")
        if not dsn:
            parser.error("DATABASE_MIGRATION_DSN required")
        try:
            applied = MigrationRunner(dsn, namespace="platform_media").apply(MIGRATIONS)
        except Exception:
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
    except Exception:
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


if __name__ == "__main__":
    main()
