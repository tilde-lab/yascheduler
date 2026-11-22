SELECT
    task_id,
    title,
    engine,
    remote_folder,
    local_folder,
    webhook_url,
    error,
    webhook_custom_params,
    extra,
    output_selections,
    status,
    allocated_node_id,
    created_at,
    updated_at
FROM yascheduler_tasks
WHERE task_id IN (SELECT unnest(cast(:task_ids AS int [])))
ORDER BY task_id;
