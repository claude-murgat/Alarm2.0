# Test Catalog — Alarm 2.0

Generated 2026-04-04. Complete catalog of every test method across all test files.

---

## File: tests/test_e2e.py

Main backend E2E test file. Runs against a live backend via `python -m pytest tests/test_e2e.py -v`.

---

### CLASS: TestBackendHealth

```
TEST: test_health_check
STEPS:
  1. GET /health
VERIFIES: status_code == 200 and json status == "ok"
PURPOSE: Ensures the backend health endpoint is responding correctly.
---
TEST: test_web_ui_loads
STEPS:
  1. GET / (root page)
VERIFIES: status_code == 200 and "Alarmes Critiques" in response text
PURPOSE: Ensures the web UI HTML loads and contains the expected title.
---
```

### CLASS: TestUserLogin

```
TEST: test_admin_login
STEPS:
  1. POST /api/auth/login with admin credentials
VERIFIES: status_code == 200, "access_token" in response, user.is_admin is True
PURPOSE: Validates admin can log in and receives a JWT with admin flag.
---
TEST: test_login_case_insensitive
STEPS:
  1. POST /api/auth/login with "ADMIN" (uppercase)
  2. POST /api/auth/login with "Admin" (mixed case)
  3. POST /api/auth/login with "aDmIn" (random case)
VERIFIES: All return status_code == 200, first returns user.name == "admin"
PURPOSE: Ensures login is case-insensitive for usernames.
---
TEST: test_user1_login
STEPS:
  1. POST /api/auth/login with user1 credentials
VERIFIES: status_code == 200, user.name == "user1"
PURPOSE: Validates non-admin user can log in.
---
TEST: test_invalid_login
STEPS:
  1. POST /api/auth/login with bad credentials
VERIFIES: status_code == 401
PURPOSE: Ensures invalid credentials are rejected.
---
TEST: test_user_list
STEPS:
  1. GET /api/users/
VERIFIES: status_code == 200, len(users) >= 3, no username contains spaces
PURPOSE: Validates user list returns all users with space-free names.
---
TEST: test_register_rejects_spaces_in_name
STEPS:
  1. POST /api/auth/register with name "bad name" (contains space)
VERIFIES: status_code == 422 or 400
PURPOSE: Ensures registration rejects names with spaces.
---
TEST: test_register_stores_lowercase
STEPS:
  1. Clean up "testcaseuser" if exists
  2. POST /api/auth/register with name "TestCaseUser"
  3. Clean up the created user
VERIFIES: Returned name == "testcaseuser" (lowercase)
PURPOSE: Ensures registration stores usernames in lowercase.
---
```

### CLASS: TestSingleAlarm

```
TEST: test_send_alarm
STEPS:
  1. Reset alarms, login as user1
  2. POST /api/alarms/send with title/message/severity
VERIFIES: status_code == 200, json status == "active"
PURPOSE: Validates basic alarm creation.
---
TEST: test_only_one_alarm_at_a_time
STEPS:
  1. POST /api/alarms/send (first alarm)
  2. POST /api/alarms/send (second alarm while first is active)
VERIFIES: Second request returns status_code == 409
PURPOSE: Enforces single active alarm constraint.
---
TEST: test_can_send_after_resolve
STEPS:
  1. POST /api/alarms/send (create alarm A)
  2. POST /api/alarms/{id}/resolve (resolve alarm A)
  3. POST /api/alarms/send (create alarm B)
VERIFIES: Third request returns status_code == 200, title == "Alarme B"
PURPOSE: Ensures a new alarm can be sent after resolving the previous one.
---
TEST: test_alarm_received_by_user
STEPS:
  1. POST /api/alarms/send with assigned_user_id = user1
  2. GET /api/alarms/mine (as user1)
VERIFIES: len(alarms) == 1, alarms[0].title == "Test reception"
PURPOSE: Validates that the assigned user receives the alarm via /mine endpoint.
---
```

### CLASS: TestAlarmAcknowledgement

```
TEST: test_acknowledge_alarm
STEPS:
  1. Reset clock/alarms, login as user1
  2. POST /api/alarms/send assigned to user1
  3. POST /api/alarms/{id}/ack as user1
VERIFIES: status_code == 200, status == "acknowledged", acknowledged_at not None, suspended_until not None
PURPOSE: Validates basic alarm acknowledgement flow.
---
TEST: test_suspended_alarm_visible_in_mine_as_acknowledged
STEPS:
  1. Create alarm assigned to user1
  2. Acknowledge it
  3. GET /api/alarms/mine as user1
VERIFIES: Alarm still visible in /mine with status == "acknowledged"
PURPOSE: Ensures acknowledged alarms remain visible (not hidden).
---
TEST: test_acknowledge_stores_user_name
STEPS:
  1. Create alarm assigned to user1
  2. Acknowledge as user1
VERIFIES: acknowledged_by_name == "user1"
PURPOSE: Ensures the acknowledging user's name is stored.
---
TEST: test_ack_expiry_reactivates_alarm
STEPS:
  1. Create alarm, acknowledge it
  2. Advance clock 31 minutes (suspension = 30 min)
  3. Wait for escalation loop tick
  4. Check alarm status
VERIFIES: Alarm status is "active" or "escalated" (not "acknowledged")
PURPOSE: Ensures ack suspension expires after 30 minutes and alarm reactivates.
---
TEST: test_ack_expiry_escalation_restarts
STEPS:
  1. Create alarm, acknowledge it
  2. Advance clock 31 minutes
  3. Wait for escalation loop tick
VERIFIES: Alarm status != "acknowledged" (reactivated or escalated)
PURPOSE: Ensures alarm is escalatable after ack expiry.
---
```

### CLASS: TestEscalation

