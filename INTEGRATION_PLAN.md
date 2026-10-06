# Tanya AI → CRM Integration Plan

> **Goal:** Connect Tanya AI to TG CRM v3.8.4 (Support Board) so she can auto-reply to customer messages
> **Estimated effort:** 2-3 weeks to pilot
> **Last updated:** 2026-09-29

---

## Phase 0 — Discovery & Confirmation (Days 1-2)

### 0.1 Confirm CRM API Function Names

**Owner:** CODER D
**File to check:** `crm/include/ajax.php` (the switch statement)

| # | Function Name | Status | Action |
|---|---------------|--------|--------|
| A1 | `send-message` | TO CONFIRM | Check if this exact name exists in ajax.php switch |
| A2 | Webhook payload shape | TO CONFIRM | Check what CRM actually sends to webhook URL |
| A4 | `get-notes` / `add-note` / `update-note` | TO CONFIRM | Check if note functions exist |
| A5 | `get-user` field names | TO CONFIRM | Check what fields are returned |
| A8 | `update-conversation-department` | TO CONFIRM | Check if department update exists |

**How to check:**
```bash
# Search for function names in CRM source
grep -n "case 'send-message'" crm/include/ajax.php
grep -n "case 'get-user'" crm/include/ajax.php
grep -n "case 'update-conversation-department'" crm/include/ajax.php
grep -n "case 'get-notes'" crm/include/ajax.php
grep -n "case 'add-note'" crm/include/ajax.php
```

**Output needed:** For each function, provide:
- Exact function name (case-sensitive)
- Required POST parameters
- Response JSON shape (success + error examples)

### 0.2 Confirm Webhook Mechanism

**Owner:** CODER D

**Questions:**
1. Does TG CRM v3.8.4 have a built-in webhook feature?
2. If yes, where is it configured? (DB setting? Admin panel?)
3. What is the payload format?
4. Does it support custom headers?
5. What is the timeout/retry behavior?

**If no webhook exists:** We will implement Alternative 1 (polling) — see `MEETING_QA.md` Q7.

### 0.3 Confirm CRM Database Schema

**Owner:** CODER D

**Queries to run:**
```sql
-- Check sb_users columns
DESCRIBE sb_users;

-- Check sb_conversations columns
DESCRIBE sb_conversations;

-- Check sb_messages columns
DESCRIBE sb_messages;

-- Check if sb_users_data exists (for extra fields)
SHOW TABLES LIKE 'sb_users_data';
```

**Output needed:** Column names and types for each table.

---

## Phase 1 — CRM Configuration (Days 3-4)

### 1.1 Create Tanya's Agent Account

**Owner:** CODER D

```sql
-- Create agent account for Tanya
INSERT INTO sb_users (first_name, last_name, email, user_type, token, password, department)
VALUES ('Ms', 'Tanya', 'tanya-ai@tglevel.com', 'agent',
        LOWER(HEX(RANDOM_BYTES(20))), '', 0);
```

**Note the returned `id`** — this is `TANYA_AGENT_ID`.

### 1.2 Create Human Support Department

**Owner:** CODER D

```sql
-- Check if department exists
SELECT * FROM sb_departments WHERE name = 'Human Support';

-- If not, create it
INSERT INTO sb_departments (name, color) VALUES ('Human Support', '#FF6B6B');
```

**Note the returned `id`** — this is `CRM_HUMAN_DEPARTMENT_ID`.

### 1.3 Configure Webhook URL

**Owner:** CODER D

**Option A — If CRM has webhook setting:**
```sql
-- Check current webhook settings
SELECT * FROM sb_settings WHERE name LIKE '%webhook%';

-- Set webhook URL
INSERT INTO sb_settings (name, value) VALUES ('webhook-url', 'https://<tanya-server>/webhook/crm')
ON DUPLICATE KEY UPDATE value = 'https://<tanya-server>/webhook/crm';
```

**Option B — If no webhook setting exists:**
- We will implement a lightweight PHP webhook receiver in CRM that forwards to Tanya
- OR use polling alternative (see `MEETING_QA.md` Q7)

### 1.4 Set Webhook Secret

**Owner:** CODER D + App Dev (together)

```bash
# Generate a strong secret
openssl rand -hex 32
```

Store this in:
- CRM webhook configuration
- Tanya's `.env` as `CRM_WEBHOOK_SECRET`

### 1.5 Verify CRM API Accessibility

**Owner:** CODER D

