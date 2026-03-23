# Critical Alarm Management System (Alarm 2.0)

System de gestion d'alarmes critiques avec notification mobile, sonnerie continue, acquittement temporaire, watchdog et escalade automatique.

## Architecture

```
Android App (Kotlin)  <──HTTP polling──>  FastAPI Backend
                                          ├── REST API
                                          ├── Web Admin UI (/)
                                          ├── Escalation Engine
                                          ├── Watchdog Monitor
                                          └── SQLite DB
```

## Prerequisites

- **Python 3.12+** (`winget install Python.Python.3.12`)
- **JDK 17** (`winget install EclipseAdoptium.Temurin.17.JDK`)
- **Docker Desktop** (`winget install Docker.DockerDesktop`) - requires WSL2 + reboot
- **Android SDK** - installed at `C:/Users/<user>/Android/Sdk`

## Quick Start

### 1. Backend (sans Docker)

```bash
cd backend
pip install -r requirements.txt
# Note: if bcrypt error, run: pip install bcrypt==4.0.1
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Backend accessible at http://localhost:8000

### 2. Backend (Docker Compose)

```bash
# Requires Docker Desktop running (WSL2 + reboot after first install)
docker compose up --build -d
```

### 3. Android (Emulator)

```bash
# Set environment
export ANDROID_HOME="C:/Users/<user>/Android/Sdk"
export JAVA_HOME="C:/Program Files/Eclipse Adoptium/jdk-17.0.18.8-hotspot"

# Install SDK components
sdkmanager "platform-tools" "platforms;android-34" "build-tools;34.0.0" "emulator" "system-images;android-34;google_apis;x86_64"

# Create AVD
avdmanager create avd -n alarm_test -k "system-images;android-34;google_apis;x86_64" -d pixel_6

# Start emulator
emulator -avd alarm_test -no-audio &

# Build and install APK
cd android
./gradlew assembleDebug
adb install app/build/outputs/apk/debug/app-debug.apk

# Launch app
adb shell am start -n com.alarm.critical/.MainActivity
```

### 4. Run E2E Tests

```bash
# Backend must be running on port 8000
# Emulator must be running with app installed
cd <project-root>
python -m pytest tests/test_e2e.py -v
```

## Test Accounts

| Email | Password | Role |
|-------|----------|------|
| admin@alarm.local | admin123 | Admin |
| user1@alarm.local | user123 | User (escalation position 1) |
| user2@alarm.local | user123 | User (escalation position 2) |

## Web Admin UI

Access at http://localhost:8000/

### Tabs:
- **Dashboard**: Active alarms, connected devices, stats
- **Users**: Add/remove users
- **Alarms**: Send alarms, view all alarms
- **Escalation**: Configure escalation chain and delays
- **Tests**: Send test alarm, simulate watchdog failure, simulate connection loss, reset all

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | /api/auth/login | Login (email+password) |
| POST | /api/auth/register | Register new user |
| GET | /api/alarms/mine | Get active alarms for current user |
| POST | /api/alarms/send | Send new alarm |
| POST | /api/alarms/{id}/ack | Acknowledge alarm (30min suspension) |
| POST | /api/devices/heartbeat | Send heartbeat (watchdog) |
| GET | /api/config/escalation | Get escalation chain |
| POST | /api/test/send-alarm | Send test alarm |

## Features

- **Login**: Email + password authentication with JWT tokens
- **Alarm Reception**: Android app polls every 3 seconds
- **Continuous Ringing**: System alarm sound at max volume
- **Critical Screen**: Full-screen red alarm overlay
- **Acknowledgement**: Stops sound, suspends alarm for 30 minutes
- **Watchdog**: Heartbeat every 30s, offline detection after 60s
- **Escalation**: Auto-escalate to next user after configurable delay (default 15 min)

## E2E Test Coverage

1. Backend health check
2. Web UI loads
3. Admin login
4. User login
5. Invalid login rejected
6. User list
7. Send alarm via API
8. User receives alarm via polling
9. Acknowledge alarm
10. Suspended alarm hidden from /mine
11. Escalation triggers after delay
12. Device registration + heartbeat
13. Watchdog detects offline
14. Web test: send alarm
15. Web test: simulate watchdog
16. Web test: simulate connection loss
17. Web test: reset all
18. Web test: system status
19. Android: app installed
20. Android: app launches
21. Android: alarm activity launches

## Troubleshooting

- **Docker won't start**: Requires WSL2 + system reboot after installation
- **bcrypt error**: Run `pip install bcrypt==4.0.1` (version compatibility)
- **Android emulator slow**: Use `-gpu swiftshader_indirect` flag
- **App can't connect**: Emulator uses 10.0.2.2 to reach host's localhost
- **Escalation not working**: Check backend logs for datetime errors

## Limitations

- Communication uses HTTP polling (not push notifications / FCM)
- SQLite database (not suitable for production at scale)
- No HTTPS/TLS (development only)
- Docker Compose requires WSL2 + reboot on fresh Windows install
