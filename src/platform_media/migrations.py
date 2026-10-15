from platform_backend_core import Migration

MIGRATIONS = [
    Migration(
        "001-media",
        """
CREATE TABLE media_files (
 id uuid PRIMARY KEY, workspace_id uuid NOT NULL, actor_id uuid NOT NULL,
 idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 128),
 fingerprint text NOT NULL, object_key text NOT NULL UNIQUE,
 filename text NOT NULL, content_type text NOT NULL,
 size bigint NOT NULL CHECK (size BETWEEN 1 AND 100000000), checksum_sha256 text NOT NULL,
 status text NOT NULL CHECK (status IN ('pending','ready','deleting','deleted')),
 created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
 completed_at timestamptz, deleted_at timestamptz,
 UNIQUE(workspace_id, idempotency_key)
);
CREATE INDEX media_workspace_created ON media_files(workspace_id, created_at DESC, id);
ALTER TABLE media_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE media_files FORCE ROW LEVEL SECURITY;
CREATE POLICY media_workspace ON media_files
 USING (workspace_id = nullif(current_setting('app.workspace_id', true),'')::uuid)
 WITH CHECK (workspace_id = nullif(current_setting('app.workspace_id', true),'')::uuid);
""",
    )
]
