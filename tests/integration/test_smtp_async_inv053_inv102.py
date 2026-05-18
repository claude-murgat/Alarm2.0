"""
Tier 2 integration tests — verrouillage en regression du fix Bug #105
(PR #106 : SMTP async via asyncio.to_thread + timeout smtplib).

Follow-up issue #111 : tests cibles, rapides (mocks ~ms) en complement
de l'E2E live `TestSmtpAsyncDoesNotBlockEventLoop` (test_e2e.py:1448).

Invariants couverts :
- INV-053 [C] : "Personne en ligne -> email direction technique". On prouve
  ici directement que `_apply_oncall_heartbeat` route l'envoi via
  `asyncio.to_thread`, donc ne peut PAS geler l'event loop.
- INV-102 [H] (indirect, hardening) : `/health 503 si escalation_loop stale`.
  Le timeout SMTP borne la latence cote socket — sans lui, un MTA injoignable
  fige le worker thread ~110s (TCP SYN default), et l'event loop reste
  responsive mais le thread pool se sature. On verrouille la presence d'un
  `timeout=` fini > 0 dans `smtplib.SMTP(...)`.

Scope strict (issue #111) :
- 0 modif code prod (les fixes sont deja en place depuis PR #106 / commit
  bbcf729). Ces tests verrouillent en regression.
- 2 tests cibles, ~20 lignes utiles. Pas de refactor _smtp_snapshot, pas de
  migration aiosmtplib, pas de cancel/shutdown leak (issues separees).

RED proof (verifiee manuellement avant commit) :
- Test 1 : commenter `await asyncio.to_thread(` a escalation.py:428 et
  appeler `send_alert_email(...)` directement -> Test 1 FAIL
  (mock_send.call_count devient 1, mock_to_thread args[0] devient autre).
- Test 2 : retirer `timeout=timeout` du `smtplib.SMTP(...)` a
  email_service.py:50 -> Test 2 FAIL (kwargs ne contient plus "timeout").
"""
import asyncio
import math
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.integration