```bash
# From Tanya server, test CRM API is reachable
curl -X POST http://localhost/crm/include/api.php \
  -d "token=<admin_token>&function=get-user&user_id=1"

# Expected: JSON response with user data
```

---

## Phase 2 — Tanya Deployment (Days 5-7)

### 2.1 Provision Server

**Owner:** App Dev / DevOps

**Minimum requirements:**
- 2 CPU cores
- 4 GB RAM
- 20 GB disk
- Python 3.11+
- Redis 5.0+
- MySQL 5.7+ / 8.0+

### 2.2 Install Dependencies

```bash
# Clone/copy project
cd /opt/tanya_ai

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Linux
# .venv\Scripts\activate   # Windows

# Install Python packages
pip install -r requirements.txt

# Install Redis (if not present)
sudo apt install redis-server  # Ubuntu/Debian
sudo systemctl enable redis
sudo systemctl start redis

# Install MySQL (if not present)
sudo apt install mysql-server
sudo systemctl enable mysql
sudo systemctl start mysql
```

### 2.3 Create Database Tables

```bash
mysql -u root -p tglevel_support < db/orch_tables.sql
```

### 2.4 Configure Environment

**Owner:** App Dev

Create `.env`:

```env
# Run mode
RUN_MODE=server
HOST=0.0.0.0
PORT=8000
DEV_CONSOLE=0
USE_LANGGRAPH=1

# AI Provider
PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...

# Redis
REDIS_URL=redis://localhost:6379

# MySQL
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=tglevel_support
MYSQL_PASSWORD=<crm_db_password>
MYSQL_DB=tglevel_support

# CRM Integration
CRM_MODE=supportboard
CRM_API_URL=http://localhost/crm/include/api.php
CRM_API_TOKEN=<tanya_agent_token_from_1.1>
CRM_WEBHOOK_SECRET=<secret_from_1.4>
TANYA_AGENT_ID=<id_from_1.1>
CRM_HUMAN_DEPARTMENT_ID=<id_from_1.2>
```

### 2.5 Start Services

```bash
# Start gateway
python app.py &

# Start turn workers (8 lanes)
for i in {0..7}; do
    python -m tanya.workers turn $i &
done

# Start persister
python -m tanya.workers persist &

# Start CRM loader
python -m tanya.workers loader &

# Start session closer
python -m tanya.workers sessions &
```

### 2.6 Verify Services

```bash
# Check gateway
curl http://localhost:8000/health

# Check Redis
redis-cli ping

# Check MySQL
mysql -u tglevel_support -p -e "SHOW TABLES LIKE 'orch_%';"

# Check workers are running
ps aux | grep "tanya.workers"
```

---

## Phase 3 — Testing (Days 8-10)

### 3.1 Unit Tests (No CRM)

```bash
python tests/run_tests.py
```

**Expected:** All 34+ tests pass.

### 3.2 Webhook Test

```bash
curl -X POST http://localhost:8000/webhook/crm \
  -H "Content-Type: application/json" \
  -d '{
    "function": "message-sent",
    "key": "<CRM_WEBHOOK_SECRET>",
    "data": {
      "message_id": 99999,
      "user_id": 42,
      "conversation_id": 101,
      "conversation_user_id": 42,
      "user_type": "user",
      "message": "Hello Tanya, what is stop-loss?"
    }
  }'
```

**Expected response:** `{"ok": true, "queued": true}`

### 3.3 API Call Test

```bash
# Test get-user
curl -X POST http://localhost:8000/dev/chat \
  -H "Content-Type: application/json" \
  -d '{"user_id": "42", "text": "What is stop-loss?"}'
```

**Expected:** Tanya processes and returns a reply.

### 3.4 End-to-End Test

1. Open CRM admin panel
2. Create a test conversation with a test user
3. Send a message as the user
4. Verify Tanya replies within 30 seconds
5. Verify reply appears in CRM conversation

### 3.5 Staff Message Test

1. Open the same conversation as an agent
2. Send a message
3. Verify Tanya stops replying (HUMAN mode)
4. Verify HUMAN mode expires after 12 hours (or release manually)

### 3.6 Handoff Test

1. Send a message Tanya cannot answer (e.g., "I want to talk to a human")
2. Verify conversation is assigned to Human Support department
3. Verify human agent sees it in their queue

### 3.7 Load Test (Optional)

