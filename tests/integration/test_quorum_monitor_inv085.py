"""Tier 2 intégration : INV-085 — orchestrateur quorum_monitor

Vérifie le câblage du tick `_run_quorum_check` :
1. transition is_lost=False → True envoie l'email initial + persist
   `quorum_state.email_sent_at`.
2. transition is_lost=True → False reset la persistance complète
   (`lost_since`, `email_sent_at`, `reminders_sent_at`).
3. à 1h05 après l'email initial avec quorum toujours perdu, le reminder 1h
   est envoyé + ajouté à `reminders_sent_at`.
4. anti-doublon initial : sur 2 ticks consécutifs avec quorum perdu et email
   déjà envoyé, le 2ème tick ne renvoie PAS l'email.

`send_alert_email` est patché par `unittest.mock.patch` pour vérifier
explicitement le nombre d'appels et leur destinataire (l'adresse
`alert_email` de SystemConfig). Pas de Mailhog en tier 2 (réservé tier 3
avec cluster réel + chaos etcd).
"""
import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration


_ALERT_EMAIL = "direction.technique@charlesmurgat.test"
_NOW = datetime(2026, 5, 28, 14, 0, 0)


def _set_alert_email(client, admin_headers):
    """Pose `alert_email` dans SystemConfig (lu par send_alert_email destinataire)."""
    r = client.post(
        "/api/config/system",
        json={"key": "alert_email", "value": _ALERT_EMAIL},
        headers=admin_headers,
    )
    assert r.status_code == 200, f"set alert_email failed: {r.status_code} {r.text}"


def _reset_quorum_state(db):
    """Remet le singleton quorum_state (id=1) à neuf entre 2 tests."""
    from backend.app.models import QuorumStateRow

    row = db.query(QuorumStateRow).filter(QuorumStateRow.id == 1).first()
    if row is None:
        row = QuorumStateRow(id=1, lost_since=None, email_sent_at=None, reminders_sent_at="[]")
        db.add(row)
    else:
        row.lost_since = None
        row.email_sent_at = None
        row.reminders_sent_at = "[]"
    db.commit()


def _read_quorum_state(db):
    """Snapshot DB de la singleton row (lecture fraîche)."""
    from backend.app.models import QuorumStateRow

    db.expire_all()
    return db.query(QuorumStateRow).filter(QuorumStateRow.id == 1).first()


def _no_quorum_snapshot(ts):
    """Snapshot non-sain (has_quorum=False)."""
    from backend.app.logic.quorum_detection import ClusterSnapshot
    return ClusterSnapshot(has_quorum=False, patroni_reachable=True, timestamp=ts)


def _healthy_snapshot(ts):
    """Snapshot sain."""
    from backend.app.logic.quorum_detection import ClusterSnapshot
    return ClusterSnapshot(has_quorum=True, patroni_reachable=True, timestamp=ts)


def _unhealthy_history(now, minutes):
    """`minutes` snapshots non-sains, un par minute, jusqu'à now-1min (inclus).
    Couvre la fenêtre anti-flapping > 3 min pour déclencher is_lost=True."""
    return [_no_quorum_snapshot(now - timedelta(minutes=m)) for m in range(minutes, 0, -1)]