def test_apply_oncall_heartbeat_routes_email_via_asyncio_to_thread_inv053(
    client, admin_headers, monkeypatch
):
    """INV-053 + Bug #105 : la branche "personne online -> email" doit passer
    `send_alert_email` a `asyncio.to_thread`, JAMAIS l'appeler en sync direct
    (sinon un SMTP injoignable gele l'event loop ~110s par tick).

    Trigger : tous users offline + pos1 offline > delay -> emails non vide.

    Double assertion (catch 2 mutants) :
    - mock_send.call_count == 0   -> tue le mutant `send_alert_email(...)` sync.
    - mock_to_thread.call_args.args[0] is mock_send
                                   -> tue le mutant `to_thread(sleep, 0); ...`
                                     qui passerait un simple `call_count >= 1`.
    """
    from backend.app.clock import now as clock_now
    from backend.app.database import SessionLocal
    from backend.app.escalation import _apply_oncall_heartbeat
    from backend.app.models import Alarm, EscalationConfig, User, SystemConfig

    # On veut la branche INV-053 (emails non vide), donc s'assurer que le
    # delay est celui par defaut (15 min) pour ne pas dependre d'autres tests
    # qui auraient deja modifie cette cle dans SystemConfig.
    r = client.post(
        "/api/config/system",
        json={"key": "oncall_offline_delay_minutes", "value": "15"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text

    # Issue #116 : reset marker one-shot INV-053 pour garantir qu'on est au
    # "1er tick d'episode". Sans ca, si un test precedent de la suite (ou
    # l'escalation_loop background entre runs) a deja pose le marker,
    # email_already_sent=True -> la branche INV-053 skippe l'envoi ->
    # to_thread jamais appele -> ce test fail.
    r = client.post(
        "/api/config/system",
        json={"key": "nobody_online_email_sent_at", "value": ""},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text

    db = SessionLocal()
    saved_states: list[tuple[int, bool, object]] = []
    try:
        chain = db.query(EscalationConfig).order_by(EscalationConfig.position).all()
        assert len(chain) >= 1, "seed chain manquante"

        # Nettoyer alarmes actives heritees d'un test precedent (INV-902 isolation).
        for a in db.query(Alarm).filter(Alarm.status.in_(["active", "escalated"])).all():
            a.status = "resolved"
        db.commit()

        # Mettre TOUS les users offline pour declencher la branche INV-053
        # ("personne online"). Position 1 doit etre offline depuis > 15 min.
        now_ref = clock_now()
        users = db.query(User).all()
        for u in users:
            saved_states.append((u.id, u.is_online, u.last_heartbeat))
            u.is_online = False
            u.last_heartbeat = now_ref - timedelta(minutes=20)
        db.commit()

        # Patches : on remplace les deux symboles tels qu'ils sont resolus
        # depuis le module escalation.py — c'est exactement ce que verifie le
        # code de prod (`from .email_service import send_alert_email` ligne 10
        # et `import asyncio` ligne 1, puis `await asyncio.to_thread(...)`
        # ligne 428). AsyncMock pour to_thread car le call site fait `await`.
        with patch(
            "backend.app.escalation.send_alert_email"
        ) as mock_send, patch(
            "backend.app.escalation.asyncio.to_thread",
            new_callable=AsyncMock,
        ) as mock_to_thread:
            asyncio.run(_apply_oncall_heartbeat(db, now_ref, chain))

            # P2 : assertions qui prouvent quelque chose de specifique.
            #
            # 1) send_alert_email NE doit PAS etre appele directement. Si le
            #    fix PR #106 etait revert (appel sync), call_count serait > 0.
            assert mock_send.call_count == 0, (
                f"Bug #105 regression : send_alert_email a ete appele en sync "
                f"({mock_send.call_count} fois). Il doit obligatoirement passer "
                f"par asyncio.to_thread pour ne pas geler l'event loop."
            )

            # 2) asyncio.to_thread DOIT avoir ete utilise (au moins 1 fois ;
            #    pas == 1 strict car l'escalation_loop background pourrait
            #    avoir tick durant le with — robustesse anti-flake).
            assert mock_to_thread.call_count >= 1, (
                f"INV-053 : aucun envoi route via asyncio.to_thread alors "
                f"que tous les users sont offline depuis > 15 min "
                f"(call_count={mock_to_thread.call_count}). Soit la branche "
                f"INV-053 ne s'est pas declenchee, soit le fix #106 a regresse."
            )

            # 3) Le premier argument positionnel passe a to_thread DOIT etre
            #    la fonction send_alert_email (telle que vue depuis le module
            #    escalation — donc notre mock_send). Tue le mutant
            #    `to_thread(asyncio.sleep, 0); send_alert_email(...)` qui
            #    passerait `call_count >= 1` mais pas cette assertion.
            assert mock_to_thread.call_args.args[0] is mock_send, (
                f"Bug #105 / mutant : asyncio.to_thread doit recevoir "
                f"send_alert_email en 1er argument positionnel, "
                f"got {mock_to_thread.call_args.args[0]!r}"
            )
    finally:
        # Restore deterministe pour ne pas casser les tests suivants.
        for uid, was_online, hb in saved_states:
            u = db.query(User).filter(User.id == uid).first()
            if u is not None:
                u.is_online = True
                u.last_heartbeat = clock_now()
        # Nettoyer toute alarme oncall eventuellement creee par un tick
        # concurrent de l'escalation_loop (pollution improbable mais possible).
        for a in db.query(Alarm).filter(Alarm.status.in_(["active", "escalated"])).all():
            a.status = "resolved"
        db.commit()
        db.close()


def test_send_alert_email_passes_finite_positive_timeout_to_smtp_inv102(monkeypatch):
    """Bug #105 hardening (INV-102 indirect) : `send_alert_email` doit passer
    un `timeout=` fini > 0 a `smtplib.SMTP(...)`. Sans timeout, smtplib herite
    du socket default (~110s sur Linux) — meme execute dans un thread worker
    (asyncio.to_thread, fix INV-053), ca sature le pool si le SMTP_HOST est
    injoignable.

    Tue tout mutant qui passerait `timeout=None`, `timeout=float('inf')`,
    `timeout=0`, ou ferait disparaitre l'argument.
    """
    # SMTP_HOST non-vide pour traverser le early-return du module
    # (`if not smtp_host: return` -> sinon le bloc SMTP est skippe).
    monkeypatch.setenv("SMTP_HOST", "smtp.example.invalid")
    monkeypatch.setenv("SMTP_PORT", "25")
    # Mode anonyme : pas de STARTTLS/login (sinon mock_smtp().__enter__()
    # devrait simuler ces methodes — inutile pour ce test focalise).
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASS", raising=False)

    with patch("backend.app.email_service.smtplib.SMTP") as mock_smtp:
        from backend.app.email_service import send_alert_email
        send_alert_email(
            subject="test-inv102",
            body="probe timeout argument",
            to="dest@example.invalid",
        )

    assert mock_smtp.call_count >= 1, (
        "smtplib.SMTP n'a pas ete instancie alors que SMTP_HOST est defini. "
        "Le early-return du module a ete pris ou le code de prod a change."
    )
    kwargs = mock_smtp.call_args.kwargs
    assert "timeout" in kwargs, (
        f"Bug #105 regression : send_alert_email doit passer timeout= a "
        f"smtplib.SMTP(...). Sans cet argument, le socket default Linux "
        f"(~110s) sature le pool de threads quand SMTP_HOST est injoignable. "
        f"kwargs={kwargs!r}"
    )
    timeout = kwargs["timeout"]
    assert timeout is not None, (
        "timeout=None equivaut au socket default (~110s). Doit etre une "
        "valeur finie > 0."
    )
    assert isinstance(timeout, (int, float)), (
        f"timeout doit etre numerique, got {type(timeout).__name__}={timeout!r}"
    )
    assert math.isfinite(timeout), (
        f"timeout doit etre fini (pas inf, pas nan), got {timeout!r}"
    )
    assert timeout > 0, (
        f"timeout doit etre strictement > 0 (0 = non-blocking I/O, "
        f"casse smtplib), got {timeout!r}"
    )
