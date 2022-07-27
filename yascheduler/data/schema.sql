CREATE TABLE yascheduler_nodes (
    ip VARCHAR(15) UNIQUE,
    ncpus SMALLINT DEFAULT NULL,
    enabled BOOLEAN DEFAULT TRUE,
    cloud VARCHAR(32) DEFAULT NULL,
    username VARCHAR(255) DEFAULT 'root'
);

CREATE TABLE yascheduler_tasks (
    task_id INT PRIMARY KEY,
    label VARCHAR(256),
    metadata jsonb,
    ip VARCHAR(15),
    status SMALLINT
);
CREATE SEQUENCE task_id_seq START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;
ALTER SEQUENCE task_id_seq OWNED BY yascheduler_tasks.task_id;
ALTER TABLE ONLY yascheduler_tasks ALTER COLUMN task_id SET DEFAULT nextval('task_id_seq'::regclass);
