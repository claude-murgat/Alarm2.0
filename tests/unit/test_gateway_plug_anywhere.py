"""Unit test (tier 1) — INV-183 : la passerelle attend un modem absent sans sortir, et lit le
contact sec (Arduino) sans modem, Arduino branchable et debranchable a chaud.

Faits du 2026-11-05 : sur alarm-1 sans modem, `main()` journalisait « Modem non detecte. Abandon. »
et rendait 1 ; systemd la relancait toutes les 10 s jusqu'a `failed`. Le contact sec n'etait
demarre qu'APRES la detection du modem : un Arduino sur une machine sans modem n'etait jamais lu.

Le vrai `main()` de `gateway/modem_gateway.py` tourne ici ; seuls le port serie (pyserial absent du
tier 1 : faux module minimal), les threads, le backend et les attentes sont remplaces.
"""
import importlib.util
import logging
import pathlib
import sys
import threading
import types

import pytest

# no_mutation : ne touche pas backend/app/logic.
pytestmark = [pytest.mark.unit, pytest.mark.no_mutation]

GW = pathlib.Path(__file__).resolve().parents[2] / "gateway"
_LOCAL = ("config", "locks", "modem_detect", "modem_health", "dry_contact", "voice_call")


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
                               modem=lambda: (None, None), ser=_Ser())

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

    def comports():
        st.events.append("scan")
        return st.ports()

    def detect(manual_port=None):
        st.events.append("scan")
        return st.modem()

    monkeypatch.setattr(sys, "argv", ["modem_gateway.py"])
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
    assert reports == ["closed"]  # Arduino branche a chaud : lu sans redemarrer la passerelle
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING and "HostDryContact" in r.message]
    assert len(warnings) == 2, [r.message for r in warnings]  # absent au depart, puis debranche
