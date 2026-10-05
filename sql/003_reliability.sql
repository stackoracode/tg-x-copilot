-- Upgrade for databases created from the first 001_schema.sql (before atomic task claims).
-- Fresh installs already have these changes in 001_schema.sql; skip this file for them.
USE tg_x_copilot;

ALTER TABLE tasks
  ADD COLUMN claimed_by VARCHAR(64) NULL AFTER attempts,
  ADD COLUMN claimed_at DATETIME(3) NULL AFTER claimed_by,
  ADD KEY idx_tasks_claim (status, claimed_at);

-- API keys are env-only now: remove any credentials previously saved from the admin UI.
DELETE FROM settings
 WHERE k IN ('cpa.api_key', 'jev.api_key', 'r2.access_key_id', 'r2.secret_access_key',
             'pipeline.merge_window_seconds');
ALTER TABLE settings DROP COLUMN is_secret;

-- Tasks left PROCESSING by the old code have no claim; return them to the queue.
UPDATE tasks SET status = 'received', stage = 'requeued' WHERE status = 'processing';
