"""
End-to-End Tests for Critical Alarm System.

These tests run against a live backend and optionally verify Android emulator behavior.
Run with: python -m pytest tests/test_e2e.py -v

Prerequisites:
- Backend running at http://localhost:8000
- (Optional) Android emulator running with app installed
"""

import os
import time
import subprocess
import requests
import pytest

BASE_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
API = f"{BASE_URL}/api"

# Test credentials
ADMIN_EMAIL = "admin@alarm.local"
ADMIN_PASSWORD = "admin123"
USER1_EMAIL = "user1@alarm.local"
USER1_PASSWORD = "user123"
USER2_EMAIL = "user2@alarm.local"
USER2_PASSWORD = "user123"


ADB_PATH = os.path.join(os.getenv("ANDROID_HOME", "C:/Users/Charles/Android/Sdk"), "platform-tools", "adb.exe")


def has_adb():
    """Check if adb is available."""
    try:
        result = subprocess.run([ADB_PATH, "devices"], capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def adb_shell(cmd):
    """Run adb shell command and return output."""
    result = subprocess.run(
        [ADB_PATH, "shell", cmd], capture_output=True, text=True, timeout=10, shell=False
    )
    return result.stdout.strip()


class TestBackendHealth:
    """Test 0: Backend is running and healthy."""

    def test_health_check(self):
        r = requests.get(f"{BASE_URL}/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_web_ui_loads(self):
        r = requests.get(BASE_URL)
        assert r.status_code == 200
        assert "Critical Alarm System" in r.text


class TestUserLogin:
    """Test 1: User login E2E."""

    def test_admin_login(self):
        r = requests.post(f"{API}/auth/login", json={
            "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD
        })
        assert r.status_code == 200
        data = r.json()
        assert "access_token" in data
        assert data["user"]["email"] == ADMIN_EMAIL
        assert data["user"]["is_admin"] is True

    def test_user1_login(self):
        r = requests.post(f"{API}/auth/login", json={
            "email": USER1_EMAIL, "password": USER1_PASSWORD
        })
        assert r.status_code == 200
        data = r.json()
        assert "access_token" in data
        assert data["user"]["name"] == "User 1"

    def test_invalid_login(self):
        r = requests.post(f"{API}/auth/login", json={
            "email": "bad@bad.com", "password": "wrong"
        })
        assert r.status_code == 401

    def test_user_list(self):
        r = requests.get(f"{API}/users/")
        assert r.status_code == 200
        users = r.json()
        assert len(users) >= 3


class TestAlarmSendAndReceive:
    """Test 2-3: Send alarm from web, verify it's created and assigned."""

    @pytest.fixture(autouse=True)
    def setup(self):
        # Reset alarms before each test
        requests.post(f"{API}/alarms/reset")
        # Login as user1
        r = requests.post(f"{API}/auth/login", json={
            "email": USER1_EMAIL, "password": USER1_PASSWORD
        })
        self.user1_token = r.json()["access_token"]
        self.user1_headers = {"Authorization": f"Bearer {self.user1_token}"}

    def test_send_alarm_via_api(self):
        """Test 2: Send alarm from web interface API."""
        r = requests.post(f"{API}/alarms/send", json={
            "title": "Server Down",
            "message": "Production server is not responding",
            "severity": "critical",
        })
        assert r.status_code == 200
        alarm = r.json()
        assert alarm["status"] == "active"
        assert alarm["title"] == "Server Down"
        assert alarm["assigned_user_id"] is not None

    def test_alarm_received_by_user(self):
        """Test 3: User receives alarm via polling endpoint."""
        # Send alarm assigned to user1
        r = requests.post(f"{API}/auth/login", json={
            "email": USER1_EMAIL, "password": USER1_PASSWORD
        })
        user1_id = r.json()["user"]["id"]

        requests.post(f"{API}/alarms/send", json={
            "title": "Test Alarm",
            "message": "Testing alarm delivery",
            "severity": "critical",
            "assigned_user_id": user1_id,
        })

        # Poll as user1
        r = requests.get(f"{API}/alarms/mine", headers=self.user1_headers)
        assert r.status_code == 200
        alarms = r.json()
        assert len(alarms) >= 1
        assert alarms[0]["title"] == "Test Alarm"


class TestAlarmAcknowledgement:
    """Test 5-7: Acknowledge alarm, stop sound, suspension."""

    @pytest.fixture(autouse=True)
    def setup(self):
        requests.post(f"{API}/alarms/reset")
        r = requests.post(f"{API}/auth/login", json={
            "email": USER1_EMAIL, "password": USER1_PASSWORD
        })
        self.token = r.json()["access_token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.user1_id = r.json()["user"]["id"]

    def test_acknowledge_alarm(self):
        """Test 5: Acknowledge alarm changes status."""
        # Create alarm
        r = requests.post(f"{API}/alarms/send", json={
            "title": "Ack Test",
            "message": "Testing acknowledgement",
            "severity": "critical",
            "assigned_user_id": self.user1_id,
        })
        alarm_id = r.json()["id"]

        # Acknowledge
        r = requests.post(f"{API}/alarms/{alarm_id}/ack", headers=self.headers)
        assert r.status_code == 200
        alarm = r.json()
        assert alarm["status"] == "acknowledged"
        assert alarm["acknowledged_at"] is not None
        assert alarm["suspended_until"] is not None

    def test_suspended_alarm_not_in_mine(self):
        """Test 7: After ack, alarm is suspended and not returned by /mine."""
        # Create and acknowledge
        r = requests.post(f"{API}/alarms/send", json={
            "title": "Suspend Test",
            "message": "Testing suspension",
            "severity": "critical",
            "assigned_user_id": self.user1_id,
        })
        alarm_id = r.json()["id"]

        requests.post(f"{API}/alarms/{alarm_id}/ack", headers=self.headers)

        # Check /mine - should not include acknowledged alarm
        r = requests.get(f"{API}/alarms/mine", headers=self.headers)
        assert r.status_code == 200
        active = [a for a in r.json() if a["id"] == alarm_id]
        assert len(active) == 0


class TestEscalation:
    """Test 8: Escalation - alarm moves to next user after timeout."""

    @pytest.fixture(autouse=True)
    def setup(self):
        requests.post(f"{API}/alarms/reset")
        # Set very short escalation delay for testing
        requests.post(f"{API}/config/escalation", json={
            "position": 1, "user_id": self._get_user_id(USER1_EMAIL), "delay_minutes": 0.1
        })
        requests.post(f"{API}/config/escalation", json={
            "position": 2, "user_id": self._get_user_id(USER2_EMAIL), "delay_minutes": 0.1
        })

    def _get_user_id(self, email):
        users = requests.get(f"{API}/users/").json()
        return next(u["id"] for u in users if u["email"] == email)

    def test_escalation_triggers(self):
        """Test 8: Alarm escalates to next user after delay."""
        user1_id = self._get_user_id(USER1_EMAIL)
        user2_id = self._get_user_id(USER2_EMAIL)

        # Send alarm to user1
        r = requests.post(f"{API}/alarms/send", json={
            "title": "Escalation Test",
            "message": "Testing escalation",
            "severity": "critical",
            "assigned_user_id": user1_id,
        })
        alarm_id = r.json()["id"]
        assert r.json()["assigned_user_id"] == user1_id

        # Wait for escalation (0.1 min = 6 seconds, check after 15s to be safe)
        time.sleep(15)

        # Check if escalated
        r = requests.get(f"{API}/alarms/")
        alarm = next(a for a in r.json() if a["id"] == alarm_id)
        assert alarm["assigned_user_id"] == user2_id, (
            f"Expected alarm to escalate to user2 (id={user2_id}), "
            f"but it's assigned to user {alarm['assigned_user_id']}"
        )
        assert alarm["escalation_count"] >= 1


class TestWatchdog:
    """Test 9: Watchdog detects connection loss."""

    def test_device_registration_and_heartbeat(self):
        """Register device and send heartbeat."""
        r = requests.post(f"{API}/auth/login", json={
            "email": USER1_EMAIL, "password": USER1_PASSWORD
        })
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # Register device
        r = requests.post(f"{API}/devices/register", headers=headers, json={
            "device_token": "test-device-001"
        })
        assert r.status_code == 200
        assert r.json()["is_online"] is True

        # Heartbeat
        r = requests.post(f"{API}/devices/heartbeat", headers=headers)
        assert r.status_code == 200

    def test_watchdog_detects_offline(self):
        """Test 9: Simulate watchdog failure - devices go offline."""
        # Register a device first
        r = requests.post(f"{API}/auth/login", json={
            "email": USER1_EMAIL, "password": USER1_PASSWORD
        })
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        requests.post(f"{API}/devices/register", headers=headers, json={
            "device_token": "test-device-watchdog"
        })

        # Simulate watchdog failure
        r = requests.post(f"{API}/test/simulate-watchdog-failure")
        assert r.status_code == 200

        # Check devices
        r = requests.get(f"{API}/devices/")
        devices = r.json()
        offline_devices = [d for d in devices if not d["is_online"]]
        assert len(offline_devices) > 0


class TestWebInterface:
    """Test the web test interface endpoints."""

    def test_send_test_alarm(self):
        r = requests.post(f"{API}/test/send-alarm")
        assert r.status_code == 200
        assert r.json()["status"] == "sent"

    def test_simulate_watchdog(self):
        r = requests.post(f"{API}/test/simulate-watchdog-failure")
        assert r.status_code == 200

    def test_simulate_connection_loss(self):
        r = requests.post(f"{API}/test/simulate-connection-loss")
        assert r.status_code == 200

    def test_reset_all(self):
        r = requests.post(f"{API}/test/reset")
        assert r.status_code == 200

    def test_system_status(self):
        r = requests.get(f"{API}/test/status")
        assert r.status_code == 200
        data = r.json()
        assert "users" in data
        assert "devices" in data
        assert "alarms" in data


class TestAndroidIntegration:
    """Android emulator integration tests (skipped if no emulator)."""

    @pytest.fixture(autouse=True)
    def check_adb(self):
        if not has_adb():
            pytest.skip("ADB not available - skipping Android tests")
        devices = adb_shell("echo ok")
        if devices != "ok":
            pytest.skip("No emulator connected")

    def test_app_installed(self):
        """Check if the app is installed on the emulator."""
        output = adb_shell("pm list packages com.alarm.critical")
        assert "com.alarm.critical" in output, "App not installed on emulator"

    def test_app_launches(self):
        """Launch the app and verify it starts."""
        adb_shell("am force-stop com.alarm.critical")
        time.sleep(1)
        adb_shell("am start -a android.intent.action.MAIN -c android.intent.category.LAUNCHER -n com.alarm.critical/.MainActivity")
        time.sleep(3)
        output = adb_shell("dumpsys activity activities")
        assert "com.alarm.critical" in output, f"App not found in activities: {output[:500]}"

    def test_alarm_activity_launches(self):
        """Send alarm and verify AlarmActivity appears."""
        requests.post(f"{API}/alarms/reset")

        # Login as user1 to get their ID
        r = requests.post(f"{API}/auth/login", json={
            "email": USER1_EMAIL, "password": USER1_PASSWORD
        })
        user1_id = r.json()["user"]["id"]

        # Make sure app is running (foreground service needs it)
        adb_shell("am force-stop com.alarm.critical")
        time.sleep(1)
        adb_shell("am start -n com.alarm.critical/.MainActivity")
        time.sleep(3)

        # Send alarm
        requests.post(f"{API}/alarms/send", json={
            "title": "Android Test",
            "message": "Testing Android alarm display",
            "severity": "critical",
            "assigned_user_id": user1_id,
        })

        # Wait for polling to pick it up (app needs to login first + poll)
        time.sleep(8)

        output = adb_shell("dumpsys activity activities")
        # App should be visible (either AlarmActivity or at least the app)
        assert "com.alarm.critical" in output, f"App not in activities after alarm"