def test_initial_email_sent_on_quorum_loss_transition(client, admin_headers):
    """INV-085 #80 : sur un tick où `evaluate_quorum_loss` déclare is_lost=True
    pour la 1ère fois (email_sent_at en DB = NULL), l'orchestrateur DOIT :
    1. appeler `send_alert_email(to=alert_email)` 1 fois
    2. persister `email_sent_at = now` dans quorum_state singleton
    3. persister `lost_since = début de la série non-saine` dans quorum_state

    Sans ce câblage, INV-085 (C, 🐛) reste 🐛 : la détection pure tourne mais
    aucun opérateur n'est notifié, le système se grippe silencieusement.
    """
    from backend.app.database import SessionLocal
    from backend.app.quorum_monitor import _run_quorum_check

    _set_alert_email(client, admin_headers)
    db = SessionLocal()
    try:
        _reset_quorum_state(db)

        # snapshot non-sain depuis 4 min continues → evaluate_quorum_loss
        # déclare is_lost=True (boundary > 3 min franchie).
        snapshot = _no_quorum_snapshot(_NOW)
        history = _unhealthy_history(_NOW, minutes=4)

        with patch("backend.app.quorum_monitor.send_alert_email") as mock_send:
            _run_quorum_check(db, snapshot, history, now=_NOW)

        # 1) email envoyé exactement 1 fois, vers alert_email
        assert mock_send.call_count == 1, (
            f"INV-085 #80 : transition is_lost=True doit déclencher 1 email "
            f"initial. send_alert_email appelé {mock_send.call_count} fois "
            f"(attendu 1). Sans wiring orchestrateur, INV-085 reste 🐛."
        )
        # Vérifier le destinataire : SystemConfig.alert_email
        call = mock_send.call_args
        # Format conventionnel send_alert_email(subject, body, to)
        to_param = call.kwargs.get("to")
        if to_param is None and len(call.args) >= 3:
            to_param = call.args[2]
        assert to_param == _ALERT_EMAIL, (
            f"INV-085 #80 : destinataire doit être alert_email "
            f"({_ALERT_EMAIL!r}), got {to_param!r}. Sinon l'email part dans le vide."
        )

        # 2) persistance email_sent_at
        row = _read_quorum_state(db)
        assert row is not None, "INV-085 : singleton quorum_state.id=1 manquante"
        assert row.email_sent_at is not None, (
            "INV-085 #80 : email_sent_at doit être persisté après l'envoi. "
            "Sinon le tick suivant renverra un 2e email (spam)."
        )

        # 3) persistance lost_since (= début de la série non-saine)
        assert row.lost_since is not None, (
            "INV-085 #80 : lost_since doit être persisté quand is_lost=True. "
            "Sinon impossible de tracer la durée de l'incident."
        )
    finally:
        db.close()


def test_quorum_recovery_resets_state(client, admin_headers):
    """INV-085 #80 reset : après résolution (transition is_lost=True → False),
    l'orchestrateur DOIT :
    1. reset `lost_since`, `email_sent_at`, `reminders_sent_at` à NULL/[]
    2. NE PAS envoyer d'email de résolution (spec : aucun email sur reprise)

    Sans le reset, l'incident suivant serait considéré comme la continuation
    du précédent (pas de nouvel email initial) → 1 seule alerte par vie du
    backend → faille silencieuse.
    """
    from backend.app.database import SessionLocal
    from backend.app.models import QuorumStateRow
    from backend.app.quorum_monitor import _run_quorum_check

    _set_alert_email(client, admin_headers)
    db = SessionLocal()
    try:
        _reset_quorum_state(db)

        # Setup : incident en cours, email déjà envoyé il y a 5 min
        row = db.query(QuorumStateRow).filter(QuorumStateRow.id == 1).first()
        row.lost_since = _NOW - timedelta(minutes=10)
        row.email_sent_at = _NOW - timedelta(minutes=5)
        row.reminders_sent_at = "[]"
        db.commit()

        # Tick avec snapshot sain → evaluate_quorum_loss déclare is_lost=False
        snapshot = _healthy_snapshot(_NOW)
        history = []  # peu importe, snapshot sain suffit

        with patch("backend.app.quorum_monitor.send_alert_email") as mock_send:
            _run_quorum_check(db, snapshot, history, now=_NOW)

        # 1) aucun email envoyé (pas d'email "résolution")
        assert mock_send.call_count == 0, (
            f"INV-085 : retour à sain ne doit PAS déclencher d'email "
            f"(spec : aucune notification de résolution). "
            f"send_alert_email appelé {mock_send.call_count} fois."
        )

        # 2) reset complet du singleton
        row = _read_quorum_state(db)
        assert row.lost_since is None, (
            f"INV-085 reset : lost_since doit être NULL après reprise, "
            f"got {row.lost_since!r}. Sinon l'incident suivant est considéré "
            f"comme la continuation."
        )
        assert row.email_sent_at is None, (
            f"INV-085 reset : email_sent_at doit être NULL après reprise, "
            f"got {row.email_sent_at!r}. Sinon prochain incident n'enverra "
            f"aucun email initial (anti-doublon le bloquerait)."
        )
        assert row.reminders_sent_at == "[]", (
            f"INV-085 reset : reminders_sent_at doit être '[]' après reprise, "
            f"got {row.reminders_sent_at!r}. Sinon les fenêtres reminders "
            f"déjà envoyées persistent sur le prochain incident."
        )
    finally:
        db.close()