```
TEST: test_escalation_user1_to_user2
STEPS:
  1. Reset, configure escalation chain: user1(pos1) -> user2(pos2) -> admin(pos3)
  2. Send alarm assigned to user1
  3. POST /api/test/trigger-escalation
  4. GET /api/alarms/
VERIFIES: alarm.assigned_user_id == user2_id, escalation_count == 1, status == "escalated"
PURPOSE: Validates basic escalation from position 1 to position 2.
---
TEST: test_escalation_chain_user2_to_admin
STEPS:
  1. Send alarm assigned to user1
  2. Trigger escalation twice
VERIFIES: alarm.assigned_user_id == admin_id, escalation_count == 2
PURPOSE: Validates double escalation through the full chain.
---
TEST: test_no_escalation_if_acknowledged
STEPS:
  1. Send alarm assigned to user1
  2. User1 acknowledges the alarm
  3. Trigger escalation
VERIFIES: alarm.assigned_user_id == user1_id (unchanged), escalation_count == 0
PURPOSE: Ensures acknowledged alarms are not escalated.
---
TEST: test_no_escalation_if_no_active_alarm
STEPS:
  1. Trigger escalation with no active alarm
VERIFIES: status_code == 200, escalated == 0
PURPOSE: Ensures escalation is a no-op when there are no active alarms.
---
TEST: test_escalation_wraps_around_after_last_user
STEPS:
  1. Send alarm assigned to user1
  2. Trigger escalation 3 times (user1 -> user2 -> admin -> user1)
VERIFIES: alarm.assigned_user_id == user1_id, escalation_count == 3
PURPOSE: Ensures escalation wraps around to the first user after the last.
---
TEST: test_escalation_wrap_continues_cycling
STEPS:
  1. Send alarm assigned to user1
  2. Trigger escalation 4 times
VERIFIES: alarm.assigned_user_id == user2_id, escalation_count == 4
PURPOSE: Ensures wrap-around cycling continues indefinitely (2+ full rounds).
---
```

### CLASS: TestEscalationWithClock

```
TEST: test_no_escalation_before_delay
STEPS:
  1. Send alarm assigned to user1
  2. Advance clock 13 minutes (threshold is 15 min)
  3. Refresh heartbeats, wait for escalation loop tick
VERIFIES: alarm.assigned_user_id == user1_id, escalation_count == 0
PURPOSE: Ensures escalation does not fire before the configured delay.
---
TEST: test_escalation_after_delay
STEPS:
  1. Send alarm assigned to user1
  2. Advance clock 16 minutes
  3. Refresh heartbeats, wait for escalation loop tick
VERIFIES: alarm.assigned_user_id == user2_id, escalation_count == 1
PURPOSE: Ensures escalation fires after the configured delay.
---
TEST: test_escalation_exactly_at_boundary
STEPS:
  1. Send alarm assigned to user1
  2. Advance clock exactly 15 minutes
  3. Refresh heartbeats, wait for escalation loop tick
VERIFIES: alarm.assigned_user_id == user2_id, escalation_count == 1
PURPOSE: Ensures escalation fires at exactly the boundary (>=).
---
```

### CLASS: TestWatchdog

```
TEST: test_user_heartbeat
STEPS:
  1. Login as user1
  2. POST /api/devices/register with device token
  3. POST /api/devices/heartbeat
VERIFIES: Both return status_code == 200
PURPOSE: Validates device registration and heartbeat submission.
---
TEST: test_watchdog_detects_offline
STEPS:
  1. Login as user2, send heartbeat
  2. POST /api/test/simulate-watchdog-failure
  3. GET /api/users/
VERIFIES: user2.is_online is False
PURPOSE: Ensures the watchdog marks users offline when heartbeats stop.
---
TEST: test_heartbeat_recent_shows_seconds
STEPS:
  1. Login as user1, send heartbeat, wait 1 second
  2. GET /api/users/
VERIFIES: user1.is_online is True, last_heartbeat is within 10 seconds of now
PURPOSE: Ensures heartbeat timestamps are accurate and recent.
---
```

### CLASS: TestWebInterface

```
TEST: test_send_test_alarm
STEPS:
  1. Reset alarms
  2. POST /api/test/send-alarm
VERIFIES: status_code == 200, json status == "sent"
PURPOSE: Validates the test alarm sending endpoint works.
---
TEST: test_simulate_watchdog
STEPS:
  1. POST /api/test/simulate-watchdog-failure
VERIFIES: status_code == 200
PURPOSE: Validates the watchdog failure simulation endpoint.
---
TEST: test_simulate_connection_loss
STEPS:
  1. POST /api/test/simulate-connection-loss
VERIFIES: status_code == 200
PURPOSE: Validates the connection loss simulation endpoint.
---
TEST: test_reset_all
STEPS:
  1. POST /api/test/reset
VERIFIES: status_code == 200
PURPOSE: Validates the full state reset endpoint.
---
TEST: test_system_status_has_connected_users
STEPS:
  1. GET /api/test/status
VERIFIES: status_code == 200, response contains "users", "connected_users", "alarms"
PURPOSE: Validates the system status endpoint returns required fields.
---
TEST: test_toggle_heartbeat_pause
STEPS:
  1. POST /api/test/toggle-heartbeat-pause (first call)
  2. POST /api/test/toggle-heartbeat-pause (second call)
VERIFIES: First call returns paused == True, second returns paused == False
PURPOSE: Validates heartbeat pause toggle works correctly.
---
```

### CLASS: TestEscalationSkipOffline

```
TEST: test_escalation_skips_offline_user
STEPS:
  1. Set everyone offline, bring user1 and admin back online (user2 stays offline)
  2. Verify user2 is offline
  3. Send alarm assigned to user1
  4. Trigger escalation
VERIFIES: alarm.assigned_user_id == admin_id (skipped user2 who is offline)
PURPOSE: Ensures escalation skips offline users in the chain.
---
TEST: test_escalation_all_offline_wraps_to_first_online
STEPS:
  1. Set everyone offline except user1
  2. Send alarm assigned to user1
  3. Trigger escalation
VERIFIES: escalated == 0 (no one else is online to escalate to)
PURPOSE: Prevents infinite loop when all other users are offline.
---
```

### CLASS: TestDeletedUserDuringAlarm

