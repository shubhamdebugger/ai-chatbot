-- Ms Tanya — 06-Oct-2026: smart handoff recovery, conversation summary, callback dashboard (+ audit trail).
-- Run once on the orch_ database (the one in MYSQL_DB), after 2026-10-05_lifecycle.sql. New installs get the same
-- objects from orch_tables.sql. Rollback: 2026-10-06_handoff_summary_callbacks_rollback.sql.

CREATE TABLE IF NOT EXISTS orch_handoffs (          -- every time Tanya hands a chat to staff, and how it ended
  handoff_id VARCHAR(64) PRIMARY KEY,                -- HO-<conversation>-<epoch>
  conversation_id VARCHAR(64) NOT NULL,
  user_id VARCHAR(64) NOT NULL,
  action VARCHAR(40) NULL,                           -- HAND_OVER_PERSON / LOG_GRIEVANCE
  reason VARCHAR(40) NULL,                           -- decider reason code (R04, R06, R06-FAILED, R03 ...)
  period VARCHAR(8) NULL,                            -- day (10 min) / night (12 h)
  started_at DATETIME NOT NULL,
  due_at DATETIME NOT NULL,                          -- recovery deadline
  status VARCHAR(20) NOT NULL DEFAULT 'open',        -- open / agent_replied / recovered / released
  last_message_id VARCHAR(32) NULL,                  -- customer message that triggered the handoff
  agent_assigned_id VARCHAR(32) NULL,
  agent_assigned_at DATETIME NULL,
  agent_replied_id VARCHAR(32) NULL,
  agent_replied_at DATETIME NULL,
  recovered_at DATETIME NULL,
  recovery_message_id VARCHAR(32) NULL,              -- CRM id of FX-32 / FX-33
  KEY k_handoff_open (status, due_at),
  KEY k_handoff_conv (conversation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS orch_audit (             -- audit trail: handoffs and callbacks
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  entity VARCHAR(20) NOT NULL,                       -- handoff / callback
  entity_id VARCHAR(80) NOT NULL,
  event VARCHAR(40) NOT NULL,                        -- created, assigned, reassigned, status:*, completed, recovered_by_tanya ...
  actor VARCHAR(64) NULL,
  detail MEDIUMTEXT NULL,
  at DATETIME NOT NULL,
  KEY k_audit_entity (entity, entity_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS orch_conv_summaries (    -- Conversation Summary shown in the CRM notes (one per chat)
  conversation_id VARCHAR(64) PRIMARY KEY,
  user_id VARCHAR(64) NOT NULL,
  summary MEDIUMTEXT NOT NULL,
  msg_count INT NULL,
  last_message_id VARCHAR(32) NULL,
  crm_note_id VARCHAR(32) NULL,
  model VARCHAR(60) NULL,
  updated_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

ALTER TABLE orch_callbacks
  MODIFY kind VARCHAR(30) NOT NULL,
  ADD COLUMN conversation_id VARCHAR(64) NULL AFTER user_id,
  ADD COLUMN source_message_id VARCHAR(32) NULL AFTER conversation_id,
  ADD COLUMN reason VARCHAR(30) NULL AFTER kind,
  ADD COLUMN promise MEDIUMTEXT NULL AFTER reason,
  ADD COLUMN due_at DATETIME NULL AFTER requested_at,
  ADD COLUMN status VARCHAR(20) NULL AFTER state,            -- pending / assigned / in_progress / overdue / completed
  ADD COLUMN assigned_agent_id VARCHAR(32) NULL AFTER status,
  ADD COLUMN completed_at DATETIME NULL,
  ADD COLUMN completed_by VARCHAR(64) NULL,
  ADD COLUMN recovered_at DATETIME NULL,
  ADD COLUMN overdue_notified_at DATETIME NULL,
  ADD KEY k_cb_status (status, due_at),
  ADD KEY k_cb_conv (conversation_id);
