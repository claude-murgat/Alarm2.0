"""Unit tests (tier 1) pour backend/app/logic/sms_timer.py.

Invariants couverts :
- INV-060 : SMS enqueued apres sms_call_delay_minutes sur notification
- INV-061 : pas de SMS si phone_number NULL (gere par l'appelant, pas teste ici)
- INV-062/063 : anti-doublon SMS/Call (via sms_sent/call_sent flags)

Tests purs : aucune DB, <100ms total.
"""
from datetime import datetime, timedelta

import pytest

from backend.app.logic.sms_timer import evaluate_sms_call_timers
from backend.app.logic.models import (
    AlarmSnapshot,
    CallEnqueue,
    NotificationSnapshot,
    SmsCallActions,
    SmsEnqueue,
)


NOW = datetime(2026, 4, 17, 12, 0, 0)
DELAY = 2.0  # sms_call_delay_minutes defaut


def _alarm(alarm_id: int = 1, status: str = "active") -> AlarmSnapshot:
    return AlarmSnapshot(
        id=alarm_id,
        status=status,
        created_at=NOW - timedelta(minutes=5),
        suspended_until=None,
        assigned_user_id=1,
        escalation_count=0,
        is_oncall_alarm=False,
    )


def _notif(
    notif_id: int = 1,
    alarm_id: int = 1,
    user_id: int = 1,
    notified_at: datetime | None = NOW - timedelta(minutes=3),  # elapsed=3 > delay=2
    sms_sent: bool = False,
    call_sent: bool = False,
) -> NotificationSnapshot:
    return NotificationSnapshot(
        id=notif_id,
        alarm_id=alarm_id,
        user_id=user_id,
        notified_at=notified_at,
        sms_sent=sms_sent,
        call_sent=call_sent,
    )


pytestmark = pytest.mark.unit


class TestBasicCases:

    def test_empty_returns_no_actions(self):
        """Attrape : crash sur listes vides."""
        result = evaluate_sms_call_timers([], [], DELAY, NOW)
        assert result == SmsCallActions(sms_enqueues=(), call_enqueues=())

    def test_no_notifications_returns_no_actions(self):
        """Alarmes sans notifications → rien a enqueue."""
        result = evaluate_sms_call_timers([_alarm()], [], DELAY, NOW)
        assert result.sms_enqueues == ()
        assert result.call_enqueues == ()

    def test_notification_elapsed_below_delay_no_enqueue(self):
        """INV-060 : elapsed < delay → pas d'enqueue."""
        notif = _notif(notified_at=NOW - timedelta(minutes=1))  # 1 < 2
        result = evaluate_sms_call_timers([_alarm()], [notif], DELAY, NOW)
        assert result == SmsCallActions(sms_enqueues=(), call_enqueues=())

    def test_notification_elapsed_exactly_delay_enqueues(self):
        """INV-060 boundary : elapsed == delay → enqueue (>= strict).
        Attrape : regression si >= devient >."""
        notif = _notif(notified_at=NOW - timedelta(minutes=2))  # 2 == 2
        result = evaluate_sms_call_timers([_alarm()], [notif], DELAY, NOW)
        assert result.sms_enqueues == (SmsEnqueue(notification_id=1, alarm_id=1, user_id=1),)
        assert result.call_enqueues == (CallEnqueue(notification_id=1, alarm_id=1, user_id=1),)

    def test_notification_elapsed_above_delay_enqueues_both(self):
        """elapsed > delay + pas encore envoye → enqueue SMS ET Call."""
        notif = _notif()  # elapsed=3, sms_sent=False, call_sent=False
        result = evaluate_sms_call_timers([_alarm()], [notif], DELAY, NOW)
        assert len(result.sms_enqueues) == 1
        assert len(result.call_enqueues) == 1

    def test_notified_at_none_is_skipped(self):
        """Notification sans notified_at (cas degenere) → skip, pas de crash."""
        notif = _notif(notified_at=None)
        result = evaluate_sms_call_timers([_alarm()], [notif], DELAY, NOW)
        assert result == SmsCallActions(sms_enqueues=(), call_enqueues=())


class TestAntiDuplicate:
    """INV-062/063 : anti-doublon via flags sms_sent et call_sent."""

    def test_sms_already_sent_not_re_enqueued(self):
        """Si sms_sent=True, pas de nouvel enqueue SMS (mais Call peut encore)."""
        notif = _notif(sms_sent=True, call_sent=False)
        result = evaluate_sms_call_timers([_alarm()], [notif], DELAY, NOW)
        assert result.sms_enqueues == ()
        assert result.call_enqueues == (CallEnqueue(notification_id=1, alarm_id=1, user_id=1),)

    def test_call_already_sent_not_re_enqueued(self):
        """Si call_sent=True, pas de nouvel enqueue Call (mais SMS peut encore)."""
        notif = _notif(sms_sent=False, call_sent=True)
        result = evaluate_sms_call_timers([_alarm()], [notif], DELAY, NOW)
        assert result.sms_enqueues == (SmsEnqueue(notification_id=1, alarm_id=1, user_id=1),)
        assert result.call_enqueues == ()

    def test_both_sent_nothing_enqueued(self):
        """sms_sent=True ET call_sent=True → plus rien a enqueue."""
        notif = _notif(sms_sent=True, call_sent=True)
        result = evaluate_sms_call_timers([_alarm()], [notif], DELAY, NOW)
        assert result == SmsCallActions(sms_enqueues=(), call_enqueues=())


