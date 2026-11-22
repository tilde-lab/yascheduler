SELECT
    task_id,
    path,
    kind,
    content
FROM yascheduler_task_inputs
WHERE task_id IN (SELECT unnest(cast(:task_ids AS INT [])))
-- C-collation byte order keeps the batched read consistent with the
-- per-entry stream (parent before descendants) regardless of locale.
ORDER BY task_id, path COLLATE "C";
