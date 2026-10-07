-- Ms Tanya — lifecycle columns for databases created from orch_tables.sql before 05-Oct-2026 (G3/G4, v4 §12).
-- New installs already get these columns from orch_tables.sql. Run once (DBA), as a user allowed to ALTER.
ALTER TABLE orch_inbox
  ADD COLUMN status VARCHAR(20) NULL AFTER received_at,
  ADD COLUMN outcome_at DATETIME NULL AFTER status,
  ADD KEY k_inbox_event (event_id);
ALTER TABLE orch_replies
  ADD COLUMN source_event_id VARCHAR(64) NULL AFTER sent_at,
  ADD COLUMN crm_message_id VARCHAR(32) NULL AFTER source_event_id;
