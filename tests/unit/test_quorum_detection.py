"""Unit tests (tier 1) pour backend/app/logic/quorum_detection.py.

Invariant couvert : INV-085 (perte de quorum cluster), sous-cas 1/3 — détection.

Tests purs : aucune DB, aucun cluster réel, aucun sleep. Entrées = snapshots,
sortie = QuorumState. <100ms total.
"""
from datetime import datetime, timedelta

import pytest

from backend.app.logic.quorum_detection import (
    ClusterSnapshot,
    QuorumState,
    REMINDER_WINDOWS,
    evaluate_quorum_loss,
    should_send_initial_email,
    should_send_reminder,
)

NOW = datetime(2026, 5, 14, 12, 0, 0)

pytestmark = pytest.mark.unit


def _healthy(ts: datetime) -> ClusterSnapshot:
    """Cluster sain : quorum présent ET Patroni joignable."""
    return ClusterSnapshot(has_quorum=True, patroni_reachable=True, timestamp=ts)


def _no_quorum(ts: datetime) -> ClusterSnapshot:
    """Quorum perdu (has_quorum=False), Patroni encore joignable."""
    return ClusterSnapshot(has_quorum=False, patroni_reachable=True, timestamp=ts)


def _patroni_down(ts: datetime) -> ClusterSnapshot:
    """Patroni injoignable, quorum nominalement présent."""
    return ClusterSnapshot(has_quorum=True, patroni_reachable=False, timestamp=ts)


def _every_minute(oldest_minutes_ago: int, factory) -> list[ClusterSnapshot]:
    """Une observation par minute, de NOW-oldest jusqu'à NOW-1 inclus."""
    return [factory(NOW - timedelta(minutes=m)) for m in range(oldest_minutes_ago, 0, -1)]


def test_quorum_lost_for_1_minute_is_not_yet_declared_lost():
    """Attrape le bug 'alerte immédiate' : has_quorum=False depuis seulement 1 min
    ne doit PAS franchir le seuil anti-flapping de 3 min → is_lost False, lost_since None."""
    history = _every_minute(1, _no_quorum)  # observation non-saine à NOW-1min
    snapshot = _no_quorum(NOW)
    state = evaluate_quorum_loss(snapshot, history)
    assert state == QuorumState(is_lost=False, lost_since=None)


def test_quorum_lost_for_4_minutes_is_declared_lost():
    """Attrape le bug 'jamais déclenché' : has_quorum=False en continu depuis 4 min
    franchit le seuil 3 min → is_lost True, lost_since = début de la série non-saine."""
    history = _every_minute(4, _no_quorum)  # NOW-4 .. NOW-1, tous non-sains
    snapshot = _no_quorum(NOW)
    state = evaluate_quorum_loss(snapshot, history)
    assert state.is_lost is True
    assert state.lost_since == NOW - timedelta(minutes=4)


def test_quorum_recovered_after_loss_resets_state():
    """Attrape le bug 'alerte collée' : après 4 min non-saines, un snapshot sain
    à NOW doit remettre l'état à zéro → is_lost False ET lost_since None."""
    history = _every_minute(4, _no_quorum)
    snapshot = _healthy(NOW)
    state = evaluate_quorum_loss(snapshot, history)
    assert state == QuorumState(is_lost=False, lost_since=None)


def test_patroni_unreachable_for_4_minutes_is_declared_lost():
    """Attrape le bug 'has_quorum seulement' : la 2e condition INV-085 (Patroni
    injoignable depuis tous les noeuds) doit aussi déclencher la perte après 4 min."""
    history = _every_minute(4, _patroni_down)
    snapshot = _patroni_down(NOW)
    state = evaluate_quorum_loss(snapshot, history)
    assert state.is_lost is True
    assert state.lost_since == NOW - timedelta(minutes=4)


# ============================================================================
# Tests ajoutés manuellement post-/ai-retry sur PR #103 (2026-05-18) :
# le bot n'a pas re-triggered (probable filtre dispatch). Ces 2 tests
# verrouillent la boundary 3 min exacte ET le comportement anti-flapping
# (série interrompue → reset compteur), qui sont le cœur sémantique de
# INV-085 [C].
# ============================================================================


