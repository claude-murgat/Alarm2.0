"""Logique métier pure — fonctions sans effet de bord, testables en isolation.

Ce module contient la logique de décision extraite de escalation.py et alarms.py
en fonctions pures qui :
- prennent un snapshot d'état en entrée (dataclasses, pas d'ORM)
- retournent une liste d'Actions à appliquer (dataclasses)
- n'ont AUCUN effet de bord (pas de DB, pas de HTTP, pas de now())

L'appelant (escalation_loop, endpoints API) est responsable de :
1. Charger l'état depuis la DB (read)
2. Appeler la fonction pure avec le snapshot
3. Appliquer les Actions retournées (write)

Les tests unitaires (tests/unit/) testent ces fonctions directement avec des
snapshots construits en dur — zéro dépendance externe, <30s total pour toute la suite.

Voir tests/INVARIANTS.md pour les règles business et docs/AI_STRATEGY.md pour
la stratégie CI.
"""
