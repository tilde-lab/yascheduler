SELECT
    task_id,
    path,
    kind,
    content
FROM yascheduler_task_inputs
WHERE task_id IN (SELECT unnest(cast(:task_ids AS INT [])))
ORDER BY task_id, path;
