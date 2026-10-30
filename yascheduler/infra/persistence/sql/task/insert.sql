INSERT INTO yascheduler_tasks (
    title, engine, local_folder, webhook_url,
    webhook_custom_params, extra, output_selections
)
VALUES (
    :title, :engine, :local_folder, :webhook_url,
    :webhook_custom_params, :extra, :output_selections
)
RETURNING
    task_id, title, engine, remote_folder, local_folder, webhook_url, error,
    webhook_custom_params, extra, output_selections, status, allocated_node_id,
    created_at, updated_at;
