"""
Endpoint de statistiques / KPIs pour le suivi des astreintes.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, extract
from datetime import timedelta
from typing import Optional

from ..database import get_db
from ..models import Alarm, AlarmNotification, EscalationConfig, User
from ..auth import get_current_user
from ..clock import now as clock_now

router = APIRouter(prefix="/api/stats", tags=["stats"])

# Jours feries France (fixes) — les variables seront calculees par annee
JOURS_FERIES_FIXES = [
    (1, 1),   # Jour de l'an
    (5, 1),   # Fete du travail
    (5, 8),   # Victoire 1945
    (7, 14),  # Fete nationale
    (8, 15),  # Assomption
    (11, 1),  # Toussaint
    (11, 11), # Armistice
    (12, 25), # Noel
]


def _est_jour_ferie_fixe(month: int, day: int) -> bool:
    return (month, day) in JOURS_FERIES_FIXES


def _paques(year: int):
    """Calcul de Paques par l'algorithme de Butcher-Meeus."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    from datetime import date
    return date(year, month, day)


def _jours_feries_variables(year: int):
    """Retourne les jours feries variables (Paques, Ascension, etc.)."""
    from datetime import date
    p = _paques(year)
    return [
        p + timedelta(days=1),   # Lundi de Paques
        p + timedelta(days=39),  # Ascension
        p + timedelta(days=50),  # Lundi de Pentecote
    ]


def _est_hors_heures_ouvrees(dt) -> bool:
    """True si le datetime est en dehors des heures ouvrees.
    Heures ouvrees : lundi-vendredi, 8h-12h et 14h-17h, hors jours feries France."""
    if dt is None:
        return False

    # Weekend
    if dt.weekday() >= 5:
        return True

    # Jour ferie fixe
    if _est_jour_ferie_fixe(dt.month, dt.day):
        return True

    # Jour ferie variable
    from datetime import date
    feries_var = _jours_feries_variables(dt.year)
    if date(dt.year, dt.month, dt.day) in feries_var:
        return True

    # Hors 8h-12h et 14h-17h
    h = dt.hour + dt.minute / 60.0
    if not ((8 <= h < 12) or (14 <= h < 17)):
        return True

    return False


@router.get("/kpi")
def get_kpis(
    weeks: int = Query(default=8, ge=1, le=52, description="Nombre de semaines"),
    hors_heures_only: bool = Query(default=True, description="Filtrer hors heures ouvrees"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retourne les KPIs d'astreinte sur les N dernieres semaines."""
    now = clock_now()
    since = now - timedelta(weeks=weeks)

    # Toutes les alarmes de la periode
    all_alarms = (
        db.query(Alarm)
        .filter(Alarm.created_at >= since)
        .order_by(Alarm.created_at)
        .all()
    )

    # ===== 1. Alarmes par semaine (avec filtre heures) =====
    weeks_data = []
    for w in range(weeks):
        week_start = now - timedelta(weeks=weeks - w)
        week_end = now - timedelta(weeks=weeks - w - 1)

        week_alarms = [a for a in all_alarms
                       if week_start <= a.created_at < week_end]

        if hors_heures_only:
            filtered = [a for a in week_alarms if _est_hors_heures_ouvrees(a.created_at)]
        else:
            filtered = week_alarms

        weeks_data.append({
            "week_start": week_start.strftime("%d/%m"),
            "total": len(filtered),
            "with_escalation": len([a for a in filtered if a.escalation_count > 0]),
        })

    # ===== 2. Taux d'escalade =====
    total_alarms = len(all_alarms)
    escalated = len([a for a in all_alarms if a.escalation_count > 0])
    escalation_rate = round(escalated / total_alarms * 100, 1) if total_alarms > 0 else 0

    # ===== 3. MTTR (Mean Time To Resolve) =====
    resolved = [a for a in all_alarms
                if a.status == "resolved" and a.acknowledged_at and a.created_at]
    if resolved:
        total_seconds = sum(
            (a.updated_at - a.created_at).total_seconds()
            for a in resolved
            if a.updated_at
        )
        mttr_minutes = round(total_seconds / len(resolved) / 60, 1)
    else:
        mttr_minutes = 0

    # ===== 4. Top alarmes recurrentes =====
    title_counts = {}
    for a in all_alarms:
        title_counts[a.title] = title_counts.get(a.title, 0) + 1
    top_recurring = sorted(title_counts.items(), key=lambda x: -x[1])[:5]

    # ===== 5. Repartition escalade (n1 vs n2 vs n3+) =====
    esc_distribution = {"position_1": 0, "position_2": 0, "position_3_plus": 0}
    for a in all_alarms:
        if a.escalation_count == 0:
            esc_distribution["position_1"] += 1
        elif a.escalation_count == 1:
            esc_distribution["position_2"] += 1
        else:
            esc_distribution["position_3_plus"] += 1

    return {
        "period_weeks": weeks,
        "hors_heures_only": hors_heures_only,
        "total_alarms": total_alarms,
        "weeks": weeks_data,
        "escalation_rate": escalation_rate,
        "mttr_minutes": mttr_minutes,
        "top_recurring": [{"title": t, "count": c} for t, c in top_recurring],
        "escalation_distribution": esc_distribution,
    }
