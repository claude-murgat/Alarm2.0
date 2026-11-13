"""Unit test (tier 1) — INV-183 : la passerelle attend un modem absent sans sortir, et lit le
contact sec (Arduino) sans modem, Arduino branchable et debranchable a chaud.

Faits du 2026-11-05 : sur alarm-1 sans modem, `main()` journalisait « Modem non detecte. Abandon. »
et rendait 1 ; systemd la relancait toutes les 10 s jusqu'a `failed`. Le contact sec n'etait
demarre qu'APRES la detection du modem : un Arduino sur une machine sans modem n'etait jamais lu.

Le vrai `main()` de `gateway/modem_gateway.py` tourne ici ; seuls le port serie (pyserial absent du
tier 1 : faux module minimal), les threads, le backend et les attentes sont remplaces.

INV-194 (passerelle sans Arduino : `no_sensor`) et INV-198 (Arduino programme s'il est muet, arme apres un
contact lu ferme) : vrai `HostDryContactMonitorThread` face a un faux Arduino.
"""
import importlib.util
import logging
import pathlib
import shutil
import sys
import threading
import types

import pytest

# no_mutation : ne touche pas backend/app/logic.
pytestmark = [pytest.mark.unit, pytest.mark.no_mutation]

GW = pathlib.Path(__file__).resolve().parents[2] / "gateway"
_LOCAL = ("config", "locks", "modem_detect", "modem_health", "dry_contact", "voice_call", "gsm7")


