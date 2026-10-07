-- Rollback of 2026-10-06_handoff_summary_callbacks.sql (drops the new data — export it first if it must be kept).
DROP TABLE IF EXISTS orch_handoffs;
DROP TABLE IF EXISTS orch_audit;
DROP TABLE IF EXISTS orch_conv_summaries;
ALTER TABLE orch_callbacks
  DROP KEY k_cb_status,
  DROP KEY k_cb_conv,
  DROP COLUMN conversation_id,
  DROP COLUMN source_message_id,
  DROP COLUMN reason,
  DROP COLUMN promise,
  DROP COLUMN due_at,
  DROP COLUMN status,
  DROP COLUMN assigned_agent_id,
  DROP COLUMN completed_at,
  DROP COLUMN completed_by,
  DROP COLUMN recovered_at,
  DROP COLUMN overdue_notified_at;
