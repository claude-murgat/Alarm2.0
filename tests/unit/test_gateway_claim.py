"""Unit test (tier 1) — INV-193 : la passerelle reserve chaque appel et chaque SMS sur le serveur,
port AT tenu, juste avant `ATD` / `AT+CMGS`, et saute l'element si la reservation est perdue.

Incident du 2026-11-12 (alarme 1231, Izeron) : alarm-4 a recompose l'appel 949 qu'alarm-3 venait de
passer, parce qu'il figurait encore dans sa liste /internal/calls/pending.

Les vrais `SmsSenderThread` / `CallSenderThread` de `gateway/modem_gateway.py` tournent ici (meme
chargement que test_gateway_plug_anywhere.py), un tour de boucle ; port serie et backend simules.
"""
import importlib.util
import logging
import pathlib
import sys
import types

import pytest

# no_mutation : ne touche pas backend/app/logic.
pytestmark = [pytest.mark.unit, pytest.mark.no_mutation]

GW = pathlib.Path(__file__).resolve().parents[2] / "gateway"
_LOCAL = ("config", "locks", "modem_detect", "modem_health", "dry_contact", "voice_call", "gsm7")


def _load():
    """Charge modem_gateway.py sans laisser ses modules (noms generiques), le faux pyserial ni son
    logging.basicConfig aux autres tests."""
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
        spec = importlib.util.spec_from_file_location("gateway_modem_gateway_claim", GW / "modem_gateway.py")
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

PHONE = "+33600000193"
CALL = {"id": 949, "to_number": PHONE, "tts_message": "Alarme.", "site": "izeron", "retries": 0}
SMS = {"id": 950, "to_number": PHONE, "body": "ALARME Izeron: test", "retries": 0}
WON = {"id": 0, "claimed": True, "claimed_by": "alarm-4"}
LOST = {"id": 0, "claimed": False, "claimed_by": "alarm-3"}


class FakeModem:
    """Faux SIM7600 : note chaque ecriture dans le journal commun ; `ATD` refuse (appel non emis,
    resultat `error` tout de suite, INV-127), SMS accepte."""

    def __init__(self, events):
        self.events, self._out = events, b""

    def write(self, data):
        data = bytes(data)
        self.events.append(("at", data.strip()))
        if data.endswith(b"\x1a"):
            self._out += b"\r\n+CMGS: 12\r\n\r\nOK\r\n"
        elif data.startswith(b"AT+CMGS="):
            self._out += b"\r\n> "
        elif data.startswith(b"ATD"):
            self._out += b"\r\nERROR\r\n"
        else:
            self._out += b"\r\nOK\r\n"
        return len(data)

    @property
    def in_waiting(self):
        return len(self._out)

    def read(self, n=1):
        data, self._out = self._out[:n], self._out[n:]
        return data

    def readline(self):
        i = self._out.find(b"\n") + 1 or len(self._out)
        line, self._out = self._out[:i], self._out[i:]
        return line

    def reset_input_buffer(self):
        self._out = b""


@pytest.fixture
def gw(monkeypatch):
    """Un tour de boucle du thread `kind` ; `claim` = reponse du serveur a /claim (None : serveur
    d'avant INV-193 ou injoignable)."""
    st = types.SimpleNamespace(events=[], claim=WON)

    def run(kind):
        thread = (mg.CallSenderThread if kind == "calls" else mg.SmsSenderThread)(FakeModem(st.events))

        def get(path):
            thread.running = False  # un seul tour
            if path.startswith("/internal/calls/pending"):
                return [CALL]
            return [SMS] if kind == "sms" else []  # INV-134 : aucun SMS ne retient l'appel

        def post(path, json_data=None):
            if path.endswith("/claim"):
                st.events.append(("claim", path, json_data, mg.at_lock._is_owned()))
                return st.claim
            st.events.append(("post", path, json_data))
            return {"status": "ok"}

        monkeypatch.setattr(mg, "_get_from_backend", get)
        monkeypatch.setattr(mg, "_post_to_backend", post)
        thread.run()
        return st.events

    monkeypatch.setattr(mg.time, "sleep", lambda s: None)
    monkeypatch.setattr(mg, "GATEWAY_ID", "alarm-4")
    mg.modem_ready.set()  # INV-145 : modem pret
    yield types.SimpleNamespace(st=st, run=run)
    mg.modem_ready.clear()


def _at(events):
    return [e[1] for e in events if e[0] == "at"]


def test_call_is_claimed_with_the_at_port_held_before_atd(gw):
    events = gw.run("calls")
    assert events == [
        ("claim", "/internal/calls/949/claim", {"gateway_id": "alarm-4"}, True),
        ("at", f"ATD{PHONE};".encode()),
        ("post", "/internal/calls/949/result", {"result": "error", "gateway_id": "alarm-4"}),
    ]


def test_sms_is_claimed_with_the_at_port_held_before_cmgs(gw):
    events = gw.run("sms")
    assert events[0] == ("claim", "/internal/sms/950/claim", {"gateway_id": "alarm-4"}, True)
    assert f'AT+CMGS="{PHONE}"'.encode() in _at(events)
    assert events[-1] == ("post", "/internal/sms/950/sent", {"gateway_id": "alarm-4"})


@pytest.mark.parametrize("kind, item", [("calls", 949), ("sms", 950)])
def test_lost_claim_skips_the_item_silently(gw, kind, item, caplog):
    gw.st.claim = LOST
    with caplog.at_level(logging.DEBUG, logger="modem_gateway"):
        events = gw.run(kind)
    assert events == [("claim", f"/internal/{kind}/{item}/claim", {"gateway_id": "alarm-4"}, True)], \
        "reservation perdue : ni commande AT, ni compte rendu"
    lines = [r for r in caplog.records if str(item) in r.getMessage()]
    assert [r.levelno for r in lines] == [logging.DEBUG], [r.getMessage() for r in lines]
    assert "alarm-3" in lines[0].getMessage()


@pytest.mark.parametrize("kind, command", [("calls", f"ATD{PHONE};"), ("sms", f'AT+CMGS="{PHONE}"')])
def test_no_answer_from_claim_still_sends(gw, kind, command):
    """Serveur pas encore a jour (404) ou injoignable : on n'empeche jamais un appel ou un SMS
    d'alarme ; seule une reponse `claimed: false` fait sauter l'element."""
    gw.st.claim = None
    events = gw.run(kind)
    assert command.encode() in _at(events)
    assert events[-1][0] == "post" and events[-1][2]["gateway_id"] == "alarm-4"