```
TEST: test_alarm_reassigned_when_user_deleted
STEPS:
  1. Create temporary user "tempuser"
  2. Send alarm assigned to tempuser
  3. DELETE tempuser
  4. GET /api/alarms/active
VERIFIES: alarm.assigned_user_id != temp_id and is not None
PURPOSE: Ensures alarms are reassigned when the assigned user is deleted.
---
```

### CLASS: TestEmptyEscalationChainAlert

```
TEST: test_alarm_with_empty_chain_sends_email
STEPS:
  1. Delete all escalation rules
  2. Verify chain is empty
  3. Send an alarm
  4. GET /api/test/last-email-sent
VERIFIES: email.sent is True, to contains "direction_technique@charlesmurgat.com", subject mentions "escalade" or "chaine"
PURPOSE: Ensures an alert email is sent when an alarm arrives with an empty escalation chain.
---
TEST: test_email_recipient_is_configurable
STEPS:
  1. POST /api/config/system with key="alert_email", value="custom@example.com"
  2. GET /api/config/system
VERIFIES: alert_email == "custom@example.com"
PURPOSE: Ensures the alert email recipient is configurable.
---
```

### CLASS: TestBackendResilience

```
TEST: test_ack_nonexistent_alarm
STEPS:
  1. Login as user1
  2. POST /api/alarms/99999/ack
VERIFIES: status_code == 404
PURPOSE: Ensures acknowledging a non-existent alarm returns 404.
---
TEST: test_resolve_nonexistent_alarm
STEPS:
  1. POST /api/alarms/99999/resolve
VERIFIES: status_code == 404
PURPOSE: Ensures resolving a non-existent alarm returns 404.
---
TEST: test_login_empty_fields
STEPS:
  1. POST /api/auth/login with empty name and password
VERIFIES: status_code in (401, 422)
PURPOSE: Ensures empty login fields are rejected.
---
TEST: test_send_alarm_missing_fields
STEPS:
  1. POST /api/alarms/send with empty JSON body
VERIFIES: status_code == 422
PURPOSE: Ensures alarm creation validates required fields.
---
TEST: test_heartbeat_with_invalid_token
STEPS:
  1. POST /api/devices/heartbeat with garbage Bearer token
VERIFIES: status_code == 401
PURPOSE: Ensures invalid JWT tokens are rejected on heartbeat.
---
TEST: test_mine_with_invalid_token
STEPS:
  1. GET /api/alarms/mine with garbage Bearer token
VERIFIES: status_code == 401
PURPOSE: Ensures invalid JWT tokens are rejected on alarm list.
---
```

### CLASS: TestTokenAutoRenewal

```
TEST: test_refresh_token_endpoint_exists
STEPS:
  1. Login as user1, get token
  2. POST /api/auth/refresh with the token
VERIFIES: status_code == 200, new access_token != old token
PURPOSE: Validates the token refresh endpoint returns a new distinct token.
---
```

### CLASS: TestPersistenceAfterCrash

```
TEST: test_data_persists_after_restart
STEPS:
  1. Reset alarms, count users
  2. Send alarm "Persistence Test"
  3. Verify alarm exists
  4. docker compose restart backend
  5. Wait for backend to be ready
  6. Verify user count unchanged and alarm still exists
VERIFIES: len(users_after) == user_count_before, alarm "Persistence Test" still exists
PURPOSE: Ensures data (users and alarms) survives a Docker backend restart.
---
```

### CLASS: TestEmailViaMailhog

```
TEST: test_empty_chain_sends_real_email
STEPS:
  1. Clear Mailhog inbox
  2. Delete all escalation rules
  3. Send alarm "Mailhog Test"
  4. Wait 2 seconds
  5. GET Mailhog API /api/v2/messages
VERIFIES: total >= 1, subject mentions "escalade"/"chaine", to contains "direction_technique@charlesmurgat.com"
PURPOSE: Validates real SMTP email delivery via Mailhog when escalation chain is empty.
---
```

### CLASS: TestCumulativeEscalation

```
TEST: test_escalated_alarm_still_visible_to_first_user
STEPS:
  1. Login user1 and user2, send heartbeats
  2. Send alarm (goes to user1)
  3. Verify user1 sees it
  4. Advance clock 16 min, send fresh heartbeats
  5. Wait for escalation loop
  6. Check /alarms/mine for both users
VERIFIES: user2 sees alarm (len >= 1), user1 STILL sees alarm (len >= 1)
PURPOSE: Ensures escalation is cumulative — previous users keep seeing the alarm.
---
TEST: test_any_notified_user_can_acknowledge
STEPS:
  1. Send alarm (goes to user1)
  2. Advance clock 16 min (escalate to user2)
  3. User1 acknowledges the alarm
VERIFIES: status_code == 200, status == "acknowledged"
PURPOSE: Ensures any previously notified user can acknowledge, not just the current assignee.
---
TEST: test_alarm_shows_notified_users
STEPS:
  1. Send alarm (goes to user1)
  2. Check notified_user_ids contains user1
  3. Trigger escalation
  4. Check notified_user_ids contains both user1 and user2
VERIFIES: user1_id in notified_user_ids (before and after), user2_id in notified_user_ids (after escalation)
PURPOSE: Validates the notified_user_ids field tracks all notified users cumulatively.
---
```

### CLASS: TestOnCallDisconnectionAlarm

