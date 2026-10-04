-- Ms Tanya — orch_ tables in the CRM's existing MySQL database (Tushar's decision, 25-Sep-2026).
-- Plain English: the permanent, audited copy of everything Ms Tanya does. Written BEHIND her by the
-- persister (tanya/workers.py) from Redis — never read or written in the live chat path.
-- No sb_ (CRM) table is touched. Run once on staging, then on live (DBA approval).
-- 8 tables from the v4 design + 8 new (Architecture v3.2 §18) + orch_voice_calls (PWA voice calls, at the end).
-- Text columns hold JSON as plain text so any MySQL 5.7+/8 or MariaDB accepts them.

-- ---------------------------------------------------------------- v4 tables
CREATE TABLE IF NOT EXISTS orch_inbox (            -- every customer message, word for word (masked)
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  event_id VARCHAR(64) NULL,
  user_id VARCHAR(64) NOT NULL,
  conversation_id VARCHAR(64) NULL,
  msg_no INT NOT NULL,
  text MEDIUMTEXT NOT NULL,
  received_at DATETIME NOT NULL,
  UNIQUE KEY uq_inbox (user_id, msg_no),
  KEY k_inbox_time (received_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS orch_replies (          -- every message Ms Tanya sent, with its line id and action
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  user_id VARCHAR(64) NOT NULL,
  msg_no INT NOT NULL,
  line_id VARCHAR(12) NOT NULL,                    -- FX-xx for fixed lines, AI for written replies
  action VARCHAR(40) NOT NULL,
  text MEDIUMTEXT NOT NULL,
  delivered TINYINT NOT NULL DEFAULT 1,            -- 0 = never reached him → never remembered as said
  sent_at DATETIME NOT NULL,
  UNIQUE KEY uq_replies (user_id, msg_no),
  KEY k_replies_time (sent_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS orch_conversation_state (   -- BOT / HUMAN mode per conversation
  conversation_id VARCHAR(64) PRIMARY KEY,
  user_id VARCHAR(64) NOT NULL,
  mode VARCHAR(10) NOT NULL,
  since DATETIME NOT NULL,
  by_who VARCHAR(40) NULL,
  updated_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS orch_ai_usage (         -- the cost ledger: every model call, including failures
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  user_id VARCHAR(64) NOT NULL,
  purpose VARCHAR(20) NOT NULL,                    -- understand | reply | check | note | audit
  provider VARCHAR(20) NOT NULL,
  model VARCHAR(60) NOT NULL,
  tokens_in INT NOT NULL DEFAULT 0,
  tokens_out INT NOT NULL DEFAULT 0,
  ms INT NOT NULL DEFAULT 0,
  cost_inr DECIMAL(12,4) NOT NULL DEFAULT 0,
  ok TINYINT NOT NULL DEFAULT 1,
  error VARCHAR(255) NULL,
  at DATETIME NOT NULL,
  KEY k_usage_time (at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS orch_lead_signals (     -- interest, purchase intent, timing objection, call preference
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  user_id VARCHAR(64) NOT NULL,
  name VARCHAR(40) NOT NULL,
  evidence VARCHAR(255) NULL,
  at DATETIME NOT NULL,
  KEY k_sig_user (user_id, at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS orch_alerts (           -- things a person must look at (cases, Hot leads, failures)
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  user_id VARCHAR(64) NULL,
  kind VARCHAR(40) NOT NULL,
  detail MEDIUMTEXT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'open',
  at DATETIME NOT NULL,
  KEY k_alert_status (status, at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS orch_dead_letters (     -- jobs that failed 5 times (nothing silently lost)
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  stream VARCHAR(40) NOT NULL,
  stream_id VARCHAR(40) NOT NULL,
  body MEDIUMTEXT NULL,
  at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS orch_kb_chunks (        -- approved knowledge pieces (RAG), with version
  chunk_id VARCHAR(64) PRIMARY KEY,
  doc_id VARCHAR(40) NOT NULL,
  title VARCHAR(255) NOT NULL,
  category VARCHAR(40) NULL,
  status VARCHAR(120) NULL,
  version VARCHAR(20) NULL,
  text MEDIUMTEXT NOT NULL,
  content_hash CHAR(40) NOT NULL,
  updated_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------- new in Architecture v3.2
CREATE TABLE IF NOT EXISTS orch_lead_state (       -- journey, last promise, counters, temperature, with version
  user_id VARCHAR(64) PRIMARY KEY,
  version INT NOT NULL,
  temperature VARCHAR(12) NULL,
  mode VARCHAR(10) NULL,
  journey MEDIUMTEXT NULL,
  counters MEDIUMTEXT NULL,
  updated_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS orch_lead_facts (       -- every fact with source, his words, date-time, stated/inferred
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  user_id VARCHAR(64) NOT NULL,
  field VARCHAR(40) NOT NULL,
  value VARCHAR(255) NOT NULL,
  his_words VARCHAR(500) NULL,
  source VARCHAR(20) NOT NULL,                     -- chat | agent_note | crm_field | app
  msg_no INT NULL,
  stated TINYINT NOT NULL DEFAULT 1,
  confidence DECIMAL(4,2) NULL,
  at DATETIME NOT NULL,
  KEY k_facts_user (user_id, field, at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS orch_lead_events (      -- sessions, value cards, cases, activities, call outcomes
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  user_id VARCHAR(64) NOT NULL,
  type VARCHAR(40) NOT NULL,
  detail MEDIUMTEXT NULL,
  at DATETIME NOT NULL,
  KEY k_events_user (user_id, at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS orch_ai_notes (         -- master copy of Ms Tanya's notes + deletion records
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  user_id VARCHAR(64) NOT NULL,
  kind VARCHAR(20) NOT NULL,                       -- lead_brief | session_note
  text MEDIUMTEXT NOT NULL,
  crm_note_id VARCHAR(64) NULL,
  deleted TINYINT NOT NULL DEFAULT 0,
  deletion_reason VARCHAR(255) NULL,
  at DATETIME NOT NULL,
  KEY k_notes_user (user_id, kind, at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS orch_callbacks (        -- requested → booked → completed (or escalated)
  callback_id VARCHAR(80) PRIMARY KEY,
  user_id VARCHAR(64) NOT NULL,
  kind VARCHAR(20) NOT NULL,                       -- person | purchase | call_preference
  state VARCHAR(20) NOT NULL,
  requested_at DATETIME NOT NULL,
  when_text VARCHAR(120) NULL,
  slot MEDIUMTEXT NULL,
  outcome VARCHAR(40) NULL,                        -- agent outcome code (Architecture §13.19)
  updated_at DATETIME NOT NULL,
  KEY k_cb_state (state, requested_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS orch_reply_trace (      -- the full trace of every turn (labels, action, guard, cost)
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  user_id VARCHAR(64) NOT NULL,
  action VARCHAR(40) NULL,
  reason VARCHAR(20) NULL,
  cost_inr DECIMAL(12,4) NOT NULL DEFAULT 0,
  trace MEDIUMTEXT NOT NULL,
  at DATETIME NOT NULL,
  KEY k_trace_user (user_id, at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS orch_pilot_groups (     -- pilot vs comparison group, from day 1
  user_id VARCHAR(64) PRIMARY KEY,
  group_name VARCHAR(20) NOT NULL,
  assigned_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS orch_compliance_audits (  -- morning audit: item, exact lines, AI verdict, human label, fix
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  audit_date DATE NOT NULL,
  user_id VARCHAR(64) NOT NULL,
  item VARCHAR(80) NOT NULL,
  lines_text MEDIUMTEXT NULL,
  ai_verdict VARCHAR(20) NOT NULL,
  human_label VARCHAR(20) NULL,                    -- confirmed | not a breach
  fix_note VARCHAR(255) NULL,
  model VARCHAR(60) NULL,
  created_at DATETIME NOT NULL,
  KEY k_audit_date (audit_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------- voice (PWA "Talk to Tanya", ElevenLabs)
CREATE TABLE IF NOT EXISTS orch_voice_calls (        -- one row per voice call: masked transcript + summary
  el_conversation_id VARCHAR(64) PRIMARY KEY,      -- ElevenLabs conversation id (webhook retries dedupe here)
  user_id VARCHAR(64) NOT NULL,                    -- CRM user id (same key as chat), or pwa:<uid> if none yet
  pwa_uid VARCHAR(64) NULL,
  sb_user_id VARCHAR(64) NULL,
  sb_conversation_id VARCHAR(64) NULL,             -- where the agent note was filed
  started_at DATETIME NULL,
  duration_secs INT NULL,
  status VARCHAR(20) NULL,
  ended_reason VARCHAR(255) NULL,
  call_successful VARCHAR(20) NULL,                -- ElevenLabs evaluation: success | failure | unknown
  title VARCHAR(255) NULL,
  summary MEDIUMTEXT NULL,
  kb_queries MEDIUMTEXT NULL,                      -- JSON: what Tanya searched in the knowledge base
  transcript MEDIUMTEXT NULL,                      -- JSON: [{role, at_secs, text, tools?}], masked
  masked VARCHAR(255) NULL,                        -- kinds of private data masked out
  cost INT NULL,                                   -- ElevenLabs credits
  received_at DATETIME NOT NULL,
  KEY k_voice_user (user_id, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
