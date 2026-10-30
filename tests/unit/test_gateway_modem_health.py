"""Unit test (tier 1) — sante du lien serie gateway <-> modem (`gateway/modem_health.py`).

Regression de l'incident onsite-2 2026-09-12 07:11 → 2026-10-13 : le modem
SIM7600 s'est re-enumere sur l'USB (ttyUSB4 disparu, AT passe sur ttyUSB3).
La gateway a garde l'ancien port : `Input/output error` chaque seconde pendant
un mois, 45 212 logs "tentative de reconnexion" sans aucune reconnexion.
`send_at_command` avale l'erreur et renvoie "" :
  - SMS → CMGF_FAILED (abandonnes apres 3 retries) ;
  - appels → "no_answer" alors qu'aucun appel n'etait parti.

Charge uniquement `gateway/modem_health.py` (aucun import) → pas de dependance
pyserial/numpy en tier 1, meme pattern que test_gateway_locking.py.
"""
import importlib.util
import pathlib

import pytest

pytestmark = pytest.mark.unit

_PATH = pathlib.Path(__file__).resolve().parents[2] / "gateway" / "modem_health.py"
_spec = importlib.util.spec_from_file_location("gateway_modem_health", _PATH)
_mh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mh)


class TestCallWasDialed:
    """Un appel n'est compte "passe" que si le modem a accepte ATD (OK)."""

    def test_ok_means_dialed(self):
        assert _mh.call_was_dialed("OK") is True

    @pytest.mark.parametrize("response", [
        "",               # port serie mort : send_at_command renvoie ""
        "ERROR",
        "+CME ERROR: 3",
        "NO CARRIER",
    ])
    def test_anything_else_is_not_dialed(self, response):
        assert _mh.call_was_dialed(response) is False


class TestKeepaliveExit:
    """Le keepalive (AT toutes les 60s) doit faire sortir la gateway quand le
    modem ne repond plus, pour que systemd (Restart=always) la relance et
    re-detecte le port. Un echec isole ne suffit pas (modem occupe)."""

    def test_ok_resets_failures(self):
        assert _mh.at_failures_after("OK", 1) == 0

    def test_no_answer_increments_failures(self):
        assert _mh.at_failures_after("", 0) == 1
        assert _mh.at_failures_after("", 1) == 2

    def test_single_failure_does_not_exit(self):
        assert _mh.at_failures_after("", 0) < _mh.MAX_AT_FAILURES

    def test_consecutive_failures_reach_exit_threshold(self):
        failures = 0
        for _ in range(_mh.MAX_AT_FAILURES):
            failures = _mh.at_failures_after("", failures)
        assert failures >= _mh.MAX_AT_FAILURES


# --- INV-128 / INV-129 : etat reseau du modem (reponses AT reelles d'onsite-2) ---

CPSI_LTE = ("+CPSI: LTE,Online,208-01,0x0262,11174659,41,EUTRAN-BAND20,6400,3,3,"
            "-160,-1157,-826,9\nOK")
CPSI_NONE = "+CPSI: NO SERVICE,Online\nOK"


class TestParseNetwork:
    """Lecture de AT+CREG? / AT+CPSI? / AT+CSQ, telles que renvoyees par le SIM7600."""

    def test_lte_registered(self):
        st = _mh.parse_network("+CREG: 0,1\nOK", CPSI_LTE, "+CSQ: 15,99\nOK")
        assert st == {"registered": True, "tech": "LTE", "rsrp": -115.7, "csq": 15}

    def test_roaming_counts_as_registered(self):
        assert _mh.parse_network("+CREG: 0,5\nOK", CPSI_LTE, "+CSQ: 15,99\nOK")["registered"] is True

    @pytest.mark.parametrize("creg", ["+CREG: 0,0\nOK", "+CREG: 0,2\nOK", ""])
    def test_not_registered(self, creg):
        st = _mh.parse_network(creg, CPSI_NONE, "+CSQ: 99,99\nOK")
        assert st == {"registered": False, "tech": None, "rsrp": None, "csq": 0}

    def test_csq_99_means_unknown_zero(self):
        assert _mh.parse_network("+CREG: 0,1\nOK", CPSI_LTE, "+CSQ: 99,99\nOK")["csq"] == 0

    def test_wcdma_has_no_rsrp(self):
        cpsi = "+CPSI: WCDMA,Online,208-01,0x2905,23481260,WCDMA 900,505,3000,0,20.0,107,65532,11,500\nOK"
        st = _mh.parse_network("+CREG: 0,1\nOK", cpsi, "+CSQ: 18,99\nOK")
        assert st == {"registered": True, "tech": "WCDMA", "rsrp": None, "csq": 18}


