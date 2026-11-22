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
WHERE status IN (SELECT unnest(cast(:statuses AS task_status [])))
ORDER BY task_id
LIMIT :lim;