```
TEST: test_oncall_offline_15min_creates_alarm
STEPS:
  1. Set user1 offline, keep user2 and admin online
  2. Advance clock 16 minutes, refresh heartbeats
  3. Wait for watchdog tick
  4. GET /api/alarms/active
VERIFIES: An alarm with "astreinte" in title exists, assigned to user2 or user2 in notified_user_ids
PURPOSE: Ensures an automatic alarm is created when the on-call user (position 1) is offline for 15+ minutes.
---
TEST: test_oncall_alarm_auto_resolves_on_reconnection
STEPS:
  1. Set user1 offline, keep user2 online
  2. Advance clock 16 min (creates on-call alarm)
  3. Bring user1 back online (send heartbeat)
  4. Wait for tick
  5. GET /api/alarms/active
VERIFIES: No alarm with "astreinte" in title remains active
PURPOSE: Ensures the on-call disconnection alarm auto-resolves when the user reconnects.
---
TEST: test_oncall_alarm_escalates_normally
STEPS:
  1. Set user1 offline, keep user2 and admin online
  2. Advance clock 16 min (creates on-call alarm)
  3. Trigger escalation manually
VERIFIES: On-call alarm has escalation_count >= 1
PURPOSE: Ensures the on-call alarm follows normal escalation rules.
---
TEST: test_no_oncall_alarm_if_not_position1
STEPS:
  1. Set user2 offline, keep user1 and admin online
  2. Advance clock 20 minutes
  3. Wait for tick
VERIFIES: No alarm with "astreinte" in title exists
PURPOSE: Ensures only position 1 (on-call) user triggers the disconnection alarm, not other positions.
---
TEST: test_nobody_connected_sends_email
STEPS:
  1. Clear Mailhog inbox
  2. Set everyone offline
  3. Advance clock 16 minutes
  4. Wait for tick
  5. Check Mailhog messages
VERIFIES: total >= 1, subject mentions "connect" or "astreinte"
PURPOSE: Ensures an email alert is sent when absolutely no one is connected.
---
```

### CLASS: TestNotifiedUsersVisibility

```
TEST: test_alarm_response_contains_notified_user_names
STEPS:
  1. Login as user1
  2. Send alarm
  3. GET /api/alarms/mine as user1
VERIFIES: "notified_user_names" field exists in alarm, contains "user1"
PURPOSE: Validates /alarms/mine returns human-readable notified user names for the mobile app.
---
```

### CLASS: TestAckedAlarmVisibility

```
TEST: test_acked_alarm_visible_to_other_notified_user
STEPS:
  1. Create alarm assigned to user1
  2. Trigger escalation (adds user2 to notified)
  3. User1 acknowledges
  4. GET /api/alarms/mine as user2
VERIFIES: User2 sees alarm with status == "acknowledged" and acknowledged_by_name == "user1"
PURPOSE: Ensures notified users can see alarms acknowledged by others.
---
TEST: test_ack_remaining_seconds_in_response
STEPS:
  1. Create alarm, acknowledge it
  2. GET /api/alarms/mine, check ack_remaining_seconds (~1800)
  3. Advance clock 10 minutes
  4. GET /api/alarms/mine, check ack_remaining_seconds (~1200)
VERIFIES: First check: 1750 <= remaining <= 1810. After 10 min: 1150 <= remaining <= 1210
PURPOSE: Validates the countdown field tracks remaining suspension time accurately.
---
TEST: test_acked_alarm_visible_to_acker_too
STEPS:
  1. Create alarm assigned to user1
  2. User1 acknowledges
  3. GET /api/alarms/mine as user1
VERIFIES: Alarm visible with status == "acknowledged" and ack_remaining_seconds not None
PURPOSE: Ensures the acknowledging user also sees the acknowledged alarm in /mine.
---
```

### CLASS: TestSmsAndHealth

```
TEST: test_health_endpoint_returns_ok
STEPS:
  1. GET /health
VERIFIES: status_code == 200, status == "ok", db is True, escalation_loop is True
PURPOSE: Validates the enriched health endpoint returns full system status.
---
TEST: test_health_endpoint_returns_503_if_loop_stalled
STEPS:
  1. POST /api/test/simulate-loop-stall
  2. GET /health
VERIFIES: status_code == 503, status == "degraded", escalation_loop is False
PURPOSE: Ensures health check detects a stalled escalation loop.
---
TEST: test_sms_pending_requires_gateway_key
STEPS:
  1. GET /internal/sms/pending without X-Gateway-Key header
VERIFIES: status_code == 401
PURPOSE: Ensures SMS gateway endpoint requires authentication.
---
TEST: test_sms_pending_wrong_key_returns_401
STEPS:
  1. GET /internal/sms/pending with wrong X-Gateway-Key
VERIFIES: status_code == 401
PURPOSE: Ensures wrong gateway key is rejected.
---
TEST: test_sms_pending_returns_empty_with_key
STEPS:
  1. GET /internal/sms/pending with correct key
VERIFIES: status_code == 200, response == []
PURPOSE: Validates empty SMS queue returns empty list.
---
TEST: test_sms_written_to_queue_on_escalation
STEPS:
  1. Set phone number for user1
  2. Send alarm assigned to user1
  3. Advance clock 16 minutes
  4. Refresh all heartbeats
  5. Wait for escalation loop (22s)
  6. GET /internal/sms/pending
VERIFIES: len(pending) >= 1, "+33600000001" in to_numbers
PURPOSE: Ensures an SMS is enqueued for users with phone numbers upon escalation.
---
TEST: test_sms_marked_sent
STEPS:
  1. Insert test SMS via /api/test/insert-sms
  2. POST /internal/sms/{id}/sent
  3. GET /internal/sms/pending
VERIFIES: SMS id not in pending list after marking sent
PURPOSE: Validates SMS can be marked as sent and removed from pending queue.
---
TEST: test_sms_marked_error
STEPS:
  1. Insert test SMS
  2. POST /internal/sms/{id}/error with error "MODEM_BUSY"
VERIFIES: retries == 1, error == "MODEM_BUSY", SMS still in pending (retries < 3)
PURPOSE: Validates SMS error reporting increments retry counter.
---
TEST: test_sms_excluded_after_max_retries
STEPS:
  1. Insert test SMS
  2. POST /internal/sms/{id}/error 3 times
  3. GET /internal/sms/pending
VERIFIES: SMS id not in pending (retries >= 3 max)
PURPOSE: Ensures SMS is excluded from pending after max retries.
---
```

### CLASS: TestRedundancy