def test_reminder_1h_sent_when_due(client, admin_headers):
    """INV-085 #81 : sur un tick à T0+1h après l'email initial avec quorum
    toujours perdu, le reminder 1h DOIT être envoyé ET persisté dans
    `reminders_sent_at`.

    Sans persistance, le reminder serait renvoyé à chaque tick (60s) entre
    1h et 3h → ~120 emails inutiles par fenêtre → casse l'invariant
    anti-spam.
    """
    from backend.app.database import SessionLocal
    from backend.app.models import QuorumStateRow
    from backend.app.quorum_monitor import _run_quorum_check

    _set_alert_email(client, admin_headers)
    db = SessionLocal()
    try:
        _reset_quorum_state(db)

        # Setup : incident en cours depuis 1h05, email initial à T0 = NOW-1h
        t0 = _NOW - timedelta(hours=1)
        row = db.query(QuorumStateRow).filter(QuorumStateRow.id == 1).first()
        row.lost_since = t0 - timedelta(minutes=5)  # début incident T0-5min
        row.email_sent_at = t0
        row.reminders_sent_at = "[]"  # aucun reminder envoyé encore
        db.commit()

        # Tick : quorum toujours perdu, on est à T0+1h pile
        snapshot = _no_quorum_snapshot(_NOW)
        history = _unhealthy_history(_NOW, minutes=4)

        with patch("backend.app.quorum_monitor.send_alert_email") as mock_send:
            _run_quorum_check(db, snapshot, history, now=_NOW)

        # 1) email reminder 1h envoyé
        assert mock_send.call_count == 1, (
            f"INV-085 #81 : à T0+1h, le reminder 1h doit être envoyé. "
            f"send_alert_email appelé {mock_send.call_count} fois (attendu 1)."
        )

        # 2) reminders_sent_at contient maintenant la fenêtre 1h (3600 secondes)
        row = _read_quorum_state(db)
        sent = json.loads(row.reminders_sent_at)
        assert 3600 in sent, (
            f"INV-085 #81 : après envoi du reminder 1h, reminders_sent_at "
            f"doit contenir 3600 (1h en secondes). Got {sent!r}. Sans cette "
            f"persistance, le reminder repart au prochain tick → spam."
        )

        # 3) email_sent_at INCHANGÉ (le reminder ne reset pas le 1er email)
        assert row.email_sent_at == t0, (
            f"INV-085 #81 : envoi d'un reminder ne doit PAS modifier "
            f"email_sent_at (sinon le calcul des fenêtres suivantes décale). "
            f"Attendu {t0}, got {row.email_sent_at}."
        )
    finally:
        db.close()


def test_no_duplicate_initial_email_on_consecutive_ticks(client, admin_headers):
    """INV-085 #80 anti-doublon : sur 2 ticks consécutifs avec quorum toujours
    perdu (60s d'écart), l'email initial ne doit partir QU'UNE FOIS.

    Régression cible : un cron qui appelle `_run_quorum_check` toutes les 60s
    enverrait 60 emails/h sans cette garde — équivaut à un déni de service
    de la boîte mail de la direction technique. C'est l'inverse de
    l'invariant anti-spam.
    """
    from backend.app.database import SessionLocal
    from backend.app.quorum_monitor import _run_quorum_check

    _set_alert_email(client, admin_headers)
    db = SessionLocal()
    try:
        _reset_quorum_state(db)

        snapshot1 = _no_quorum_snapshot(_NOW)
        history1 = _unhealthy_history(_NOW, minutes=4)

        with patch("backend.app.quorum_monitor.send_alert_email") as mock_send:
            # Tick 1 : transition is_lost=True → email envoyé
            _run_quorum_check(db, snapshot1, history1, now=_NOW)
            assert mock_send.call_count == 1, (
                f"Sanity : tick 1 doit envoyer 1 email initial, "
                f"got {mock_send.call_count}"
            )

            # Tick 2 : 60s plus tard, quorum TOUJOURS perdu
            now2 = _NOW + timedelta(seconds=60)
            snapshot2 = _no_quorum_snapshot(now2)
            history2 = _unhealthy_history(now2, minutes=5)  # 5 min continues

            _run_quorum_check(db, snapshot2, history2, now=now2)

            # Toujours 1 appel total — pas de 2e email initial
            assert mock_send.call_count == 1, (
                f"INV-085 #80 anti-doublon : 2 ticks consécutifs avec quorum "
                f"perdu ne doivent envoyer QU'UN email initial. send_alert_email "
                f"appelé {mock_send.call_count} fois après tick 2 (attendu 1). "
                f"Régression : sans le filtre `email_sent_at is None`, le cron "
                f"60s envoie 60 emails/h."
            )
    finally:
        db.close()
