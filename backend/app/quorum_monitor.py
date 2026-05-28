"""INV-085 : orchestrateur tick pour la détection de perte de quorum cluster
et l'envoi d'emails d'alerte direction technique (initial + reminders 1h/3h/6h).

Architecture (cf #80 + #81) :

1. `quorum_monitor_loop()` tourne en background asyncio (démarré dans main.py)
   - Tick toutes les `_QUORUM_TICK_SECONDS` secondes (60s par défaut).
   - Construit un `ClusterSnapshot` courant en lisant `/api/cluster` local
     (has_quorum) + en pinguant Patroni sur chaque nœud (patroni_reachable).
   - Maintient un historique des snapshots récents (>3 min, pour anti-flapping).
   - Appelle `_run_quorum_check(db, snapshot, history, now)` UNIQUEMENT sur
     le leader Patroni (cf is_current_leader) — sinon 3 backends enverraient
     3 emails identiques en cluster.

2. `_run_quorum_check` (testable unit / tier 2) :
   - Appelle `evaluate_quorum_loss(snapshot, history)` → QuorumState.
   - Lit `QuorumStateRow` (singleton id=1) : état persistant de l'incident.
   - Si `is_lost=True` ET 1er email pas envoyé : `send_alert_email` + persist
     `email_sent_at`.
   - Si `is_lost=True` ET reminder dû (cf `should_send_reminder`) :
     `send_alert_email` + persist la fenêtre dans `reminders_sent_at`.
   - Si retour à sain (`is_lost=False`) ET un incident était en cours : reset
     `lost_since`, `email_sent_at`, `reminders_sent_at`.

La séparation `_run_quorum_check` / loop permet de tester le comportement
sans démarrer la background task (tier 2 avec snapshot injecté).
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from .clock import now as clock_now
from .database import SessionLocal
from .email_service import send_alert_email
from .leader_election import is_leader
from .logic.quorum_detection import (
    ClusterSnapshot,
    QUORUM_LOSS_THRESHOLD,
    REMINDER_WINDOWS,
    evaluate_quorum_loss,
    should_send_initial_email,
    should_send_reminder,
)
from .models import QuorumStateRow, SystemConfig


logger = logging.getLogger(__name__)

_QUORUM_TICK_SECONDS = 60

# Fenêtre d'historique gardée en mémoire — doit couvrir au moins le seuil
# anti-flapping (3 min) + une marge. À 60s/tick, 5 entrées suffisent.
_HISTORY_WINDOW = QUORUM_LOSS_THRESHOLD + timedelta(minutes=2)


def _get_alert_email(db: Session) -> Optional[str]:
    """Lit `alert_email` depuis SystemConfig. None si non configuré."""
    cfg = db.query(SystemConfig).filter(SystemConfig.key == "alert_email").first()
    return cfg.value if cfg and cfg.value else None


def _get_or_create_singleton(db: Session) -> QuorumStateRow:
    """Singleton id=1. Créé si absent (cas de test avec DB sans migration init)."""
    row = db.query(QuorumStateRow).filter(QuorumStateRow.id == 1).first()
    if row is None:
        row = QuorumStateRow(
            id=1, lost_since=None, email_sent_at=None, reminders_sent_at="[]"
        )
        db.add(row)
        db.flush()
    return row


def _parse_reminders(raw: str) -> list[timedelta]:
    """Parse le JSON liste-d'int-secondes en list[timedelta]."""
    try:
        return [timedelta(seconds=s) for s in json.loads(raw or "[]")]
    except (ValueError, TypeError):
        logger.warning("quorum_state.reminders_sent_at invalide (%r), reset à []", raw)
        return []


def _dump_reminders(reminders: list[timedelta]) -> str:
    """Sérialise list[timedelta] en JSON liste-d'int-secondes."""
    return json.dumps([int(td.total_seconds()) for td in reminders])


def _send_initial_email(alert_email: str, lost_since: datetime, now: datetime) -> None:
    """INV-085 #80 : envoie le 1er email d'alerte direction technique."""
    elapsed = now - lost_since
    minutes = int(elapsed.total_seconds() / 60)
    subject = "[Alarme Murgat] Perte de quorum cluster"
    body = (
        f"Le cluster Alarme Murgat a perdu son quorum (détecté depuis {minutes} "
        f"minutes, début à {lost_since.isoformat()}).\n\n"
        f"Sans quorum, aucune écriture n'est possible — les alarmes ne peuvent "
        f"plus être créées ni acquittées. Intervention manuelle requise sur "
        f"Patroni / etcd.\n\n"
        f"Reminders programmés à 1h, 3h, 6h tant que le quorum n'est pas rétabli."
    )
    send_alert_email(subject=subject, body=body, to=alert_email)


def _send_reminder_email(
    alert_email: str, window: timedelta, lost_since: datetime, now: datetime
) -> None:
    """INV-085 #81 : envoie un reminder pour la fenêtre 1h/3h/6h."""
    hours = int(window.total_seconds() / 3600)
    elapsed = now - lost_since
    minutes = int(elapsed.total_seconds() / 60)
    subject = f"[Alarme Murgat] Rappel +{hours}h — quorum cluster toujours perdu"
    body = (
        f"Rappel ({hours}h après notification initiale) : le cluster Alarme "
        f"Murgat n'a toujours pas retrouvé son quorum (perdu depuis {minutes} "
        f"minutes, début à {lost_since.isoformat()}).\n\n"
        f"Intervention manuelle requise sur Patroni / etcd."
    )
    send_alert_email(subject=subject, body=body, to=alert_email)


