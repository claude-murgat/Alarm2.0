"""Unit test (tier 1) — INV-146 : le watchdog modem detecte un modem present sur l'USB
mais muet, via les redemarrages en boucle de la gateway (`scripts/modem-watchdog.py`).

Alarme 886 du 2026-10-21 : SIM7600 visible sur l'USB mais port inutilisable
(`BrokenPipeError`), gateway relancee toutes les ~90 s par systemd. Le watchdog ne
reagissait qu'a un modem absent de l'USB : aucune recuperation, ni SMS ni appel.
"""
import importlib.util
import pathlib

import pytest

pytestmark = pytest.mark.unit

_PATH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "modem-watchdog.py"
_spec = importlib.util.spec_from_file_location("modem_watchdog", _PATH)
_wd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_wd)

W = 600  # fenetre de 10 min


class TestMuteDetected:
    def test_three_restarts_in_window_is_mute(self):
        assert _wd.mute_detected([1000, 1090, 1180], now=1200) is True

    def test_two_restarts_is_not_mute(self):
        assert _wd.mute_detected([1000, 1090], now=1100) is False

    def test_old_restarts_are_ignored(self):
        assert _wd.mute_detected([1000, 1090, 1180], now=1000 + W + 1) is False

    def test_today_incident_timeline(self):
        # sorties 16:03:05, 16:04:47, 16:06:30 (en secondes depuis 16:00)
        assert _wd.mute_detected([185, 287, 390], now=400) is True


# --- INV-184 : n'agir que sur un modem deja vu, une seule recuperation PCI par episode ---

class TestAbsentAction:
    def test_never_seen_is_ignored(self):
        # Machine sans modem : ni reset PCI (Arduino du contact sec sur le meme bus), ni email.
        assert _wd.absent_action(seen=False, recovered=False) == "ignorer"
        assert _wd.absent_action(seen=False, recovered=True) == "ignorer"

    def test_first_absence_of_the_episode_recovers(self):
        assert _wd.absent_action(seen=True, recovered=False) == "recuperer"

    def test_already_recovered_in_this_episode_only_waits(self):
        assert _wd.absent_action(seen=True, recovered=True) == "attendre"


class _Stop(BaseException):
    pass


class TestMainLoop:
    """Vraie boucle `main()` ; horloge, presence du modem, recuperation et emails remplaces.
    Avant : sur une machine sans modem, PCI remove/rescan toutes les ~10 min sans fin
    (« toujours absent — nouvelle tentative de recup silencieuse »)."""

    @pytest.fixture
    def run(self, monkeypatch, tmp_path):
        def run(present, ticks=40):
            st = {"t": 0, "sleeps": 0, "recovers": 0, "emails": [], "bodies": []}

            def sleep(s):
                if st["sleeps"] >= ticks:
                    raise _Stop
                st["sleeps"] += 1
                st["t"] += s

            def recover():
                st["recovers"] += 1

            monkeypatch.setattr(_wd.time, "sleep", sleep)
            monkeypatch.setattr(_wd, "modem_present", lambda: present(st["t"]))
            monkeypatch.setattr(_wd, "recover", recover)
            monkeypatch.setattr(_wd, "send_email", lambda subject, body: (
                st["emails"].append(subject), st["bodies"].append(body)) and True)
            monkeypatch.setattr(_wd, "gateway_restarts", lambda: 0)
            monkeypatch.setattr(_wd.sys, "argv", ["modem-watchdog.py"])
            with pytest.raises(_Stop):
                _wd.main()
            return st
        monkeypatch.setattr(_wd, "SEEN_FILE", str(tmp_path / "seen"), raising=False)
        monkeypatch.setattr(_wd, "RECOVERED_FILE", str(tmp_path / "recovered"), raising=False)
        # /sys/bus/pci factice : controleur present (sauf test dedie)
        (tmp_path / "pci" / "devices" / _wd.XHCI).mkdir(parents=True)
        monkeypatch.setattr(_wd, "PCI", str(tmp_path / "pci"), raising=False)
        run.tmp = tmp_path
        return run

    def test_never_seen_modem_is_never_recovered_nor_mailed(self, run):
        st = run(lambda t: False)
        assert (st["recovers"], st["emails"]) == (0, [])

    def test_seen_then_gone_recovers_once_and_mails_once(self, run):
        st = run(lambda t: t < 120, ticks=60)
        assert st["t"] > 1800  # 30 min d'absence au moins
        assert st["recovers"] == 1
        assert len(st["emails"]) == 1 and "DOWN" in st["emails"][0]

    def test_back_then_gone_again_is_a_new_episode(self, run):
        st = run(lambda t: t < 120 or 1000 <= t < 1200, ticks=60)
        assert st["recovers"] == 2
        assert len(st["emails"]) == 2

    def test_restarted_watchdog_does_not_recover_again_in_the_same_episode(self, run):
        (run.tmp / "seen").touch()
        (run.tmp / "recovered").touch()  # recuperation deja faite avant le redemarrage
        st = run(lambda t: False)
        assert (st["recovers"], st["emails"]) == (0, [])

    # --- Revue PR #296 : mutants survivants M4/M5, F4 ---

    def test_episode_is_written_to_disk_before_waiting(self, run):
        """M4 : sans le fichier, un watchdog redemarre (redeploiement) refait une recuperation."""
        run(lambda t: t < 120, ticks=10)
        assert (run.tmp / "recovered").exists()

    def test_episode_file_is_removed_when_the_modem_is_back(self, run):
        """M5 : sinon un watchdog redemarre plus tard ignorerait la disparition suivante."""
        run(lambda t: t < 120 or t >= 1000, ticks=30)
        assert not (run.tmp / "recovered").exists()

    def test_controller_left_removed_is_rescanned_at_start(self, run):
        """F4a : watchdog tue entre `remove` et `rescan` -> modem ET Arduino coupes sans fin."""
        (run.tmp / "pci" / "devices" / _wd.XHCI).rmdir()
        run(lambda t: True, ticks=1)
        assert (run.tmp / "pci" / "rescan").read_text() == "1"

    def test_present_controller_is_not_rescanned(self, run):
        run(lambda t: True, ticks=1)
        assert not (run.tmp / "pci" / "rescan").exists()

    def test_down_email_says_the_arduino_is_affected(self, run):
        """F4b : le reset PCI coupe tout le controleur USB, Arduino du contact sec compris."""
        st = run(lambda t: t < 120, ticks=10)
        body = st["bodies"][0]
        assert "n'en depend pas" not in body
        assert "controleur USB" in body and "Arduino" in body and "contact sec de cette machine" in body


def test_smtp_settings_come_from_an_env_file_that_still_exists():
    """Jour du demenagement (INV-090) : .env.prod.node1/2 supprimes. Le watchdog lit SMTP_* dans un
    .env.prod du depot present sur toutes les machines, sinon ses emails partent « SMTP incomplet »."""
    src = _PATH.read_text()
    env = src.split("load_env('/opt/alarm/", 1)[1].split("'", 1)[0]
    assert "SMTP_HOST=" in (_PATH.parents[1] / env).read_text()