@pytest.mark.parametrize(
    "duration_seconds,expected_is_lost,case_label",
    [
        # 2 min 59 s : strictement avant le seuil → pas encore déclaré perdu.
        # Ferme la borne inférieure : un mutant `>=` au lieu de `>` passerait
        # le test 4 min mais échouerait ici (179 > 180 = False, mais 179 >= 180
        # = False aussi — par contre 180 > 180 = False et 180 >= 180 = True).
        (179, False, "2min59s — sous le seuil"),
        # 3 min EXACTEMENT : seuil non-inclus (`> 3 min`, pas `>= 3 min`).
        # C'est CE cas qui tue le mutant `>=` : avec `>=`, lost_since = NOW-3min
        # et (180-0) >= 180 = True → fail. Avec `>`, (180) > 180 = False → pass.
        (180, False, "3min00s — pile sur le seuil (strict, non-inclus)"),
        # 3 min 01 s : juste au-dessus du seuil → déclaré perdu.
        # Ferme la borne supérieure : prouve que la détection se déclenche
        # bien dès qu'on dépasse strictement 3 min.
        (181, True, "3min01s — strictement au-dessus du seuil"),
    ],
)
def test_quorum_loss_boundary_3min_strict(duration_seconds, expected_is_lost, case_label):
    """INV-085 [C] : la transition de détection est strictement à `> 3 minutes`.

    Tue le mutant `> QUORUM_LOSS_THRESHOLD` ↔ `>= QUORUM_LOSS_THRESHOLD`
    (off-by-one sur le seuil temporel). Pour un invariant [C] zéro-bug, la
    boundary exacte doit être verrouillée sur les 2 côtés (179s → False,
    180s → False, 181s → True) — c'est le minimum pour fermer la borne.

    Construit une série continue d'observations non-saines couvrant
    `duration_seconds` jusqu'à NOW, puis vérifie le verdict du detector.
    """
    # history doit contenir toutes les observations >= NOW-duration et < NOW
    # (le snapshot courant est ajouté séparément en argument 1).
    # On en met une par seconde aux extrémités pour que `lost_since` soit
    # exactement à NOW-duration (snapshot le plus ancien de la série continue).
    oldest = NOW - timedelta(seconds=duration_seconds)
    history = [
        _no_quorum(oldest),
        _no_quorum(NOW - timedelta(seconds=duration_seconds // 2)),
        _no_quorum(NOW - timedelta(seconds=1)),
    ]
    snapshot = _no_quorum(NOW)

    state = evaluate_quorum_loss(snapshot, history)

    assert state.is_lost is expected_is_lost, (
        f"INV-085 boundary [{case_label}] : duration={duration_seconds}s, "
        f"attendu is_lost={expected_is_lost}, observé is_lost={state.is_lost}. "
        f"Si ce cas échoue, le mutant `>` ↔ `>=` n'est plus tué — la boundary "
        f"exacte de 3min n'est plus garantie strict."
    )


def test_quorum_loss_series_interrupted_resets_counter():
    """INV-085 [C] : une observation saine intercalée COUPE la série non-saine,
    le compteur de durée repart de zéro à la dernière reprise de l'état dégradé.

    Tue le mutant `break` ↔ `continue` dans la boucle de parcours de
    `history` : sans le `break` (sortie de série), la fonction additionnerait
    naïvement toutes les observations non-saines de l'historique, ignorant
    l'anti-flapping. C'est le CŒUR SÉMANTIQUE de INV-085 (sinon un glitch
    Patroni court déclencherait une fausse alerte de perte de quorum).

    Construction : 2 min non-saines → 1 snapshot SAIN intercalé → 2 min non-saines
    + snapshot courant non-sain. Total "non-saines" = ~4 min, mais la série
    CONTINUE qui se termine à NOW dure seulement ~2 min → sous le seuil 3 min
    → is_lost doit rester False.
    """
    # Construction :
    #  - 2 obs non-saines : NOW-5min, NOW-4min  (ancien bloc avant le trou)
    #  - 1 obs SAINE : NOW-3min                  (le "trou" qui réinitialise)
    #  - 2 obs non-saines : NOW-2min, NOW-1min  (nouveau bloc qui se prolonge à NOW)
    #  - snapshot courant : non-sain à NOW
    history = [
        _no_quorum(NOW - timedelta(minutes=5)),
        _no_quorum(NOW - timedelta(minutes=4)),
        _healthy(NOW - timedelta(minutes=3)),   # ← coupe la série anti-flapping
        _no_quorum(NOW - timedelta(minutes=2)),
        _no_quorum(NOW - timedelta(minutes=1)),
    ]
    snapshot = _no_quorum(NOW)

    state = evaluate_quorum_loss(snapshot, history)

    # La série continue qui touche NOW = NOW-2min .. NOW = 2 minutes.
    # Sous le seuil 3 min → is_lost doit être False.
    assert state.is_lost is False, (
        "INV-085 anti-flapping : un snapshot SAIN à NOW-3min coupe la série "
        "non-saine. La série CONTINUE qui touche NOW dure ~2min (NOW-2min..NOW), "
        f"sous le seuil 3min → is_lost doit être False (observé : {state.is_lost}). "
        "Si ce cas échoue, le mutant `break` ↔ `continue` n'est plus tué — "
        "l'anti-flapping de INV-085 n'est plus garanti."
    )
    assert state.lost_since is None, (
        f"INV-085 anti-flapping : is_lost=False implique lost_since=None "
        f"(contrat de QuorumState). Observé : lost_since={state.lost_since!r}."
    )


# ============================================================================
# Tests pour fermer les 2 mutants survivants restants (mutmut tier 1.5 score
# 97.5% → 100% strict). 2 mutants équivalents (`frozen=True` → `frozen=False`
# sur les dataclasses) sont pragma-isés directement dans le code source.
# ============================================================================


def test_quorum_history_with_duplicate_snapshot_timestamp_excluded():
    """INV-085 [C] : si une observation dans history porte le MÊME timestamp
    que le snapshot courant, elle DOIT être exclue de earlier (filtre strict `<`).

    Tue le mutant `s.timestamp < snapshot.timestamp` ↔ `s.timestamp <= snapshot.timestamp`
    (filtre du sorted) : avec `<=`, une observation dupliquant snapshot.timestamp
    serait incluse dans `earlier` et changerait le calcul de lost_since si elle
    est non-saine (peut artificiellement reculer le compteur).

    Construction : history contient une obs SAINE au MÊME timestamp que snapshot
    (cas pathologique mais possible : 2 readers concurrents qui inscrivent dans
    l'historique à la même ms). Si on incluait cette obs (mutant `<=`), elle
    serait la 1ère candidate dans `earlier` (tri desc), saine → break immédiat,
    lost_since resterait = snapshot.timestamp → is_lost = 0 > 3min = False
    (résultat IDENTIQUE au comportement correct pour ce cas — mais on prouve
    le strict `<` via un cas où l'inclusion CHANGERAIT le résultat).

    Pour vraiment forcer la différence : l'obs au même timestamp est non-saine
    ET il y a 5 min d'obs non-saines avant. Avec `<` strict : l'obs au même
    timestamp est exclue, earlier commence à NOW-1min (non-sain), remonte jusqu'à
    NOW-5min (non-sain continu) → lost_since = NOW-5min → is_lost = True.
    Avec mutant `<=` : l'obs au même timestamp est incluse en 1er (non-sain) →
    lost_since = snapshot.timestamp (même résultat car premier de la série).
    Donc égal aussi.

    Le seul cas qui DIFFÈRE : l'obs au même timestamp est saine ET les obs
    précédentes seraient non-saines. Avec `<` : exclue, earlier commence à
    NOW-1min non-sain → 5 min non-saines → is_lost=True. Avec `<=` : incluse
    en 1er (saine) → break immédiat, lost_since = snapshot.timestamp →
    is_lost=False.
    """
    history = [
        # 5 min d'observations non-saines continues jusqu'à NOW-1min
        _no_quorum(NOW - timedelta(minutes=5)),
        _no_quorum(NOW - timedelta(minutes=4)),
        _no_quorum(NOW - timedelta(minutes=3)),
        _no_quorum(NOW - timedelta(minutes=2)),
        _no_quorum(NOW - timedelta(minutes=1)),
        # ⚠ Observation SAINE au MÊME timestamp que le snapshot courant.
        # Doit être EXCLUE par le filtre `s.timestamp < snapshot.timestamp`.
        _healthy(NOW),
    ]
    snapshot = _no_quorum(NOW)

    state = evaluate_quorum_loss(snapshot, history)

    # Avec `<` strict (correct) : l'obs saine à NOW est exclue, earlier = 5min
    # non-saines continues → lost_since = NOW-5min → is_lost = True.
    # Avec mutant `<=` : l'obs saine à NOW est incluse en 1ère position du tri desc
    # → break immédiat → lost_since = snapshot.timestamp → is_lost = False.
    assert state.is_lost is True, (
        "INV-085 : une observation au MÊME timestamp que le snapshot doit être "
        "EXCLUE de earlier (filtre strict `<`, pas `<=`). Si le mutant `<=` est "
        f"introduit, l'obs saine à NOW casse la série et is_lost devient False. "
        f"Observé : is_lost={state.is_lost}."
    )
    assert state.lost_since == NOW - timedelta(minutes=5), (
        f"INV-085 : lost_since doit être le début de la série continue qui "
        f"se termine à NOW (5 min avant), got {state.lost_since}."
    )


def test_quorum_history_empty_with_unhealthy_snapshot():
    """INV-085 [C] : si history est vide ET snapshot non-sain, lost_since
    doit être initialisé à snapshot.timestamp (pas None), et is_lost = False
    (durée 0 < 3min seuil).

    Tue le mutant `lost_since = snapshot.timestamp` ↔ `lost_since = None`
    (initialisation par défaut avant la boucle). Avec le mutant, si history
    est vide, la boucle ne tourne pas et `lost_since` reste None → la ligne
    suivante `(snapshot.timestamp - lost_since)` lèverait TypeError sur
    `datetime - None` → crash de la fonction au lieu d'un retour propre.

    Avec le code correct, lost_since = snapshot.timestamp → durée = 0 →
    is_lost = False → retour QuorumState(False, None).
    """
    state = evaluate_quorum_loss(_no_quorum(NOW), history=[])

    assert state == QuorumState(is_lost=False, lost_since=None), (
        "INV-085 : history vide + snapshot non-sain doit retourner "
        f"QuorumState(is_lost=False, lost_since=None), got {state!r}. "
        "Si le mutant `lost_since = None` (au lieu de snapshot.timestamp) est "
        "introduit, la fonction crashe sur TypeError `datetime - None` au lieu "
        "de retourner proprement."
    )


# ============================================================================
# INV-085 sous-cas 2/3 (#80) — should_send_initial_email
#
# Décide si l'email initial d'alerte direction technique doit être envoyé.
# Règle : envoyer SSI is_lost=True ET aucun email pas encore envoyé pour
# l'incident courant.
# ============================================================================


_LOST = QuorumState(is_lost=True, lost_since=NOW - timedelta(minutes=4))
_SANE = QuorumState(is_lost=False, lost_since=None)


def test_initial_email_due_when_lost_and_never_sent():
    """INV-085 sous-cas 2/3 (#80) : sur transition is_lost=True et
    email_sent_at=None, l'email initial DOIT partir.

    Tue le stub RED (return False) : avec le stub, ce test échoue car
    on attend True. C'est la fonction CŒUR de #80."""
    assert should_send_initial_email(_LOST, email_sent_at=None) is True, (
        "INV-085 #80 : is_lost=True + email_sent_at=None → doit retourner True. "
        "Sinon aucun email ne part jamais et l'invariant 'alerter la direction "
        "sur quorum perdu' n'est jamais respecté."
    )


def test_initial_email_not_due_when_already_sent_in_same_incident():
    """INV-085 sous-cas 2/3 (#80) anti-doublon : si l'email a déjà été envoyé
    pour cet incident (email_sent_at non-None), un tick suivant qui constate
    encore is_lost=True ne doit PAS renvoyer le même email — sinon spam à
    chaque tick (60s → 60 emails/h hors anti-spam #81).

    Tue le mutant `email_sent_at is None` ↔ `email_sent_at is not None`."""
    assert should_send_initial_email(
        _LOST,
        email_sent_at=NOW - timedelta(minutes=2),
    ) is False, (
        "INV-085 #80 : is_lost=True mais email déjà envoyé il y a 2 min "
        "→ no-op (pas de renvoi). Sinon spam à chaque tick (60s)."
    )


def test_initial_email_not_due_when_not_lost():
    """INV-085 sous-cas 2/3 (#80) : quand le quorum est sain (is_lost=False),
    aucun email à envoyer — même si email_sent_at est None (cas le plus
    fréquent en régime nominal).

    Tue le mutant `state.is_lost is True` ↔ `True` (toujours True) :
    sans cette branche, la fonction enverrait un email à CHAQUE tick en régime
    sain (catastrophe — bypass complet de la détection)."""
    assert should_send_initial_email(_SANE, email_sent_at=None) is False, (
        "INV-085 #80 : is_lost=False → no-op systématique. Sinon spam permanent "
        "en régime nominal (60 emails/h)."
    )


# ============================================================================
# INV-085 sous-cas 3/3 (#81) — should_send_reminder
#
# Décide si un reminder anti-spam est dû pour la fenêtre 1h, 3h ou 6h depuis
# l'email initial. Retourne la fenêtre due (timedelta) ou None.
#
# Convention boundary : "≥ W" (à exactement W écoulé, le reminder est dû).
# Spec catalogue : "reminders à 1h, 3h, 6h" — borne inclusive (1h pile suffit).
# ============================================================================


# Référence : email initial envoyé à T0 = NOW - 7h. On peut donc tester
# toutes les fenêtres (1h, 3h, 6h) avec divers `now` = T0 + delta.
_T0 = NOW - timedelta(hours=7)


def test_reminder_not_due_before_1h_after_initial_email():
    """INV-085 #81 : à 30 min après l'email initial, AUCUN reminder n'est dû
    (la 1ère fenêtre est à 1h).

    Tue le mutant `>= 1h` ↔ `>= 0s` (seuil ramené à zéro) qui ferait partir
    un reminder dès le tick suivant l'email initial."""
    now = _T0 + timedelta(minutes=30)
    assert should_send_reminder(
        state=_LOST,
        email_sent_at=_T0,
        reminders_sent_at=[],
        now=now,
    ) is None, (
        "INV-085 #81 : 30 min < 1h → aucun reminder dû (None attendu). "
        "Sinon spam dès le 1er tick après l'email initial."
    )


def test_reminder_1h_due_at_exactly_1h_when_none_sent():
    """INV-085 #81 boundary 1h : à exactement 1h après l'email initial,
    le reminder 1h est dû (borne `>=`, pas `>`).

    Tue le mutant `>= 1h` ↔ `> 1h` (off-by-one strict) : avec `>`, à 1h pile
    le test échoue car None retourné au lieu de 1h."""
    now = _T0 + timedelta(hours=1)
    result = should_send_reminder(
        state=_LOST,
        email_sent_at=_T0,
        reminders_sent_at=[],
        now=now,
    )
    assert result == timedelta(hours=1), (
        f"INV-085 #81 : à T0+1h pile, reminder 1h dû. Attendu timedelta(hours=1), "
        f"got {result!r}. Si None : le mutant `> 1h` (au lieu de `>= 1h`) "
        f"est passé — la fenêtre 1h est skippée d'un tick."
    )


def test_reminder_1h_not_redue_if_already_sent():
    """INV-085 #81 anti-doublon : après envoi du reminder 1h, un tick suivant
    à 1h05 ne doit PAS le ré-envoyer (sinon spam intra-fenêtre).

    Tue le mutant qui ignorerait le filtre `reminders_sent_at` :
    sans filtre, la fonction renverrait 1h à chaque tick entre 1h et 3h."""
    now = _T0 + timedelta(hours=1, minutes=5)
    assert should_send_reminder(
        state=_LOST,
        email_sent_at=_T0,
        reminders_sent_at=[timedelta(hours=1)],
        now=now,
    ) is None, (
        "INV-085 #81 : reminder 1h déjà envoyé, le tick suivant à 1h05 doit "
        "retourner None. Sinon spam à chaque tick (60s) jusqu'à 3h."
    )


def test_reminder_3h_due_at_3h_when_1h_already_sent():
    """INV-085 #81 fenêtre 3h : à 3h exact, 1h déjà envoyé, le reminder 3h
    est dû.

    Tue 2 mutants potentiels :
    - `>= 3h` ↔ `> 3h` : à 3h pile, mutant `>` retournerait None
    - boucle qui sortirait à la 1ère fenêtre due sans regarder les suivantes
      (return 1h à tort puisque déjà envoyé) : le filtre exclut 1h, doit
      passer à 3h."""
    now = _T0 + timedelta(hours=3)
    result = should_send_reminder(
        state=_LOST,
        email_sent_at=_T0,
        reminders_sent_at=[timedelta(hours=1)],
        now=now,
    )
    assert result == timedelta(hours=3), (
        f"INV-085 #81 : à T0+3h, 1h déjà envoyé → reminder 3h dû. "
        f"Attendu timedelta(hours=3), got {result!r}."
    )


def test_reminder_6h_due_at_6h_when_1h_and_3h_already_sent():
    """INV-085 #81 fenêtre 6h : à 6h exact, 1h+3h déjà envoyés, le 6h est dû.
    Dernière fenêtre du jeu."""
    now = _T0 + timedelta(hours=6)
    result = should_send_reminder(
        state=_LOST,
        email_sent_at=_T0,
        reminders_sent_at=[timedelta(hours=1), timedelta(hours=3)],
        now=now,
    )
    assert result == timedelta(hours=6), (
        f"INV-085 #81 : à T0+6h, reminders 1h+3h déjà envoyés → 6h dû. "
        f"Attendu timedelta(hours=6), got {result!r}."
    )


def test_reminder_stops_after_6h():
    """INV-085 #81 stop final : à 7h, tous les 3 reminders déjà envoyés
    (1h+3h+6h) → AUCUN nouveau reminder. La spec est explicite : aucun
    reminder au-delà de 6h (tranché 2026-04-20).

    Tue un mutant qui ajouterait une 4e fenêtre (12h, 24h, etc.)."""
    now = _T0 + timedelta(hours=7)
    assert should_send_reminder(
        state=_LOST,
        email_sent_at=_T0,
        reminders_sent_at=list(REMINDER_WINDOWS),
        now=now,
    ) is None, (
        "INV-085 #81 : tous reminders (1h/3h/6h) envoyés → silence radio "
        "définitif après 6h. La spec interdit toute nouvelle relance "
        "(décision 2026-04-20). Si != None, une 4e fenêtre a été ajoutée."
    )


def test_reminder_not_due_when_initial_email_not_yet_sent():
    """INV-085 #81 ordre causal : tant que l'email initial n'a pas été envoyé
    (email_sent_at=None), aucun reminder n'est dû — sinon on enverrait des
    reminders sur un email inexistant.

    Cas réaliste : transition is_lost=True intercepte au même tick que
    `should_send_initial_email` → on appelle d'abord initial puis reminder ;
    si le code de reminder ne checke pas email_sent_at, il peut considérer
    `now - None` et crasher OU se déclencher à tort.

    Tue le mutant `if email_sent_at is None: return None` ↔ pas de check
    (path TypeError ou behavior incorrect)."""
    now = NOW
    assert should_send_reminder(
        state=_LOST,
        email_sent_at=None,
        reminders_sent_at=[],
        now=now,
    ) is None, (
        "INV-085 #81 : pas de reminder sans email initial préalable. "
        "Le tick courant doit traiter d'abord `should_send_initial_email`. "
        "Si crash ou non-None : la fonction n'a pas géré le guard email_sent_at=None."
    )


def test_reminder_not_due_when_quorum_recovered():
    """INV-085 #81 garde-fou : si le quorum est revenu sain (is_lost=False)
    entre 2 ticks, AUCUN reminder ne doit partir, même si la fenêtre temporelle
    est franchie depuis l'email initial.

    Réaliste : email initial envoyé, puis quorum résolu à T0+30min, puis
    le tick à T0+1h05 voit `is_lost=False` → la persistance d'incident
    (reset) sera faite par l'orchestrateur dans le même tick, mais la
    fonction pure doit déjà être robuste à ce cas.

    Tue le mutant `state.is_lost is True` ↔ `True` (toujours True) qui
    enverrait des reminders éternels même après résolution."""
    now = _T0 + timedelta(hours=1, minutes=5)
    assert should_send_reminder(
        state=_SANE,
        email_sent_at=_T0,
        reminders_sent_at=[],
        now=now,
    ) is None, (
        "INV-085 #81 : is_lost=False → no-op, peu importe la fenêtre. "
        "Sinon des reminders partent même après que le quorum est revenu."
    )