```
TEST: test_both_backends_respond_to_health
STEPS:
  1. GET /health on backend1 (port 8000)
  2. GET /health on backend2 (port 8001)
VERIFIES: Both return status_code == 200, db is True
PURPOSE: Validates both backends are alive and connected to the database.
---
TEST: test_exactly_one_node_is_primary
STEPS:
  1. GET /health on all 3 backend URLs
  2. Count nodes with role == "primary"
VERIFIES: Exactly 1 primary in the cluster
PURPOSE: Ensures Patroni + etcd maintain a single leader at all times.
---
TEST: test_leadership_failover_when_primary_stops
STEPS:
  1. Find primary, create alarm on it
  2. docker compose stop the entire primary node
  3. Wait up to 60s for a new primary to emerge
  4. Verify alarm persists on new primary
  5. Restart the old primary (cleanup)
VERIFIES: new_primary_url is not None, alarm accessible on new primary
PURPOSE: Validates automatic failover when the primary node dies (DB + backend + etcd).
---
TEST: test_alarm_created_on_backend1_visible_on_backend2
STEPS:
  1. POST /api/alarms/send on backend1
  2. GET /api/alarms/ on backend2
VERIFIES: alarm_id exists in backend2's alarm list
PURPOSE: Validates data replication — alarms are visible across all backends.
---
TEST: test_alarm_resolved_on_backend1_visible_on_backend2
STEPS:
  1. Create alarm on backend1
  2. Resolve it on backend1
  3. GET /api/alarms/ on backend2
VERIFIES: Alarm status == "resolved" on backend2
PURPOSE: Validates resolve status is replicated across backends.
---
TEST: test_ack_on_backend1_visible_on_backend2
STEPS:
  1. Create alarm on backend1
  2. Acknowledge on backend1
  3. GET /api/alarms/ on backend2
VERIFIES: Alarm status == "acknowledged" on backend2
PURPOSE: Validates acknowledgement is replicated across backends.
---
TEST: test_user_list_consistent_across_backends
STEPS:
  1. GET /api/users/ on backend1 and backend2
  2. Compare sorted user ID lists
VERIFIES: ids_b1 == ids_b2
PURPOSE: Ensures user data is consistent across all backends.
---
TEST: test_token_from_backend1_works_on_backend2
STEPS:
  1. Login on backend1, get token
  2. GET /api/alarms/mine on backend2 with that token
VERIFIES: status_code == 200
PURPOSE: Ensures JWT tokens are cross-compatible (same SECRET_KEY).
---
TEST: test_token_from_backend2_works_on_backend1
STEPS:
  1. Login on backend2, get token
  2. GET /api/alarms/mine on backend1 with that token
VERIFIES: status_code == 200
PURPOSE: Ensures JWT cross-compatibility in the reverse direction.
---
TEST: test_heartbeat_on_backend2_visible_on_backend1
STEPS:
  1. Simulate connection loss (all offline)
  2. Send heartbeat on the primary
  3. Check user1 is_online from another backend
VERIFIES: user1.is_online is True on the other backend
PURPOSE: Ensures heartbeat state is replicated across backends.
---
TEST: test_sms_queue_visible_from_both_backends
STEPS:
  1. Insert SMS via backend1
  2. GET /internal/sms/pending on both backends
VERIFIES: sms_id present in both backends' pending lists
PURPOSE: Ensures SMS queue is visible from all backends (shared DB).
---
TEST: test_sms_marked_sent_on_backend2_disappears_from_backend1
STEPS:
  1. Insert SMS via primary
  2. Mark as sent via primary
  3. Check /pending on another backend
VERIFIES: sms_id not in other backend's pending list
PURPOSE: Ensures SMS state changes replicate across backends.
---
TEST: test_no_duplicate_sms_from_escalation
STEPS:
  1. Set phone number for user1
  2. Send alarm, advance clock 16 min on all nodes
  3. Wait for escalation
  4. Check /internal/sms/pending
VERIFIES: Exactly 1 SMS for user1's phone number (not duplicated)
PURPOSE: Validates anti-duplicate guard prevents double SMS from concurrent nodes.
---
```

### CLASS: TestDatabaseReplication

```
TEST: test_standby_is_in_recovery_mode
STEPS:
  1. Execute SQL on standby: SELECT pg_is_in_recovery()
VERIFIES: Result == "t" (true)
PURPOSE: Confirms the standby is running as a hot standby in recovery mode.
---
TEST: test_primary_is_not_in_recovery
STEPS:
  1. Execute SQL on primary: SELECT pg_is_in_recovery()
VERIFIES: Result == "f" (false)
PURPOSE: Confirms the primary is not in recovery mode.
---
TEST: test_alarm_replicates_to_standby
STEPS:
  1. Create alarm via API
  2. Wait 3 seconds
  3. Query standby DB for the alarm
VERIFIES: COUNT(*) == 1 for the alarm on standby
PURPOSE: Validates streaming replication delivers alarm data to the standby.
---
TEST: test_alarm_resolution_replicates
STEPS:
  1. Create alarm, resolve it
  2. Wait 3 seconds
  3. Query standby for alarm status
VERIFIES: status == "resolved" on standby
PURPOSE: Validates resolve status replicates to standby.
---
TEST: test_heartbeat_replicates_to_standby
STEPS:
  1. Login and send heartbeat
  2. Wait 3 seconds
  3. Query standby: SELECT is_online FROM users WHERE name='user1'
VERIFIES: Result == "t" (true)
PURPOSE: Validates user online status replicates to standby.
---
TEST: test_replication_lag_is_negligible
STEPS:
  1. Create 5 alarms rapidly (resolve each to avoid single alarm constraint)
  2. Wait 5 seconds
  3. Compare alarm count on primary vs standby
VERIFIES: count_standby == count_primary
PURPOSE: Ensures replication lag is under 5 seconds for burst writes.
---
TEST: test_standby_rejects_direct_writes
STEPS:
  1. Attempt INSERT on standby via docker exec psql
VERIFIES: returncode != 0, error contains "read-only" or "recovery"
PURPOSE: Validates standby rejects writes, protecting against split-brain.
---
TEST: test_promotion_promotes_standby_to_primary
STEPS:
  1. Stop the primary DB
  2. pg_ctl promote on standby
  3. Wait 3 seconds
  4. Check pg_is_in_recovery is false
  5. Attempt a write on promoted standby
  6. Cleanup: restart primary, rebuild standby from scratch
VERIFIES: pg_is_in_recovery == "f" after promotion, write succeeds
PURPOSE: Validates standby can be promoted to primary and accept writes (disaster recovery).
---
```