def _run_quorum_check(
    db: Session,
    snapshot: ClusterSnapshot,
    history: list[ClusterSnapshot],
    now: datetime,
) -> None:
    """Orchestrateur du tick INV-085 (testable tier 2).

    Voir docstring du module pour le contrat complet. Idempotent : 2 ticks
    consécutifs avec le même état n'envoient pas 2 emails (l'anti-doublon
    est porté par `email_sent_at` + `reminders_sent_at`).
    """
    state = evaluate_quorum_loss(snapshot, history)
    row = _get_or_create_singleton(db)

    # Cas 1 : retour à sain alors qu'un incident était en cours → reset complet.
    if not state.is_lost and row.lost_since is not None:
        logger.info(
            "INV-085 : quorum retrouvé (lost_since=%s, email_sent_at=%s), reset",
            row.lost_since, row.email_sent_at,
        )
        row.lost_since = None
        row.email_sent_at = None
        row.reminders_sent_at = "[]"
        db.commit()
        return

    # Cas 2 : état nominal, rien à faire.
    if not state.is_lost:
        return

    # Cas 3 : quorum perdu — tracer lost_since + envoyer email initial ou reminder.
    if row.lost_since is None:
        row.lost_since = state.lost_since

    alert_email = _get_alert_email(db)
    if alert_email is None:
        logger.warning(
            "INV-085 : quorum perdu mais SystemConfig.alert_email vide — "
            "aucun email envoyé. Configurer alert_email pour activer l'alerte."
        )
        db.commit()
        return

    # Email initial : à envoyer SSI is_lost et email_sent_at None.
    if should_send_initial_email(state, row.email_sent_at):
        _send_initial_email(alert_email, row.lost_since, now)
        row.email_sent_at = now
        db.commit()
        return

    # Reminder : à envoyer SSI email_sent_at set et fenêtre 1h/3h/6h franchie
    # qui n'a pas encore été envoyée.
    reminders = _parse_reminders(row.reminders_sent_at)
    window = should_send_reminder(state, row.email_sent_at, reminders, now)
    if window is not None:
        _send_reminder_email(alert_email, window, row.lost_since, now)
        reminders.append(window)
        row.reminders_sent_at = _dump_reminders(reminders)
        db.commit()
        return

    # Aucun email à envoyer ce tick — persister juste lost_since si nouveau.
    db.commit()


def _build_snapshot_sync(now: datetime) -> ClusterSnapshot:
    """Lit Patroni REST API pour construire un snapshot courant.

    Reproduit la logique de `/api/cluster` (main.py:cluster_status). Bloquant ;
    appelé via `asyncio.to_thread` depuis la loop.
    """
    import urllib.request
    import json as _json

    patroni_url = os.getenv("PATRONI_URL", "http://patroni:8008")
    try:
        req = urllib.request.Request(f"{patroni_url}/cluster", method="GET")
        resp = urllib.request.urlopen(req, timeout=3)
        cluster = _json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.warning("INV-085 : Patroni injoignable depuis ce noeud (%s)", e)
        return ClusterSnapshot(
            has_quorum=False, patroni_reachable=False, timestamp=now
        )

    members = cluster.get("members", [])
    healthy = sum(1 for m in members if m.get("state") in ("running", "streaming"))
    total = len(members)
    has_quorum = total > 0 and healthy > total / 2
    return ClusterSnapshot(
        has_quorum=has_quorum, patroni_reachable=True, timestamp=now
    )


async def quorum_monitor_loop() -> None:
    """Background task INV-085 : tick toutes les `_QUORUM_TICK_SECONDS` secondes.

    Gating : SEUL LE LEADER Patroni exécute `_run_quorum_check` — sinon les 3
    backends en cluster enverraient chacun leur email (catastrophe anti-spam).
    Le leader change automatiquement à un failover (cf `leader_election`).

    L'historique des snapshots est gardé in-memory : redémarrer le backend
    repart d'un historique vide → 3 min de "warming up" avant qu'on puisse
    déclencher une alerte (acceptable : la perte de quorum persistant est
    détectée au pire 3 min + tick après le restart). La persistance
    `quorum_state.lost_since` survit aux restarts pour ne pas perdre l'incident.
    """
    history: list[ClusterSnapshot] = []
    logger.info(
        "quorum_monitor_loop démarré (tick=%ss, leader-gated, INV-085)",
        _QUORUM_TICK_SECONDS,
    )
    while True:
        try:
            await asyncio.sleep(_QUORUM_TICK_SECONDS)
        except asyncio.CancelledError:
            logger.info("quorum_monitor_loop annulé (shutdown)")
            raise

        if not is_leader.is_set():
            # Pas leader → on ne tient même pas d'historique (sera vide
            # quand on deviendra leader, warming up de 3 min). Acceptable.
            history = []
            continue

        try:
            now = clock_now()
            snapshot = await asyncio.to_thread(_build_snapshot_sync, now)

            # Maintien de l'historique : on garde uniquement les snapshots
            # postérieurs à `now - _HISTORY_WINDOW`. Ordre croissant temporel.
            cutoff = now - _HISTORY_WINDOW
            history = [s for s in history if s.timestamp >= cutoff]
            history.append(snapshot)

            db = SessionLocal()
            try:
                _run_quorum_check(db, snapshot, history[:-1], now=now)
            finally:
                db.close()
        except Exception:
            logger.exception("quorum_monitor_loop : erreur dans le tick (continue)")
