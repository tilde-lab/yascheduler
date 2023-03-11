CREATE TABLE yascheduler_nodes (
    ip VARCHAR(15) UNIQUE,
    ncpus SMALLINT DEFAULT NULL,
    enabled BOOLEAN DEFAULT TRUE,
    cloud VARCHAR(32) DEFAULT NULL,
    username VARCHAR(255) DEFAULT 'root'
);

CREATE TABLE yascheduler_tasks (
    task_id SERIAL PRIMARY KEY,
    label VARCHAR(256),
    metadata jsonb,
    ip VARCHAR(15),
    status SMALLINT
);