def _load():
    """Charge modem_gateway.py sans laisser ses modules (noms generiques), le faux pyserial ni son
    logging.basicConfig (niveau et handler du logger racine) aux autres tests."""
    root = logging.getLogger()
    root_state = (root.level, list(root.handlers))
    stubs = {}
    if importlib.util.find_spec("serial") is None:
        serial = types.ModuleType("serial")
        serial.Serial = object
        serial.SerialException = serial.SerialTimeoutException = OSError
        tools, lp = types.ModuleType("serial.tools"), types.ModuleType("serial.tools.list_ports")
        lp.comports = lambda: []
        serial.tools, tools.list_ports = tools, lp
        stubs = {"serial": serial, "serial.tools": tools, "serial.tools.list_ports": lp}
    before = set(sys.modules)
    sys.modules.update(stubs)
    sys.path.insert(0, str(GW))
    try:
        spec = importlib.util.spec_from_file_location("gateway_modem_gateway", GW / "modem_gateway.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        root.setLevel(root_state[0])
        root.handlers[:] = root_state[1]
        sys.path.remove(str(GW))
        for name in (*_LOCAL, *stubs):
            if name not in before:
                sys.modules.pop(name, None)


mg = _load()
HostDryContact = mg.HostDryContactMonitorThread  # la vraie classe (la fixture la remplace)

SIMCOM_AT = types.SimpleNamespace(device="/dev/ttyUSB2", vid=0x1E0E, location="1-3:1.2")


class _Stop(BaseException):
    """Arrete une boucle infinie depuis une attente (hors de portee des `except Exception`)."""


class _Ser:
    closed = False

    def close(self):
        self.closed = True


class _NoWaitEvent(threading.Event):
    def wait(self, timeout=None):
        return False


@pytest.fixture
def gw(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="modem_gateway")
    st = types.SimpleNamespace(events=[], started=[], sleeps=[], sleep_limit=20, ports=lambda: [],
                               modem=lambda: (None, None), ser=_Ser(), on_sleep=lambda n: None)

    def fake_thread(name):
        class T:
            def __init__(self, *a, **kw):
                self.name, self.running = name, True

            def start(self):
                st.started.append(name)
                st.events.append(f"start:{name}")
        return T

    for name in ("HostDryContactMonitorThread", "DryContactMonitorThread", "SmsSenderThread",
                 "CallSenderThread", "SmsReceiverThread", "HealthMonitorThread"):
        monkeypatch.setattr(mg, name, fake_thread(name))

    def sleep(seconds):
        if len(st.sleeps) >= st.sleep_limit:
            raise _Stop
        st.sleeps.append(seconds)
        st.on_sleep(len(st.sleeps))

    def comports():
        st.events.append("scan")
        return st.ports()

    def detect(manual_port=None):
        st.events.append("scan")
        return st.modem()

    monkeypatch.setattr(sys, "argv", ["modem_gateway.py"])
    monkeypatch.setattr(mg, "GATEWAY_KEY", "cle-de-prod")
    monkeypatch.setattr(mg, "MODEM_AT_PORT", "")
    monkeypatch.setattr(mg, "DRY_CONTACT_ENABLED", True)
    monkeypatch.setattr(mg, "DRY_CONTACT_SOURCE", "host")
    monkeypatch.setattr(mg.time, "sleep", sleep)
    monkeypatch.setattr(mg, "list_ports", types.SimpleNamespace(comports=comports), raising=False)
    monkeypatch.setattr(mg, "detect_modem_port", detect)
    monkeypatch.setattr(mg, "send_at_command", lambda ser, cmd, timeout=2.0: "")  # modem muet en marche
    monkeypatch.setattr(mg, "_find_primary_url", lambda: None)
    monkeypatch.setattr(mg, "_post_to_backend", lambda *a, **kw: None)
    monkeypatch.setattr(mg, "pcm_reboot", _NoWaitEvent())
    return st


MODEM_THREADS = ["SmsSenderThread", "CallSenderThread", "SmsReceiverThread", "HealthMonitorThread"]


def test_no_modem_waits_without_exiting_and_reads_the_dry_contact(gw, caplog):
    with pytest.raises(_Stop):  # main() ne rend jamais la main : arretee a la 21e attente
        mg.main()
    assert gw.events[0] == "start:HostDryContactMonitorThread"  # avant la 1re recherche du modem
    assert gw.started == ["HostDryContactMonitorThread"]  # ni SMS, ni appels, ni alerte backends
    assert gw.sleeps == [15] * 20
    assert caplog.text.count("En attente du modem") == 1
    assert "Gateway operationnelle" in caplog.text  # repere attendu par alarm-gateway-sync


def test_modem_on_usb_but_mute_on_two_searches_exits_for_a_systemd_restart(gw):
    gw.ports = lambda: [SIMCOM_AT]
    assert mg.main() == 1  # INV-126 : relance comptee par le watchdog (INV-146)
    assert gw.sleeps == [15]  # 1re recherche muette : le modem demarre peut-etre encore
    assert gw.started == ["HostDryContactMonitorThread"]


def test_hot_plugged_modem_is_used_and_the_dry_contact_is_not_started_twice(gw):
    gw.ports = lambda: [SIMCOM_AT] if len(gw.sleeps) >= 2 else []
    gw.modem = lambda: ("/dev/ttyUSB2", gw.ser) if gw.ports() else (None, None)
    assert mg.main() == 1  # keepalive AT sans reponse 2 fois : sortie INV-126, inchangee
    assert gw.sleeps == [15, 15]
    assert gw.started == ["HostDryContactMonitorThread", *MODEM_THREADS]
    assert gw.ser.closed


def test_arduino_absent_plugged_then_unplugged_warns_once_per_absence(gw, monkeypatch, caplog):
    resolved = iter([None, None, None, "/dev/ttyACM0"])
    reports = []

    class Arduino:
        def __init__(self, port, baud, timeout=None):
            self.lines = [b"DC:0\r\n", b"DC:0\r\n"]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def readline(self):
            if self.lines:
                return self.lines.pop(0)
            raise OSError("device reports readiness to read but returned no data (device disconnected?)")

    monkeypatch.setattr(mg.serial, "Serial", Arduino)
    monkeypatch.setattr(mg, "_report_dry_contact_state", lambda gid, state: reports.append(state))
    gw.sleep_limit = 8
    t = HostDryContact(port="", baud=115200, gateway_id="alarm-3", normal_value=0,
                       poll_seconds=5, liveness_seconds=5)
    monkeypatch.setattr(t, "_resolve_port", lambda: next(resolved, None))
    with pytest.raises(_Stop):
        t.run()
    # Arduino branche a chaud : lu sans redemarrer la passerelle ; sans lui, `no_sensor` a chaque recherche (INV-194)
    assert reports == ["no_sensor"] * 3 + ["closed"] + ["no_sensor"] * 6
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING and "HostDryContact" in r.message]
    assert len(warnings) == 2, [r.message for r in warnings]  # absent au depart, puis debranche


