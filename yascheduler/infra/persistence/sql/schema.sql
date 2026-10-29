-- Bootstrap the yascheduler_migrations tracker.
-- Runs BEFORE CREATE TABLE IF NOT EXISTS yascheduler_nodes because the
-- presence of yascheduler_nodes is the signal distinguishing a fresh DB
-- (seed to latest) from a legacy DB (no seed, run all migrations).
-- last_migration is the single manual edit point when a migration is added.
DO $$
DECLARE
  last_migration CONSTANT TEXT := '013';
BEGIN
  IF to_regclass('yascheduler_migrations') IS NULL THEN
    EXECUTE 'CREATE TABLE yascheduler_migrations (
      migration_id TEXT PRIMARY KEY,
      created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )';
    IF to_regclass('yascheduler_nodes') IS NULL THEN
      INSERT INTO yascheduler_migrations (migration_id) VALUES (last_migration);
    END IF;
  END IF;
END $$;

-- node_status enum: created idempotently before the CREATE TABLE that
-- references it.
DO $$
BEGIN
    IF to_regtype('node_status') IS NULL THEN
        CREATE TYPE NODE_STATUS AS ENUM ('OTHER');
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS yascheduler_nodes (
    node_id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    enabled BOOLEAN DEFAULT TRUE,
    status NODE_STATUS NOT NULL DEFAULT 'OTHER',
    hostname VARCHAR(255),
    port INTEGER NOT NULL DEFAULT 22,
    username VARCHAR(255) DEFAULT 'root',
    jump_host VARCHAR(255),
    jump_port INTEGER NOT NULL DEFAULT 22,
    jump_username VARCHAR(255) NOT NULL DEFAULT 'root',
    ncpus SMALLINT DEFAULT NULL,
    cloud VARCHAR(32) DEFAULT NULL,
    external_id VARCHAR(255),
    CONSTRAINT node_port_range CHECK (port > 0 AND port < 65536),
    CONSTRAINT node_jump_port_range CHECK (jump_port > 0 AND jump_port < 65536),
    CONSTRAINT node_ncpus_positive CHECK (ncpus IS NULL OR ncpus > 0)
);

-- task_status enum: created idempotently (CREATE TYPE has no IF NOT EXISTS).
DO $$
BEGIN
    IF to_regtype('task_status') IS NULL THEN
        CREATE TYPE TASK_STATUS AS ENUM ('TO_DO', 'RUNNING', 'DONE');
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS yascheduler_tasks (
    task_id INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    title VARCHAR(256),
    engine VARCHAR(64) NOT NULL,
    status TASK_STATUS NOT NULL DEFAULT 'TO_DO',
    allocated_node_id INTEGER REFERENCES yascheduler_nodes (
        node_id
    ) ON DELETE SET NULL,
    local_folder VARCHAR(1024),
    remote_folder VARCHAR(1024),
    webhook_url VARCHAR(2048),
    webhook_custom_params JSONB NOT NULL DEFAULT '{}'::JSONB,
    error TEXT,
    extra JSONB NOT NULL DEFAULT '{}'::JSONB,
    CONSTRAINT task_status_field_invariants CHECK (
        (status = 'TO_DO' AND allocated_node_id IS NULL AND error IS NULL)
        OR (
            status = 'RUNNING'
            AND allocated_node_id IS NOT NULL
            AND error IS NULL
            AND remote_folder IS NOT NULL
        )
        OR (status = 'DONE')
    )
);

-- Task-owned input snapshot entries (ADR-0019): one row per captured file or
-- directory of a task's input snapshot. `path` is the canonical task-relative
-- path; `content` holds exact file bytes (an empty byte string is an empty
-- file, never a directory) and is NULL for directories. Rows are immutable
-- after insertion and cascade only with their owning task.
CREATE TABLE IF NOT EXISTS yascheduler_task_inputs (
    task_id INTEGER NOT NULL REFERENCES yascheduler_tasks (
        task_id
    ) ON DELETE CASCADE,
    path TEXT NOT NULL,
    kind TEXT NOT NULL,
    content BYTEA,
    CONSTRAINT task_input_pk PRIMARY KEY (task_id, path),
    CONSTRAINT task_input_kind_values CHECK (kind IN ('file', 'directory')),
    CONSTRAINT task_input_kind_content_invariants CHECK (
        (kind = 'file' AND content IS NOT NULL)
        OR (kind = 'directory' AND content IS NULL)
    )
);

-- Shared trigger function: sets NEW.updated_at = NOW() on every row update.
-- Called by yascheduler_tasks_touch_updated_at and
-- yascheduler_nodes_touch_updated_at.
CREATE OR REPLACE FUNCTION YASCHEDULER_TOUCH_UPDATED_AT() RETURNS TRIGGER
LANGUAGE plpgsql
AS $$ BEGIN NEW.updated_at = NOW(); RETURN NEW; END; $$;

-- Install yascheduler_tasks trigger if the updated_at column exists (safe for
-- legacy DBs where CREATE TABLE IF NOT EXISTS is a no-op on pre-existing
-- tables).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'yascheduler_tasks' AND column_name = 'updated_at'
    ) THEN
        DROP TRIGGER IF EXISTS yascheduler_tasks_touch_updated_at
        ON yascheduler_tasks;
        CREATE TRIGGER yascheduler_tasks_touch_updated_at
        BEFORE UPDATE ON yascheduler_tasks
        FOR EACH ROW EXECUTE FUNCTION YASCHEDULER_TOUCH_UPDATED_AT();
    END IF;
END $$;

-- Install yascheduler_nodes trigger if the updated_at column exists.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'yascheduler_nodes' AND column_name = 'updated_at'
    ) THEN
        DROP TRIGGER IF EXISTS yascheduler_nodes_touch_updated_at
        ON yascheduler_nodes;
        CREATE TRIGGER yascheduler_nodes_touch_updated_at
        BEFORE UPDATE ON yascheduler_nodes
        FOR EACH ROW EXECUTE FUNCTION YASCHEDULER_TOUCH_UPDATED_AT();
    END IF;
END $$;
