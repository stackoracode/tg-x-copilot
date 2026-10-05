-- tg-x-copilot schema (MySQL 8.0+). Idempotent: safe to re-run.
-- mysql -u root -p < sql/001_schema.sql

CREATE DATABASE IF NOT EXISTS tg_x_copilot
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE tg_x_copilot;

-- One row per Copilot task (one forwarded message or merged message group).
CREATE TABLE IF NOT EXISTS tasks (
  id            CHAR(32)      NOT NULL PRIMARY KEY,
  status        VARCHAR(24)   NOT NULL,
  stage         VARCHAR(32)   NULL,
  locale        VARCHAR(16)   NOT NULL DEFAULT 'en-US',
  market        VARCHAR(8)    NOT NULL DEFAULT 'US',
  tg_chat_id    BIGINT        NOT NULL,
  tg_user_id    BIGINT        NOT NULL,
  envelope      JSON          NOT NULL,          -- normalized InputEnvelope
  source_text   MEDIUMTEXT    NULL,
  jev_result    JSON          NULL,              -- TriageResult (Jev typed answers -> route)
  score         DECIMAL(5,3)  NULL,              -- Jev value score normalized to 0..1
  route         VARCHAR(24)   NULL,
  evaluation    JSON          NULL,
  draft_text    TEXT          NULL,
  draft_meta    JSON          NULL,              -- hook, claims, guard problems, media summary
  attempts      INT           NOT NULL DEFAULT 0,
  claimed_by    VARCHAR(64)   NULL,              -- worker that atomically claimed the task
  claimed_at    DATETIME(3)   NULL,              -- claim lease start (stale leases are requeued)
  error         TEXT          NULL,
  created_at    DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at    DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  KEY idx_tasks_status (status, created_at),
  KEY idx_tasks_claim (status, claimed_at),
  KEY idx_tasks_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Media items per task. Incoming media is NOT stored: source_* columns only describe it.
-- asset_* points at the single R2 object persisted for this item (if any):
--   final  = kept/enhanced/generated image used in the draft
--   review = compressed copy kept so the operator can review it (deleted on approve/reject)
-- Assets are content-addressed (assets/<sha[:2]>/<sha>.<ext>) and shared across tasks;
-- an object is deleted from R2 only when no row references asset_key any more.
CREATE TABLE IF NOT EXISTS task_media (
  id               BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  task_id          CHAR(32)     NOT NULL,
  idx              INT          NOT NULL,
  tg_message_id    BIGINT       NOT NULL,
  kind             VARCHAR(24)  NOT NULL,
  mime             VARCHAR(64)  NULL,
  size_bytes       BIGINT       NULL,
  width            INT          NULL,
  height           INT          NULL,
  source_sha256    CHAR(64)     NULL,
  analysis         JSON         NULL,
  decision         VARCHAR(16)  NULL,            -- keep | enhance | regenerate | review
  decision_reason  TEXT         NULL,
  ai_generated     TINYINT(1)   NOT NULL DEFAULT 0,
  asset_key        VARCHAR(255) NULL,
  asset_kind       VARCHAR(8)   NULL,            -- final | review
  asset_size       BIGINT       NULL,
  asset_mime       VARCHAR(64)  NULL,
  created_at       DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at       DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  UNIQUE KEY uq_media_task_idx (task_id, idx),
  KEY idx_media_source_sha (source_sha256),
  KEY idx_media_asset (asset_key),
  CONSTRAINT fk_media_task FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Per-task timeline for debugging on the VPS (also shown in the admin UI).
CREATE TABLE IF NOT EXISTS task_events (
  id          BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  task_id     CHAR(32)     NOT NULL,
  level       VARCHAR(8)   NOT NULL DEFAULT 'info',
  step        VARCHAR(32)  NOT NULL,
  message     TEXT         NOT NULL,
  data        JSON         NULL,
  created_at  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  KEY idx_events_task (task_id, id),
  CONSTRAINT fk_events_task FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Runtime setting overrides (whitelisted non-secret keys only, see config.EDITABLE_KEYS).
-- API keys/credentials are env-only and must never be stored here.
CREATE TABLE IF NOT EXISTS settings (
  k           VARCHAR(128) NOT NULL PRIMARY KEY,
  v           JSON         NOT NULL,
  updated_at  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Supported locales (i18n extension point).
CREATE TABLE IF NOT EXISTS locales (
  code           VARCHAR(16)  NOT NULL PRIMARY KEY,
  language_name  VARCHAR(64)  NOT NULL,
  market         VARCHAR(8)   NOT NULL,
  enabled        TINYINT(1)   NOT NULL DEFAULT 1,
  config         JSON         NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Lightweight X platform rules per locale/market (char limit, hashtags, banned phrases...).
CREATE TABLE IF NOT EXISTS x_rules (
  id           INT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
  locale       VARCHAR(16)  NOT NULL,
  market       VARCHAR(8)   NOT NULL,
  rule_key     VARCHAR(64)  NOT NULL,
  rule_value   JSON         NOT NULL,
  description  VARCHAR(255) NULL,
  enabled      TINYINT(1)   NOT NULL DEFAULT 1,
  UNIQUE KEY uq_rules (locale, market, rule_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Hook patterns used as style guidance for the rewrite prompt.
CREATE TABLE IF NOT EXISTS hooks (
  id        INT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
  locale    VARCHAR(16)  NOT NULL,
  market    VARCHAR(8)   NOT NULL,
  name      VARCHAR(64)  NOT NULL,
  pattern   TEXT         NOT NULL,
  example   TEXT         NULL,
  weight    INT          NOT NULL DEFAULT 0,
  enabled   TINYINT(1)   NOT NULL DEFAULT 1,
  UNIQUE KEY uq_hooks (locale, market, name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Models discovered via GET /v1/models on CPA (OpenAI format) and TypeSafe (Jev).
CREATE TABLE IF NOT EXISTS models (
  id            INT           NOT NULL AUTO_INCREMENT PRIMARY KEY,
  provider      VARCHAR(32)   NOT NULL,
  model_id      VARCHAR(191)  NOT NULL,
  owned_by      VARCHAR(128)  NULL,
  capabilities  JSON          NULL,
  raw           JSON          NULL,
  last_seen_at  DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uq_models (provider, model_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