class TestAlarmStatusFilter:
    """Ne pas envoyer SMS/Call pour les alarmes acquittees."""

    def test_acknowledged_alarm_skipped(self):
        """Alarme acknowledged → pas d'enqueue meme si elapsed > delay.
        Attrape : regression si la logique d'ack suspension est cassee."""
        alarm = _alarm(status="acknowledged")
        notif = _notif()
        result = evaluate_sms_call_timers([alarm], [notif], DELAY, NOW)
        assert result == SmsCallActions(sms_enqueues=(), call_enqueues=())

    def test_resolved_alarm_skipped(self):
        """Alarme resolved → pas d'enqueue."""
        alarm = _alarm(status="resolved")
        notif = _notif()
        result = evaluate_sms_call_timers([alarm], [notif], DELAY, NOW)
        assert result == SmsCallActions(sms_enqueues=(), call_enqueues=())

    @pytest.mark.parametrize("status", ["active", "escalated"])
    def test_active_and_escalated_alarms_enqueue(self, status: str):
        """Alarme active ou escalated → enqueue si elapsed >= delay."""
        alarm = _alarm(status=status)
        notif = _notif()
        result = evaluate_sms_call_timers([alarm], [notif], DELAY, NOW)
        assert len(result.sms_enqueues) == 1
        assert len(result.call_enqueues) == 1

    def test_notification_for_unknown_alarm_skipped(self):
        """Notification qui pointe vers une alarme inexistante → skip (robustesse)."""
        notif = _notif(alarm_id=999)  # pas dans la liste alarms
        result = evaluate_sms_call_timers([_alarm(alarm_id=1)], [notif], DELAY, NOW)
        assert result == SmsCallActions(sms_enqueues=(), call_enqueues=())


class TestMultipleNotifications:

    def test_multiple_notifications_all_processed(self):
        """Plusieurs notifs → toutes evaluees independamment."""
        notifs = [
            _notif(notif_id=1, user_id=1),  # enqueue both
            _notif(notif_id=2, user_id=2, sms_sent=True),  # only call
            _notif(notif_id=3, user_id=3, notified_at=NOW - timedelta(minutes=1)),  # skip (too fresh)
        ]
        result = evaluate_sms_call_timers([_alarm()], notifs, DELAY, NOW)
        assert len(result.sms_enqueues) == 1  # notif 1
        assert len(result.call_enqueues) == 2  # notifs 1 et 2
        assert result.sms_enqueues[0].user_id == 1
        assert {e.user_id for e in result.call_enqueues} == {1, 2}


class TestPurity:

    def test_no_mutation(self):
        alarm = _alarm()
        notif = _notif()
        evaluate_sms_call_timers([alarm], [notif], DELAY, NOW)
        assert alarm.status == "active"
        assert notif.sms_sent is False

    def test_deterministic(self):
        alarm = _alarm()
        notif = _notif()
        r1 = evaluate_sms_call_timers([alarm], [notif], DELAY, NOW)
        r2 = evaluate_sms_call_timers([alarm], [notif], DELAY, NOW)
        assert r1 == r2


class TestContinueSemantics:
    """Tue les mutations `continue` -> `break` dans la boucle principale (mutmut
    79, 82, 86, 92). Une notification skippee ne doit JAMAIS empecher le
    traitement des notifications suivantes — sinon une seule notif anormale
    silencerait toutes les autres et personne ne serait alerte."""

    def test_skip_notified_at_none_continues_with_next(self):
        """Mutmut 79 : if notified_at is None: continue (vs break)."""
        notifs = [
            _notif(notif_id=1, notified_at=None),  # skip via continue
            _notif(notif_id=2, user_id=2),         # eligible
        ]
        result = evaluate_sms_call_timers([_alarm()], notifs, DELAY, NOW)
        assert len(result.sms_enqueues) == 1
        assert result.sms_enqueues[0].notification_id == 2

    def test_skip_unknown_alarm_continues_with_next(self):
        """Mutmut 82 : if alarm is None: continue (vs break)."""
        notifs = [
            _notif(notif_id=1, alarm_id=999),       # alarme inconnue → skip
            _notif(notif_id=2, alarm_id=1, user_id=2),  # eligible
        ]
        result = evaluate_sms_call_timers([_alarm(alarm_id=1)], notifs, DELAY, NOW)
        assert len(result.sms_enqueues) == 1
        assert result.sms_enqueues[0].notification_id == 2

    def test_skip_acked_alarm_continues_with_next(self):
        """Mutmut 86 : if alarm.status not in (active, escalated): continue (vs break)."""
        alarms = [
            _alarm(alarm_id=1, status="acknowledged"),  # acked → skip
            _alarm(alarm_id=2, status="active"),        # eligible
        ]
        notifs = [
            _notif(notif_id=1, alarm_id=1, user_id=1),
            _notif(notif_id=2, alarm_id=2, user_id=2),
        ]
        result = evaluate_sms_call_timers(alarms, notifs, DELAY, NOW)
        assert len(result.sms_enqueues) == 1
        assert result.sms_enqueues[0].notification_id == 2

    def test_skip_too_fresh_continues_with_next(self):
        """Mutmut 92 : if elapsed < delay: continue (vs break)."""
        notifs = [
            _notif(notif_id=1, notified_at=NOW - timedelta(minutes=1)),  # 1 < 2 → skip
            _notif(notif_id=2, user_id=2),                                # 3 > 2 → eligible
        ]
        result = evaluate_sms_call_timers([_alarm()], notifs, DELAY, NOW)
        assert len(result.sms_enqueues) == 1
        assert result.sms_enqueues[0].notification_id == 2
