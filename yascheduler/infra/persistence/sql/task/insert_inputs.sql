-- Insert one task's owned input snapshot entries in a single statement.
-- Arrays are positionally aligned by the caller (repository insert); the
-- task_input_pk rejects duplicate paths, the FK rejects orphans, and the kind/
-- content CHECKs reject invalid rows — all inside the caller's transaction.
INSERT INTO yascheduler_task_inputs (task_id, path, kind, content)
SELECT
    :task_id,
    entry_path,
    entry_kind,
    entry_content
FROM unnest(
    cast(:paths AS TEXT []),
    cast(:kinds AS TEXT []),
    cast(:contents AS BYTEA [])
) AS entries (entry_path, entry_kind, entry_content);
