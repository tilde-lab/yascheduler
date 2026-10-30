-- Keyset page of one: the next snapshot entry after :after, in C-collation
-- byte order so a parent directory always sorts before its descendants
-- regardless of the database locale. One row per read: deployment streams
-- entries and never holds the whole snapshot.
SELECT
    task_id,
    path,
    kind,
    content
FROM yascheduler_task_inputs
WHERE task_id = :task_id AND (path COLLATE "C") > :after
ORDER BY path COLLATE "C"
LIMIT 1;
