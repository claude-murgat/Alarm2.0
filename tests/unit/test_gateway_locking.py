"""Unit test (tier 1) — verrou serie du gateway modem (`gateway/locks.py`).

Regression de l'incident 2026-06-17 17:47 (onsite-2) : `at_lock` etait un
`threading.Lock` non reentrant. `SmsReceiverThread.run()` detient le verrou quand
il appelle `_handle_incoming_sms`, qui re-fait `with at_lock:` pour lire le SMS
entrant → le thread se bloque sur lui-meme (self-deadlock) et gele TOUTE la
gateway des qu'un SMS entrant arrive (ack par SMS, SMS operateur, etc.).

Le fix : `at_lock = threading.RLock()` (reentrant).

Tests purs et **anti-hang** (tout en `blocking=False` : sur un Lock non reentrant
ils echouent proprement au lieu de bloquer). Charge uniquement `gateway/locks.py`
(qui n'importe que `threading`) → aucune dependance lourde (pyserial/numpy) en
tier 1.
"""
import importlib.util
import pathlib
import threading

import pytest

pytestmark = pytest.mark.unit

# Charge gateway/locks.py par chemin (pas de mutation de sys.path → sans risque
# de shadowing sous pytest-xdist / pytest-randomly).
_LOCKS_PATH = pathlib.Path(__file__).resolve().parents[2] / "gateway" / "locks.py"
_spec = importlib.util.spec_from_file_location("gateway_locks", _LOCKS_PATH)
_locks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_locks)


def _other_thread_can_acquire() -> bool:
    """True si un AUTRE thread arrive a acquerir at_lock (non-bloquant)."""
    result = []

    def _try():
        got = _locks.at_lock.acquire(blocking=False)
        result.append(got)
        if got:
            _locks.at_lock.release()

    t = threading.Thread(target=_try)
    t.start()
    t.join(timeout=5)
    return bool(result and result[0])


def test_at_lock_is_reentrant():
    """Un thread qui detient deja at_lock peut le re-acquerir (cas run() →
    _handle_incoming_sms). Avec un Lock non reentrant, la 2e acquisition
    non-bloquante renvoie False → preuve du self-deadlock qui gele la gateway."""
    at_lock = _locks.at_lock

    assert at_lock.acquire(blocking=False) is True  # simule run() qui tient le verrou
    try:
        reacquired = at_lock.acquire(blocking=False)  # simule `with at_lock:` interne
        if reacquired:
            at_lock.release()
        assert reacquired is True, (
            "at_lock non reentrant (threading.Lock) : SmsReceiver se self-deadlock "
            "des qu'un SMS entrant arrive et gele toute la gateway"
        )
    finally:
        at_lock.release()


def test_at_lock_still_excludes_other_threads_and_releases():
    """RLock ne doit PAS devenir un no-op : il garde l'exclusion mutuelle entre
    threads differents, et se libere apres usage reentrant equilibre.
    Tout en `blocking=False` → ne hang pas si at_lock redevient un Lock."""
    at_lock = _locks.at_lock

    assert at_lock.acquire(blocking=False) is True
    reentrant = at_lock.acquire(blocking=False)
    if not reentrant:
        at_lock.release()  # libere l'unique acquisition avant d'echouer (anti-hang)
        pytest.fail("at_lock non reentrant — cf test_at_lock_is_reentrant")
    try:
        # Tant que ce thread tient le verrou, un autre thread ne doit PAS l'avoir.
        assert _other_thread_can_acquire() is False, "RLock doit exclure les autres threads"
    finally:
        at_lock.release()
        at_lock.release()  # equilibre les 2 acquire

    # Verrou libre : un autre thread peut desormais l'acquerir.
    assert _other_thread_can_acquire() is True, "verrou non libere apres usage reentrant"