# --- INV-194 : passerelle vivante sans Arduino ---

def test_no_arduino_reports_no_sensor_every_5_s(gw, monkeypatch):
    """Aucun port µC : un rapport `no_sensor` par recherche, avant chaque attente de 5 s (des le depart)."""
    events = []
    monkeypatch.setattr(mg, "_report_dry_contact_state", lambda gid, state: events.append((gid, state)))
    gw.on_sleep = lambda n: events.append("sleep")
    gw.sleep_limit = 4
    t = HostDryContact(port="", baud=115200, gateway_id="alarm-4", normal_value=0,
                       poll_seconds=5, liveness_seconds=5)
    monkeypatch.setattr(t, "_resolve_port", lambda: None)
    with pytest.raises(_Stop):
        t.run()
    assert events == [("alarm-4", "no_sensor"), "sleep"] * 4 + [("alarm-4", "no_sensor")]
    assert gw.sleeps == [5.0] * 4


def test_mute_arduino_reports_no_sensor_every_poll(gw, monkeypatch, caplog):
    """Port ouvert mais aucune ligne `DC:` (Arduino non programme, fige) : `no_sensor` a chaque poll (5 s),
    un seul avertissement."""
    reads, reports = [], []
    clock = iter(range(1000, 2000))  # readline : timeout de 1 s

    class MuteArduino:
        def __init__(self, port, baud, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def readline(self):
            reads.append(1)
            if len(reads) > 11:
                raise _Stop
            return b""

    monkeypatch.setattr(mg.serial, "Serial", MuteArduino)
    monkeypatch.setattr(mg, "time", types.SimpleNamespace(sleep=mg.time.sleep, monotonic=lambda: next(clock)))
    monkeypatch.setattr(mg, "_report_dry_contact_state", lambda gid, state: reports.append(state))
    t = HostDryContact(port="/dev/ttyACM0", baud=115200, gateway_id="alarm-2", normal_value=0,
                       poll_seconds=5, liveness_seconds=5)
    with pytest.raises(_Stop):
        t.run()
    assert reports == ["no_sensor"] * 3  # lectures a t = 1000, 1005, 1010 sur 11 secondes
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING and "HostDryContact" in r.message]
    assert len(warnings) == 1, [r.message for r in warnings]


# --- Revue PR #296 ---

def test_manual_port_is_tried_without_the_simcom_vid_filter(gw, monkeypatch, tmp_path):
    """F6 : MODEM_AT_PORT impose (lien udev, adaptateur) -> essaye des qu'il existe, sans filtre VID ;
    absent -> attente, pas de sortie."""
    port = tmp_path / "sim7600_at"
    monkeypatch.setattr(mg, "MODEM_AT_PORT", str(port))
    gw.on_sleep = lambda n: port.touch() if n == 2 else None  # branche apres 2 recherches
    gw.modem = lambda: (str(port), gw.ser) if port.exists() else (None, None)
    assert mg.main() == 1  # modem utilise puis muet en marche (keepalive) : sortie INV-126
    assert gw.sleeps == [15, 15]
    assert gw.started == ["HostDryContactMonitorThread", *MODEM_THREADS]


@pytest.mark.parametrize("key", ["", "changeme-in-prod"])
def test_missing_gateway_key_is_one_error_at_start(gw, monkeypatch, caplog, key):
    monkeypatch.setattr(mg, "GATEWAY_KEY", key)
    gw.sleep_limit = 5
    with pytest.raises(_Stop):
        mg.main()
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR and "GATEWAY_KEY" in r.message]
    assert len(errors) == 1


