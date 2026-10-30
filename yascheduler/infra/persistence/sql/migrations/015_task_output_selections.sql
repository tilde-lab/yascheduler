-- Migration 015: record the output selections a task was accepted under.
-- NULL means the task predates the record; an empty list is not a valid record.
ALTER TABLE yascheduler_tasks
ADD COLUMN output_selections TEXT [];