### CLASS: TestClusterEndpoint

```
TEST: test_cluster_endpoint_returns_200
STEPS:
  1. GET /api/cluster
VERIFIES: status_code == 200
PURPOSE: Validates the cluster info endpoint exists and responds.
---
TEST: test_cluster_response_has_members
STEPS:
  1. GET /api/cluster
VERIFIES: "members" in response, len(members) >= 1
PURPOSE: Validates cluster endpoint returns member list.
---
TEST: test_cluster_members_have_required_fields
STEPS:
  1. GET /api/cluster
  2. Check each member object
VERIFIES: Each member has "name", "role", "state", "api_url"
PURPOSE: Validates cluster member data structure.
---
TEST: test_cluster_has_exactly_one_leader
STEPS:
  1. GET /api/cluster
  2. Filter members with role == "leader"
VERIFIES: Exactly 1 leader
PURPOSE: Ensures cluster reports a single leader.
---
TEST: test_cluster_reports_local_node
STEPS:
  1. GET /api/cluster
VERIFIES: "local_node" and "local_role" present in response
PURPOSE: Validates cluster endpoint reports the local node identity.
---
TEST: test_cluster_reports_quorum_status
STEPS:
  1. GET /api/cluster
VERIFIES: quorum.total >= 1, quorum.healthy >= 1, "has_quorum" exists
PURPOSE: Validates quorum reporting in the cluster endpoint.
---
TEST: test_cluster_available_on_all_backends
STEPS:
  1. GET /api/cluster on all 3 backend URLs
VERIFIES: All return status_code == 200
PURPOSE: Ensures cluster endpoint works on replicas too, not just the primary.
---
TEST: test_web_ui_has_cluster_tab
STEPS:
  1. GET / (root page)
VERIFIES: "Cluster" and "clusterMembers" in HTML
PURPOSE: Ensures the web UI includes the cluster monitoring tab.
---
```

### CLASS: TestHeartbeatFailover

```
TEST: test_heartbeat_survives_leader_death
STEPS:
  1. Login and send heartbeat on primary
  2. Verify connected_users >= 1
  3. docker compose stop the primary node
  4. Wait for new primary (up to 60s)
  5. Send heartbeat on new primary with same JWT token
  6. Verify connected_users >= 1 on new primary
  7. Restart old primary
VERIFIES: Heartbeat accepted on new primary (status 200), connected_users >= 1
PURPOSE: Validates heartbeat continuity after leader failover with the same JWT.
---
TEST: test_heartbeat_on_replica_returns_503
STEPS:
  1. Find a replica backend
  2. Login on primary, get token
  3. POST heartbeat to replica
VERIFIES: status_code == 503, detail contains "replica"
PURPOSE: Ensures replicas reject heartbeat writes and return a clear error.
---
```

### CLASS: TestAndroidHeartbeatFailover

```
TEST: test_android_heartbeat_survives_leader_death
STEPS:
  1. Find a working emulator with network
  2. Login via API, inject SharedPrefs, launch app
  3. Wait for heartbeats (connected_users >= 1 on primary)
  4. docker compose stop the primary node
  5. Wait for new primary
  6. Wait for app to switch heartbeats to new primary
  7. Verify heartbeats persist (3 checks over 15 seconds)
  8. Restart old primary
VERIFIES: heartbeat_resumed is True, persistent_count >= 2 out of 3 checks
PURPOSE: Validates the real Android app automatically switches heartbeat target after leader failover.
---
```

### CLASS: TestEscalationDelayGlobal

```
TEST: test_global_delay_endpoint_returns_current_value
STEPS:
  1. GET /api/config/escalation-delay
VERIFIES: status_code == 200, minutes == 15
PURPOSE: Validates the global escalation delay endpoint returns the default value.
---
TEST: test_global_delay_can_be_updated
STEPS:
  1. POST /api/config/escalation-delay with minutes=10
  2. GET /api/config/escalation-delay
VERIFIES: Both return minutes == 10
PURPOSE: Validates the global escalation delay is updatable.
---
TEST: test_global_delay_rejects_below_1
STEPS:
  1. POST /api/config/escalation-delay with minutes=0.5
VERIFIES: status_code == 422
PURPOSE: Ensures minimum delay validation (>= 1 minute).
---
TEST: test_global_delay_rejects_above_60
STEPS:
  1. POST /api/config/escalation-delay with minutes=61
VERIFIES: status_code == 422
PURPOSE: Ensures maximum delay validation (<= 60 minutes).
---
TEST: test_escalation_uses_global_delay
STEPS:
  1. Set global delay to 5 minutes
  2. Send alarm assigned to user1
  3. Advance clock 6 minutes, refresh heartbeats
  4. Wait for 2 escalation ticks (22s)
VERIFIES: alarm.assigned_user_id == user2_id
PURPOSE: Validates escalation honors the global delay setting instead of per-position delays.
---
```

### CLASS: TestEscalationChainBulk