```bash
# Send 10 messages rapidly
for i in {1..10}; do
  curl -X POST http://localhost:8000/webhook/crm \
    -H "Content-Type: application/json" \
    -d "{\"function\":\"message-sent\",\"key\":\"<secret>\",\"data\":{\"user_id\":42,\"message\":\"Message $i\",\"user_type\":\"user\",\"conversation_id\":101,\"message_id\":$i}}"
done
```

**Expected:** All messages processed, no crashes, latency < 30s.

---

## Phase 4 — Pilot (Days 11-17)

### 4.1 Pilot Scope

| Parameter | Value |
|-----------|-------|
| Users | 10 trial users |
| Duration | 1 week |
| Hours | 10:00-19:00 IST (calling window) |
| Support | On-call dev available |

### 4.2 Pilot Success Criteria

| Criteria | Target | Measurement |
|----------|--------|-------------|
| Response time | < 15s average | `orch_reply_trace` |
| Accuracy | > 80% correct answers | Manual review |
| User satisfaction | > 4/5 | Survey |
| Zero critical bugs | 0 | Bug tracker |
| AI spend | < INR 200/day | `orch_ai_usage` |

### 4.3 Pilot Monitoring

**Daily checks:**
```bash
# Check health
curl http://localhost:8000/health

# Check errors
redis-cli XLEN tanya:errors

# Check dead letters
redis-cli XLEN tanya:dead

# Check daily spend
mysql -e "SELECT SUM(cost_inr) FROM orch_ai_usage WHERE date = CURDATE();"

# Check turn latency
mysql -e "SELECT AVG(duration_ms) FROM orch_reply_trace WHERE date = CURDATE();"
```

### 4.4 Pilot Review

At end of pilot, review:
- All metrics vs targets
- User feedback
- Bugs found and fixed
- Issues encountered
- Lessons learned

**Decision point:** Go / No-Go for full launch

---

## Phase 5 — Full Launch (Day 18+)

### 5.1 Pre-Launch Checklist

| # | Item | Status |
|---|------|--------|
| 1 | Pilot criteria met | ☐ |
| 2 | All critical bugs fixed | ☐ |
| 3 | Monitoring alerts configured | ☐ |
| 4 | On-call rotation defined | ☐ |
| 5 | Rollback plan tested | ☐ |
| 6 | Documentation updated | ☐ |
| 7 | Team trained | ☐ |

### 5.2 Launch Steps

1. Set `CRM_MODE=supportboard` in production `.env`
2. Enable CRM webhook for all conversations
3. Start all Tanya workers
4. Monitor for 24 hours
5. Gradually increase user base

### 5.3 Post-Launch

- Daily health checks for first week
- Weekly performance review
- Monthly cost review
- Quarterly compliance audit

---

## Risk Register

| # | Risk | Probability | Impact | Mitigation | Owner |
|---|------|-------------|--------|------------|-------|
| R1 | API function names wrong | High | Blocking | Confirm in Phase 0 | CODER D |
| R2 | Webhook doesn't exist | Medium | Blocking | Implement polling fallback | App Dev |
| R3 | LLM cost exceeds budget | Medium | High | Daily ceiling + alerts | App Dev |
| R4 | CRM performance impact | Low | Medium | Rate limit + off-peak | CODER D |
| R5 | Data privacy breach | Medium | High | Input masking + audit | App Dev |
| R6 | SEBI compliance violation | Low | Critical | 3-layer guard | App Dev |
| R7 | Redis data loss | Low | High | AOF persistence | App Dev |
| R8 | User adoption low | Medium | Medium | Training + UX review | Product |

---

## Timeline Summary

```
Week 1:  [Phase 0] Discovery & Confirmation
         [Phase 1] CRM Configuration

Week 2:  [Phase 2] Tanya Deployment
         [Phase 3] Testing

Week 3:  [Phase 4] Pilot (Days 11-17)
         [Phase 5] Full Launch (Day 18+)
```

---

## Open Dependencies

| # | Dependency | Blocking Phase | Owner |
|---|------------|----------------|-------|
| D1 | API function names confirmed | Phase 1 | CODER D |
| D2 | Webhook mechanism confirmed | Phase 1 | CODER D |
| D3 | Server provisioned | Phase 2 | DevOps |
| D4 | AI provider key obtained | Phase 2 | App Dev |
| D5 | CRM DB password shared | Phase 2 | CODER D |

---

## Sign-Off

| Role | Name | Date | Signature |
|------|------|------|-----------|
| CRM Developer (CODER D) | | | |
| App Developer | | | |
| Product Owner | | | |
| DevOps | | | |

---

*This plan should be reviewed and updated after each phase completion.*