class TestReregisterDue:
    """INV-128 : apres 5 keepalives (≈5 min) sans reseau, UNE tentative de
    reinscription (radio coupee/rallumee) par episode."""

    def test_not_before_threshold(self):
        assert _mh.reregister_due(_mh.UNREGISTERED_CHECKS_BEFORE_RECOVERY - 1, tried=False) is False

    def test_at_threshold(self):
        assert _mh.reregister_due(_mh.UNREGISTERED_CHECKS_BEFORE_RECOVERY, tried=False) is True

    def test_only_once_per_episode(self):
        assert _mh.reregister_due(_mh.UNREGISTERED_CHECKS_BEFORE_RECOVERY + 3, tried=True) is False


class TestSendsAllowed:
    """INV-145 : la gateway ne tente un SMS/appel que si le modem repond a AT et est
    inscrit sur un reseau. Sinon l'essai echouerait a coup sur et brulerait un des
    3 essais (INV-064) : alarme 886 du 2026-10-21, SMS abandonne en 1 min 30 modem muet."""

    def test_ready_modem_sends(self):
        assert _mh.sends_allowed(at_failures=0, registered=True) is True

    def test_unregistered_modem_does_not_send(self):
        assert _mh.sends_allowed(at_failures=0, registered=False) is False

    def test_mute_modem_does_not_send(self):
        assert _mh.sends_allowed(at_failures=1, registered=True) is False


class TestPcmRebootAllowed:
    """INV-169 : canal audio bloque -> redemarrage du modem (AT+CFUN=1,1) au plus une fois
    par 30 min. Encore bloque dans la fenetre -> pas de redemarrage (repli TTS), sinon
    la gateway relancee par systemd redemarrerait le modem en boucle."""

    NOW = 1_800_000_000.0

    def test_window_is_30_min(self):
        assert _mh.PCM_REBOOT_MIN_INTERVAL_S == 30 * 60

    def test_never_rebooted(self):
        assert _mh.pcm_reboot_allowed(None, self.NOW) is True

    @pytest.mark.parametrize("ago", [0, 60, 29 * 60])
    def test_rebooted_recently_is_refused(self, ago):
        assert _mh.pcm_reboot_allowed(self.NOW - ago, self.NOW) is False

    @pytest.mark.parametrize("ago", [30 * 60, 3600])
    def test_rebooted_long_ago_is_allowed(self, ago):
        assert _mh.pcm_reboot_allowed(self.NOW - ago, self.NOW) is True

    def test_timestamp_in_the_future_does_not_block_forever(self):
        # Horloge recalee en arriere : la trace ne doit pas bloquer l'auto-reparation des heures.
        assert _mh.pcm_reboot_allowed(self.NOW + 3600, self.NOW) is True


class TestProbeOrder:
    """INV-174 : le dernier backend primary connu est sonde en premier ; les autres suivent dans
    l'ordre configure (NODE1_URL..NODE5_URL), sans doublon."""

    URLS = ["http://10.99.0.3:8000", "http://10.99.0.2:8000", "http://10.99.0.1:8000"]

    def test_cached_second_comes_first_others_keep_their_order(self):
        assert _mh.probe_order("http://10.99.0.2:8000", self.URLS) == [
            "http://10.99.0.2:8000", "http://10.99.0.3:8000", "http://10.99.0.1:8000"]

    def test_cached_already_first_is_unchanged(self):
        assert _mh.probe_order("http://10.99.0.3:8000", self.URLS) == self.URLS

    def test_no_cache_keeps_configured_order(self):
        assert _mh.probe_order(None, self.URLS) == self.URLS

    def test_cached_url_no_longer_configured_is_ignored(self):
        assert _mh.probe_order("http://10.99.0.9:8000", self.URLS) == self.URLS

    def test_no_duplicate(self):
        order = _mh.probe_order("http://10.99.0.1:8000", self.URLS)
        assert len(order) == len(set(order)) == len(self.URLS)


class TestHttpTimeoutDefault:
    """INV-174 : 3 s par noeud (10 s avant : un noeud mort en tete coutait 10 s par requete)."""

    def test_default_is_3_seconds(self, monkeypatch):
        monkeypatch.delenv("HTTP_TIMEOUT_SECONDS", raising=False)
        spec = importlib.util.spec_from_file_location("gateway_config", _PATH.parent / "config.py")
        cfg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cfg)
        assert cfg.HTTP_TIMEOUT_SECONDS == 3