```
TEST: test_save_escalation_chain_replaces_all
STEPS:
  1. POST /api/config/escalation/bulk with user_ids=[admin, user1]
  2. GET /api/config/escalation
VERIFIES: len(chain) == 2, chain[0].user_id == admin, chain[1].user_id == user1
PURPOSE: Validates bulk save replaces the entire escalation chain.
---
TEST: test_save_escalation_chain_rejects_duplicate_user
STEPS:
  1. POST /api/config/escalation/bulk with user_ids=[user1, user1]
VERIFIES: status_code == 422
PURPOSE: Prevents duplicate users in the escalation chain.
---
TEST: test_save_escalation_chain_rejects_empty
STEPS:
  1. POST /api/config/escalation/bulk with user_ids=[]
VERIFIES: status_code == 422
PURPOSE: Prevents saving an empty escalation chain.
---
TEST: test_save_chain_positions_auto_numbered
STEPS:
  1. POST /api/config/escalation/bulk with user_ids=[user2, admin, user1]
  2. GET /api/config/escalation
VERIFIES: Positions are 1, 2, 3 in the order given; user IDs match
PURPOSE: Validates positions are auto-numbered sequentially.
---
```

### CLASS: TestEscalationFrontend

```
TEST: test_frontend_has_delay_input
STEPS:
  1. GET / (root page)
VERIFIES: "escalationDelay" or "escalation-delay" in HTML
PURPOSE: Ensures the web UI has an input for the global escalation delay.
---
TEST: test_frontend_has_drag_drop_chain
STEPS:
  1. GET / (root page)
VERIFIES: HTML contains "availableUsers", "escalationChain", "saveEscalation", "cancelEscalation" (or kebab-case variants)
PURPOSE: Ensures the web UI has the drag-and-drop escalation chain editor.
---
TEST: test_frontend_all_users_present
STEPS:
  1. GET /api/users/ and GET /
VERIFIES: HTML contains "loadEscalation", "availableUsers", "escalationChain"
PURPOSE: Validates the JS loads and populates both user lists dynamically.
---
```

---

## File: tests/test_frontend.py

Playwright browser tests for the web frontend. Requires the 3-node cluster running.

---

### CLASS: TestNavigation

```
TEST: test_page_loads_with_dashboard
STEPS:
  1. Navigate to primary URL
  2. Wait for network idle and stats grid
VERIFIES: Active tab text == "Tableau de bord", #dashboard is visible
PURPOSE: Ensures the dashboard loads as the default active tab.
---
TEST: test_all_tabs_clickable
STEPS:
  1. Click each of the 6 tabs: Utilisateurs, Alarmes, Escalade, Tests, Cluster, Tableau de bord
  2. For each, check the corresponding panel is visible
VERIFIES: Each panel (#users, #alarms, #escalation, #tests, #cluster, #dashboard) becomes visible
PURPOSE: Validates all navigation tabs work and show the correct panel.
---
```

### CLASS: TestEscalationDragDrop

```
TEST: test_escalation_shows_chain_and_available
STEPS:
  1. Go to Escalade tab
  2. Count items in #escalationChain and #availableUsers
VERIFIES: Total items >= 3 (all users appear in one list or the other)
PURPOSE: Ensures the escalation config UI populates both lists with all users.
---
TEST: test_remove_user_from_chain
STEPS:
  1. Go to Escalade tab
  2. Count chain items (must be >= 1)
  3. Click the remove button on the first user
VERIFIES: Chain count decreased by 1, available list has >= 1 item
PURPOSE: Validates removing a user from the escalation chain via the UI.
---
TEST: test_save_escalation_sends_correct_request
STEPS:
  1. Go to Escalade tab
  2. Remove first user from chain
  3. Click Save, intercept the HTTP request
VERIFIES: Request body has "user_ids" as a list with len >= 1
PURPOSE: Validates the save button sends the correct bulk escalation API call.
---
TEST: test_cancel_escalation_reloads
STEPS:
  1. Go to Escalade tab, count chain items
  2. Remove a user
  3. Click Cancel
  4. Wait 2 seconds
VERIFIES: Chain count returns to original value
PURPOSE: Validates cancel button reloads the chain from the server, discarding edits.
---
TEST: test_autorefresh_does_not_overwrite_edits
STEPS:
  1. Go to Escalade tab
  2. Remove a user (triggers editingEscalation flag)
  3. Wait 6 seconds (longer than 5s autorefresh interval)
VERIFIES: Chain count stays at edited value (not reverted by autorefresh)
PURPOSE: Ensures autorefresh pauses during user edits to prevent data loss.
---
```

### CLASS: TestEscalationDelay

```
TEST: test_delay_input_shows_current_value
STEPS:
  1. Go to Escalade tab
  2. Read #escalationDelay input value
VERIFIES: Value == "15" (default delay)
PURPOSE: Ensures the delay input loads the current server value.
---
TEST: test_save_delay_sends_correct_request
STEPS:
  1. Go to Escalade tab
  2. Fill #escalationDelay with "10"
  3. Click "Sauvegarder le delai", intercept HTTP request
  4. Restore to 15 via API
VERIFIES: Request body has minutes == 10
PURPOSE: Validates saving a new escalation delay sends the correct API call.
---
```

### CLASS: TestAlarms

```
TEST: test_send_alarm_from_form
STEPS:
  1. Go to Alarmes tab
  2. Fill title "Test Playwright", message, severity "critical"
  3. Click "Envoyer" button
  4. Wait 2 seconds
VERIFIES: #allAlarmsTable contains text "Test Playwright"
PURPOSE: Validates the alarm creation form works end-to-end in the browser.
---
TEST: test_resolve_alarm
STEPS:
  1. Create alarm via API
  2. Wait for autorefresh (6s)
  3. Click "Resoudre" button if present
VERIFIES: (Implicit — no crash on resolve action)
PURPOSE: Validates the resolve button works in the browser UI.
---
```

### CLASS: TestCluster

```
TEST: test_cluster_panel_shows_quorum
STEPS:
  1. Go to Cluster tab
VERIFIES: #quorumBanner contains text "Quorum"
PURPOSE: Ensures the cluster panel displays quorum status.
---
TEST: test_cluster_panel_shows_members
STEPS:
  1. Go to Cluster tab
VERIFIES: #clusterMembers has >= 2 rows
PURPOSE: Ensures the cluster panel displays at least 2 cluster members.
---
```

---

## File: tests/test_manual_multi_emulator.py

Manual HA test with 3 emulators and 3 Patroni nodes. Run directly via `python tests/test_manual_multi_emulator.py`.