def test_real_gateway_key_logs_no_error(gw, caplog):
    gw.sleep_limit = 2
    with pytest.raises(_Stop):
        mg.main()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_repeated_http_errors_are_logged_once_per_10_min(monkeypatch, caplog):
    """F5 : cle refusee -> 401 sur report-state toutes les 5 s ; un message par 10 min et par appel."""
    caplog.set_level(logging.INFO, logger="modem_gateway")
    clock = [1000.0]
    monkeypatch.setattr(mg, "_http_error_logged", {}, raising=False)
    monkeypatch.setattr(mg.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(mg, "_find_primary_url", lambda: "http://10.99.0.4:8000")
    monkeypatch.setattr(mg.requests, "post", lambda *a, **kw: types.SimpleNamespace(status_code=401))
    monkeypatch.setattr(mg.requests, "get", lambda *a, **kw: types.SimpleNamespace(status_code=401))

    def http_warnings():
        return [r.message for r in caplog.records if "HTTP 401" in r.message]
    for _ in range(100):
        assert mg._post_to_backend("/internal/alarms/report-state", {"state": "closed"}) is None
        clock[0] += 5
    assert len(http_warnings()) == 1, http_warnings()
    mg._get_from_backend("/internal/sms/pending")  # autre appel : son propre message
    assert len(http_warnings()) == 2
    clock[0] += 600
    mg._post_to_backend("/internal/alarms/report-state", {"state": "closed"})
    assert len(http_warnings()) == 3


# --- INV-198 : Arduino pret a brancher (programmation automatique, armement) ---

SERIAL_NO = "33092A0C393131399E1D33354B57336D"  # numero de l'Arduino d'alarm-4 (by-id), 2026-11-13
FIRMWARE_BIN = GW / "firmware" / "dry_contact_r4" / "dry_contact_r4.ino.bin"


@pytest.fixture
def r4(gw, monkeypatch, tmp_path):
    """Faux Arduino UNO R4 Minima sur l'USB : port serie (pyserial), `list_ports`, `/sys/bus/usb/devices`
    et `dfu-util`. Horloge : 1 s par lecture du port, et la duree de chaque attente."""
    b = types.SimpleNamespace(
        usb=(0x2341, 0x0069), serial_no=SERIAL_NO, mode="app",  # "app" (port serie) ou "dfu" (bootloader seul)
        line=None,  # ligne envoyee chaque seconde par l'application (None : muet, Arduino neuf)
        at={},  # {instant: ligne} : ce que lit l'Arduino a partir de cet instant
        dfu_rc=0, t=[1000.0], limit=4000.0, until=lambda: False,
        opened=[], touches=[], flashes=[], reports=[],
        dfu_util=str(tmp_path / "dfu-util"), firmware=str(tmp_path / "dry_contact_r4.ino.bin"))
    pathlib.Path(b.dfu_util).write_text("#!/bin/sh\n")
    pathlib.Path(b.dfu_util).chmod(0o755)
    pathlib.Path(b.firmware).write_bytes(b"\x00" * 64)
    sysfs = tmp_path / "sys-bus-usb-devices"
    sysfs.mkdir()

    def set_mode(mode):
        b.mode = mode
        dev = sysfs / "1-2"
        if mode == "dfu":
            dev.mkdir(exist_ok=True)
            (dev / "idVendor").write_text("2341\n")
            (dev / "idProduct").write_text("0369\n")
        else:
            shutil.rmtree(dev, ignore_errors=True)

    def tick(seconds):
        b.t[0] += seconds
        if b.t[0] > b.limit:
            raise _Stop

    def current_line():
        line = b.line
        for since, scripted in sorted(b.at.items()):
            if b.t[0] >= since:
                line = scripted
        return line

    class Serial:
        def __init__(self, port, baud, timeout=None):
            assert port == "/dev/ttyACM0"
            if b.mode != "app":
                raise OSError(f"could not open port {port}: No such file or directory")
            if baud == 1200:  # l'UNO R4 redemarre sur son bootloader DFU
                b.touches.append(b.t[0])
                set_mode("dfu")
            else:
                b.opened.append(b.t[0])

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def close(self):
            pass

        def readline(self):
            tick(1)
            if b.mode != "app":
                raise OSError("device reports readiness to read but returned no data (device disconnected?)")
            line = current_line()
            return f"{line}\r\n".encode() if line else b""

    def comports():
        if b.mode != "app":
            return []
        return [types.SimpleNamespace(device="/dev/ttyACM0", vid=b.usb[0], pid=b.usb[1], serial_number=b.serial_no)]

    def run(cmd, **kw):
        b.flashes.append((b.t[0], cmd, kw))
        assert b.mode == "dfu", "dfu-util sans bootloader DFU"
        if b.dfu_rc:
            return types.SimpleNamespace(returncode=b.dfu_rc, stdout="", stderr="dfu-util: Error during download\n")
        set_mode("app")  # programme : l'application demarre et lit le contact (ferme)
        b.line = "DC:0"
        return types.SimpleNamespace(returncode=0, stdout="Download done.\nDone!\n", stderr="")

    def report(gateway_id, state):
        b.reports.append((b.t[0], state))
        if b.until():
            raise _Stop

    def thread():
        t = HostDryContact(port="", baud=115200, gateway_id="alarm-2", normal_value=0,
                           poll_seconds=5, liveness_seconds=5)
        t._resolve_port = lambda: "/dev/ttyACM0" if b.mode == "app" else None
        return t

    monkeypatch.setattr(mg.serial, "Serial", Serial)
    monkeypatch.setattr(mg, "list_ports", types.SimpleNamespace(comports=comports), raising=False)
    monkeypatch.setattr(mg, "subprocess", types.SimpleNamespace(run=run), raising=False)
    monkeypatch.setattr(mg, "time", types.SimpleNamespace(sleep=tick, monotonic=lambda: b.t[0]))
    monkeypatch.setattr(mg, "_report_dry_contact_state", report)
    monkeypatch.setattr(mg, "USB_DEVICES", str(sysfs), raising=False)
    monkeypatch.setattr(mg, "DFU_UTIL", b.dfu_util, raising=False)
    monkeypatch.setattr(mg, "FIRMWARE_BIN", b.firmware, raising=False)
    monkeypatch.setattr(mg, "DRY_CONTACT_ARMED_FILE", str(tmp_path / "armed-arduinos"), raising=False)
    b.set_mode, b.thread = set_mode, thread
    b.states = lambda: [s for _, s in b.reports]
    return b


def test_silent_arduino_is_programmed_then_read(r4, caplog):
    """Arduino neuf (muet) : programme 15 s apres l'ouverture du port, `no_sensor` jusque-la, puis lu."""
    r4.until = lambda: "closed" in r4.states()
    with pytest.raises(_Stop):
        r4.thread().run()
    states = r4.states()
    assert states[-1] == "closed" and set(states[:-1]) == {"no_sensor"}, states
    times = [t for t, _ in r4.reports]
    assert max(b - a for a, b in zip(times, times[1:])) <= 5  # vivante tout du long (fenetre de 15 s)
    assert len(r4.touches) == 1 and 15 <= r4.touches[0] - r4.opened[0] <= 20
    (_, cmd, kw), = r4.flashes
    assert cmd[0] == r4.dfu_util and "0x2341:0x0369" in " ".join(cmd)
    assert cmd[cmd.index("-D") + 1] == r4.firmware
    assert 0 < kw["timeout"] <= 90
    assert SERIAL_NO in caplog.text


def test_arduino_stuck_in_its_bootloader_is_programmed_directly(r4):
    """Application absente ou corrompue : bootloader DFU seul, sans port serie -> programme sans 1200 bauds."""
    r4.set_mode("dfu")
    r4.until = lambda: "closed" in r4.states()
    with pytest.raises(_Stop):
        r4.thread().run()
    assert r4.touches == [] and len(r4.flashes) == 1
    assert r4.states()[-1] == "closed" and set(r4.states()[:-1]) == {"no_sensor"}


@pytest.mark.parametrize("usb,line", [((0x1A86, 0x7523), None), ((0x2341, 0x0069), "DC:0")],
                         ids=["autre-peripherique-muet", "arduino-qui-parle"])
def test_only_a_silent_arduino_r4_is_ever_programmed(r4, usb, line):
    r4.usb, r4.line, r4.limit = usb, line, 1000 + 120
    with pytest.raises(_Stop):
        r4.thread().run()
    assert r4.touches == [] and r4.flashes == []


def test_failed_programming_is_never_retried_within_30_min(r4, caplog):
    """dfu-util en echec, Arduino reste dans son bootloader : aucun second dfu-util (ni remise a zero) avant
    30 min, puis un seul essai. Constat du 2026-11-13 : les essais enchaines ont bloque le bootloader."""
    r4.dfu_rc, r4.limit = 74, 1000 + 31 * 60 + 30
    with pytest.raises(_Stop):
        r4.thread().run()
    assert len(r4.touches) == 1
    (_, _, _), (second, _, _) = r4.flashes
    assert 30 * 60 <= second - r4.touches[0] <= 30 * 60 + 10
    errors = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 2 and "74" in errors[0], errors
    assert set(r4.states()) == {"no_sensor"}


def test_without_arduino_dfu_util_the_arduino_is_left_alone(r4, monkeypatch, caplog):
    """Prerequis absent (partie hote pas encore deployee) : pas de bascule en DFU qui laisserait l'Arduino
    sans port serie ; une ERROR."""
    monkeypatch.setattr(mg, "DFU_UTIL", str(pathlib.Path(r4.dfu_util).with_name("absent")), raising=False)
    r4.limit = 1000 + 60
    with pytest.raises(_Stop):
        r4.thread().run()
    assert r4.touches == [] and r4.flashes == []
    errors = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and "dfu-util" in errors[0], errors


def test_arduino_alarms_only_after_reading_the_contact_closed_once(r4, caplog):
    """Branche avant le cablage de son contact (lu ouvert) : `no_sensor`, un avertissement ; une fois lu ferme,
    l'Arduino est arme et un contact ouvert alarme."""
    r4.line, r4.at, r4.limit = "DC:1", {1030: "DC:0", 1040: "DC:1"}, 1050
    with pytest.raises(_Stop):
        r4.thread().run()
    states = r4.states()
    first_closed = states.index("closed")
    assert first_closed >= 5 and states[:first_closed] == ["no_sensor"] * first_closed, states
    assert states[-1] == "open"
    warnings = [r.message for r in caplog.records if "pas encore armé" in r.message]
    assert len(warnings) == 1 and SERIAL_NO in warnings[0], warnings


def test_armed_arduino_alarms_at_once_after_a_restart(r4):
    r4.line = "DC:0"
    r4.until = lambda: "closed" in r4.states()
    with pytest.raises(_Stop):
        r4.thread().run()  # arme
    r4.line, r4.reports[:] = "DC:1", []
    r4.until = lambda: True
    with pytest.raises(_Stop):
        r4.thread().run()  # passerelle relancee (ou Arduino rebranche) : nouvelle instance
    assert r4.states() == ["open"]


def test_arduino_without_serial_number_alarms_as_before(r4):
    """Rien a retenir d'une relance a l'autre : pas d'armement, fail-to-alarm comme avant INV-198."""
    r4.serial_no, r4.line = None, "DC:1"
    r4.until = lambda: True
    with pytest.raises(_Stop):
        r4.thread().run()
    assert r4.states() == ["open"]


def test_firmware_bin_is_shipped_with_the_gateway_code():
    """Meme chemin relatif dans le depot et dans /opt/alarm-gateway (image, alarm-gateway-sync, install.sh)."""
    assert pathlib.Path(mg.FIRMWARE_BIN) == FIRMWARE_BIN
    assert FIRMWARE_BIN.is_file(), f"{FIRMWARE_BIN} absent : y copier le .bin compile (cf test_dry_contact_host.py)"


def test_dfu_util_and_armed_arduinos_are_where_the_machine_provides_them():
    assert mg.DFU_UTIL == "/usr/local/lib/alarm-gateway/dfu-util"  # fork d'Arduino pose par install.sh
    assert mg.DRY_CONTACT_ARMED_FILE == "/var/lib/alarm-gateway/armed-arduinos"
    unit = (GW.parent / "infra" / "onsite" / "systemd" / "alarm-sms-gateway.service").read_text().splitlines()
    assert "StateDirectory=alarm-gateway" in unit