---

### SCENARIO: run_scenario (5 phases, not a pytest class)

```
TEST: PHASE 0 — Verification cluster
STEPS:
  1. Wait for all 3 nodes to be healthy
  2. Find the primary node
  3. Verify all 3 nodes respond to /health
  4. Reset backend state
  5. Login all 3 users (user1, user2, admin)
  6. Setup emulators in parallel (inject SharedPrefs, launch app)
  7. Wait 8 seconds for heartbeats
  8. GET /api/test/status
VERIFIES: All 3 nodes healthy, a primary exists, connected_users >= 3
PURPOSE: Validates the full cluster is operational with 3 emulators connected.
---
TEST: PHASE 1 — Alarme + verification replication
STEPS:
  1. Send alarm on primary
  2. Wait 3 seconds
  3. Check all 3 nodes see the alarm via /api/alarms/active
  4. Verify user1 sees the alarm via /alarms/mine
VERIFIES: Each node has the alarm (replication OK), user1 sees it
PURPOSE: Validates alarm creation replicates to all 3 nodes via streaming replication.
---
TEST: PHASE 2 — Kill primary
STEPS:
  1. docker compose stop the primary (DB + backend + etcd)
  2. Wait for it to be confirmed DOWN
  3. Wait for a new primary to emerge (up to 60s)
  4. Verify alarm persists on new primary
  5. Resolve alarm on new primary (write test)
  6. Create a new alarm on new primary
VERIFIES: Primary is DOWN, new primary emerges, alarm persists (same id), writes succeed on new primary
PURPOSE: Validates full failover — killing the primary transfers leadership and preserves data.
---
TEST: PHASE 3 — Rejoin
STEPS:
  1. Restart the killed node
  2. Wait for it to be healthy (up to 60s)
  3. Wait 5 seconds, check if the rejoined node sees the new alarm
VERIFIES: Killed node rejoins the cluster, sees the alarm created during failover (resync OK)
PURPOSE: Validates a crashed node can rejoin and resync data from the new primary.
---
TEST: PHASE 4 — Escalade
STEPS:
  1. Re-login all users on current primary, send heartbeats
  2. Trigger escalation twice
  3. After first escalation: alarm assigned to user2
  4. After second escalation: alarm assigned to admin
  5. Verify all 3 users see the alarm (cumulative)
  6. Resolve alarm as admin
  7. Verify user1 no longer has active alarm
VERIFIES: Escalation works on new primary (user1->user2->admin), cumulative visibility, resolution works
PURPOSE: Validates escalation works correctly after a failover event.
---
TEST: PHASE 5 — Coherence finale
STEPS:
  1. Wait 5 seconds
  2. For each of the 3 nodes, check /health and /api/alarms/active
VERIFIES: All 3 nodes have 0 active alarms (consistent state)
PURPOSE: Validates final cluster-wide consistency after all operations.
---
```

---

## File: tests/test_failback.py

Standalone failback test script. Verifies that Android apps switch from VPS2 back to VPS1 when VPS2 dies. Run directly via `python tests/test_failback.py`.

---

### SCENARIO: Failback VPS2 -> VPS1 (6 steps, not a pytest class)

```
TEST: Step 0 — Pre-flight emulator network check
STEPS:
  1. For each of 3 emulators, clear adb reverse, set up port forwarding
  2. Ping 10.0.2.2 (host) from each emulator
VERIFIES: At least 2 emulators have working network connectivity
PURPOSE: Ensures enough emulators are functional before running the test.
---
TEST: Step 1 — Ensure both VPS UP
STEPS:
  1. docker compose start backend on both VPS1 and VPS2
  2. Wait for both to be healthy
  3. POST /api/test/reset on VPS1
VERIFIES: VPS1 healthy, VPS2 healthy
PURPOSE: Establishes the baseline with both backends running.
---
TEST: Step 2 — Setup apps
STEPS:
  1. For each working emulator: force stop app, login, inject prefs, launch app
  2. Wait 10 seconds for heartbeats
  3. Check each emulator shows connected status
VERIFIES: Apps launched and (ideally) connected
PURPOSE: Sets up the Android apps on emulators with valid auth tokens.
---
TEST: Step 3 — Force apps onto VPS2
STEPS:
  1. docker compose stop backend on VPS1
  2. Wait 15 seconds
  3. Check emulators are connected (to VPS2)
VERIFIES: Apps have switched to VPS2 after VPS1 went down
PURPOSE: Forces the apps to connect to VPS2 as the only available backend.
---
TEST: Step 4 — Bring VPS1 back
STEPS:
  1. docker compose start backend on VPS1
  2. Wait for VPS1 healthy
VERIFIES: VPS1 is healthy again
PURPOSE: Restores VPS1 to prepare for the failback test.
---
TEST: Step 5 — THE REAL TEST: Kill VPS2, apps must failback to VPS1
STEPS:
  1. Create alarm on VPS1
  2. docker compose stop backend on VPS2
  3. Verify VPS2 is down
  4. Monitor for up to 60 seconds: check connected_users on VPS1 every 5 seconds
  5. Success when connected_users >= number of working emulators
VERIFIES: connected_users >= len(working_emus) on VPS1 within 60 seconds
PURPOSE: The core failback test — validates that Android apps automatically reconnect to VPS1 when VPS2 dies.
---
TEST: Step 6 — Cleanup
STEPS:
  1. POST /api/test/reset on VPS1
  2. Restart VPS2 backend
VERIFIES: (cleanup, no assertions)
PURPOSE: Restores the environment to a clean state after the test.
---
```

---

## Summary

| File | Classes/Scenarios | Test Count |
|------|-------------------|------------|
| tests/test_e2e.py | 22 classes | 92 tests |
| tests/test_frontend.py | 5 classes | 11 tests |
| tests/test_manual_multi_emulator.py | 1 scenario | 6 phases |
| tests/test_failback.py | 1 scenario | 7 steps |

**Total: 116 test methods/phases**
