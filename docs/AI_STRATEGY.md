# Stratégie IA autonome + CI — Alarme Murgat

> Document de pilotage. À lire avant toute modification de la suite de tests ou du pipeline CI.
> **Version** : 2026-04-17 — **Statut** : draft, à appliquer par étapes.

---

## 1. Mission

### Le système
**Alarme Murgat** est un système d'alarme **critique industriel**. Exigence métier : **zero-bug**. Une alarme manquée = intervention retardée sur un incident potentiellement grave. Une alarme en trop = perte de confiance du personnel d'astreinte (ils coupent, ils ratent la vraie).

Le système est en production continue. Il est modifié fréquemment (nouveaux canaux de notification, ajustements UI Android, évolutions backend).

### L'IA autonome
Une IA est intégrée au pipeline pour **traiter les retours utilisateurs en autonomie** :
- Réception d'un bug report (via issue GitHub, Slack, email relayé)
- Analyse du problème
- Écriture d'un test qui reproduit le bug (RED)
- Écriture d'un fix (GREEN)
- Ouverture d'une PR
- Itération jusqu'à ce que la CI passe
- Merge (humain ou automatique selon criticité)

**Ce qu'on veut** : que l'IA soit un contributeur fiable, pas un générateur de PRs à babysitter.
**Ce qu'on ne veut pas** : que l'IA verrouille progressivement le code avec des tests médiocres qui empêchent toute évolution future.

### Les deux risques symétriques

| Risque | Conséquence |
|---|---|
| **Sous-testé** (pas assez de tests) | Bug en prod. Alarme ratée. Confiance perdue. |
| **Sur-testé** (trop de tests, ou tests de mauvaise qualité) | Code verrouillé. Chaque refactor casse 50 tests. L'IA (ou l'humain) a peur de toucher. Le système ossifie. Nouveaux bugs introduits lors de contournements. |

Cette doc vise à naviguer entre les deux.

---

## 2. Principes fondamentaux

### P1. Les tests viennent de la SPEC, pas du code
Le code peut contenir un bug. Si l'IA écrit des tests en lisant le code, elle FIGE le bug. Pour éviter ça :
- **Source de vérité** : [tests/INVARIANTS.md](../tests/INVARIANTS.md) — catalogue des règles business avec ID stables.
- **Consigne IA** : "Ne lis pas le code source pour écrire un test. Lis l'invariant (INV-XXX) et écris un test qui le vérifie."
- Si un invariant manque → **lever une question**, ne pas deviner depuis le code.

### P2. Un test doit PROUVER quelque chose
Un test qui passe avec une implémentation triviale ou buggy ne vaut rien. Contrôlé par **mutation testing** : on mute le code, le test doit casser. Si le test passe avec le code muté, il ne prouve rien → il est supprimé ou renforcé.

### P3. Préférer invariants > implementation
- **Invariant** : "après N opérations, si `status=acknowledged` alors `suspended_until > now`" (survit aux refactors).
- **Implementation** : "POST /ack renvoie `suspended_until = now + 1800s`" (fige une implémentation, casse à chaque changement interne).
- Utiliser `hypothesis` (property-based) pour les invariants combinatoires.

### P4. Budget de tests fixé
"Maximum 3-5 tests par feature ou par bug fix". Sans budget, l'IA génère 30 tests pour se rassurer (getters, imports, 200 OK). Avec budget, elle priorise. 50 tests qui prouvent quelque chose > 500 tests qui donnent l'illusion.

### P5. TDD strict pour les fix
1. User reporte un bug.
2. IA écrit un test qui REPRODUIT le bug (RED).
3. **Humain valide que le test capture bien le bug.**
4. IA écrit le fix (GREEN).
5. Tous les tests passent.

Jamais l'inverse. Cela rend impossible le fix "qui marche par hasard".

### P6. Le test casse ou il dégage
Un test flaky (passe parfois, échoue parfois) est **plus dangereux** qu'un bug. Il apprend à l'IA (et à l'équipe) à "retry jusqu'à ce que ça passe", jusqu'au jour où un vrai bug est retry-passé par chance. **Règle** : un test flaky est réparé sous 48h ou supprimé.

---

## 3. Architecture CI en niveaux

Tous les tests doivent passer avant un merge. Les niveaux ne filtrent pas — ils **ordonnent** l'exécution pour que l'échec arrive vite et informativement.

### Tier 1 — Unit tests purs (<30s)
- **Cible** : logique métier extraite en fonctions pures (`evaluate_escalation`, `evaluate_ack_expiry`, `evaluate_oncall_heartbeat`, etc.).
- **Dépendances** : aucune (pas de DB, pas de FastAPI, pas de cluster).
- **Outils** : `pytest`, `hypothesis` (property-based), `mutmut` (mutation testing nightly).
- **Gating** : **pre-commit hook**. `git commit` échoue localement si un test casse.
- **Temps de feedback** : 5-30 secondes.
- **Ce qu'il teste** : invariants INV-001 à INV-085 en isolation.

### Tier 2 — Integration tests (<3 min)
- **Cible** : endpoints FastAPI via `TestClient` + SQLite temp file par test (transactional rollback).
- **Dépendances** : Python seulement, pas de Docker, pas de Patroni.
- **Outils** : `pytest-asyncio`, `FastAPI TestClient`, `schemathesis` (contract tests).
- **Gating** : **required status check GitHub Actions** sur la PR. Bouton "Merge" grisé si fail.
- **Temps de feedback** : 1-3 minutes.
- **Ce qu'il teste** : routes API, auth, validation Pydantic, race conditions API (via `threading.Thread`).

### Tier 3 — E2E cluster (<5 min)
- **Cible** : scénarios business de bout en bout contre le cluster 3 nœuds.
- **Dépendances** : Docker Compose complet (Patroni + etcd + 3 backends + Mailhog).
- **Outils** : `pytest` + `requests` (code actuel).
- **Gating** : **required status check avant merge sur main**.
- **Temps de feedback** : 3-5 minutes.
- **Ce qu'il teste** : replication cross-node, token cross-node, golden paths (création → escalade → ack → résolution).

### Tier 4 — Chaos / Failover (nightly, non-blocking)
- **Cible** : tests qui cassent volontairement l'infra.
- **Exemples concrets** :
  - `docker compose stop` primary pendant écriture alarme
  - Coupure 2 des 3 etcd (perte de quorum)
  - Latence réseau 500ms entre nœuds (`tc`, `toxiproxy`)
  - Disque primary plein
  - Modem SIM7600 tué en plein appel
- **Gating** : **aucun**. Notification Slack/email si fail.
- **Temps** : peut prendre 20-30 min, lancé à 2h du matin.
- **Ce qu'il teste** : résistance aux pannes réelles — la classe de bug qui fait perdre une alarme un jour de panne.

### Parallélisation — essentielle pour tenir les temps

Sans parallélisation, les 187 tests actuels prennent 66 min. **Inutilisable pour une IA qui itère.** Deux leviers :

#### Levier 1 — Parallélisation INTRA-niveau (pytest-xdist)
Chaque niveau lance ses tests en parallèle sur N workers.
- **Tier 1** : trivial (fonctions pures sans état partagé). `pytest -n auto`.
- **Tier 2** : chaque test a son propre SQLite temp → isolation totale. `pytest -n auto`.
- **Tier 3** : un cluster Docker par worker (`docker compose -p worker1`, `-p worker2`). Plus coûteux mais faisable sur 2-4 workers.
- **Gain** : tier 3 passe de ~15 min (séquentiel) à ~4-5 min (4 workers).

#### Levier 2 — Parallélisation INTER-niveaux
Tous les tiers tournent en même temps au lieu de `tier2.needs: tier1`.

| Stratégie | Temps total | Coût CI | Feedback précoce | Verdict |
|---|---|---|---|---|
| **Séquentiel** (tier1 → tier2 → tier3) | somme | minimal | oui, tier 1 échoue vite | ✅ recommandé si tier 1 est fiable |
| **Parallèle** (tous en même temps) | max | 3× | non, tout est lancé quand même | ⚠️ gâchis si tier 1 fail régulièrement |

**Recommandation** : **séquentiel inter-niveaux, parallèle intra-niveau**. Tier 1 est rapide (30s) donc le "coût d'attente" est négligeable. Si tier 1 fail, lancer tier 2 et 3 est du gâchis (même bug, détecté plus tard).

### Estimation réaliste APRÈS extraction logique pure (priorité 1)

Sur les 187 tests actuels, l'extraction en fonctions pures redistribue :
- ~120 tests → tier 1 (logique pure, <30s total avec xdist)
- ~45 tests → tier 2 (API, <2 min avec xdist)
- ~20 tests → tier 3 (cluster, <5 min avec 2 workers)
- ~2 tests → tier 4 (failover complet, nightly)

**Temps total PR (séquentiel inter + parallèle intra)** : ~7-8 minutes. Acceptable pour l'IA.

**Sans extraction** : impossible de passer sous 30 min, peu importe la stratégie.

### Schéma GitHub Actions (proposé)

```yaml
name: CI
on: [push, pull_request]

jobs:
  tier1_unit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pytest -m unit -n auto --randomly-seed=last   # ← xdist parallèle intra
    timeout-minutes: 2

  tier2_integration:
    needs: tier1_unit   # ← séquentiel inter : si tier1 fail, pas la peine de lancer tier2
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pytest -m integration -n auto   # ← parallèle intra
    timeout-minutes: 3

  tier3_e2e:
    needs: tier2_integration
    runs-on: ubuntu-latest
    strategy:
      matrix:
        worker: [1, 2]   # ← 2 clusters Docker en parallèle
    steps:
      - run: docker compose -p worker${{ matrix.worker }} up -d
      - run: pytest -m e2e --worker-id=${{ matrix.worker }}
    timeout-minutes: 6

  tier4_chaos:
    if: github.event_name == 'schedule'   # ← nightly cron uniquement
    runs-on: ubuntu-latest
    steps:
      - run: pytest -m chaos
    timeout-minutes: 45
    continue-on-error: true   # ← pas de blocage
```

---

## 4. Process pour un fix autonome

### Workflow type

```
1. Réception issue : "les alarmes ne se résolvent plus quand l'oncall revient"

2. IA consulte :
   - tests/INVARIANTS.md → identifie INV-051 (auto-résolution oncall)
   - .claude/CLAUDE.md → charge le contexte projet

3. IA lit le test correspondant à INV-051 :
   - Est-il flaky ? (historique des 30 derniers runs)
   - Passe-t-il aujourd'hui ? (re-run local)

4. Cas A : le test passe → le bug n'est pas là où il croit.
   IA lève une question à l'humain ("INV-051 test pass mais user reporte X — clarifier").

5. Cas B : le test échoue → bug reproduit.
   IA écrit éventuellement un test PLUS précis qui cerne le bug exact.

6. IA écrit le fix minimal dans la fonction pure extraite
   (ex: evaluate_oncall_heartbeat).

7. IA lance tier 1 localement (<30s).
   - Fail → diagnostic immédiat (fonction + ligne), fix ciblé.
   - Pass → commit.

8. Push → CI tourne tier 2 (3min) puis tier 3 (5min) en arrière-plan.

9. IA attend le résultat.
   - Tier 2 fail → le fix casse un contrat API. IA voit CE test, fix ciblé.
   - Tier 3 fail → problème d'intégration cluster (plus rare). Idem.

10. Tous verts → PR prête pour review humain.

11. Humain vérifie : le test reproduit-il VRAIMENT le bug reporté ?
    Le fix est-il minimal ?

12. Merge. Tier 4 chaos tournera la nuit — si fail, c'est un signal pour demain.
```

### Prompt IA (template à injecter en début de session autonome)

```
Tu es un contributeur du projet Alarme Murgat (système critique zero-bug).

CONSULTE D'ABORD :
- tests/INVARIANTS.md (source de vérité business)
- tests/audit_v2.json (bugs connus)
- docs/AI_STRATEGY.md (ce doc)

RÈGLES STRICTES :
1. Tests viennent de la SPEC (INVARIANTS.md), jamais du code.
2. Pour un bug report : écris un test RED qui reproduit AVANT tout fix.
3. Budget : max 5 tests par fix.
4. Interdit : assert is not None seul, mock.patch pour contourner, time.sleep sans justification.
5. Chaque test a un docstring "Attrape le bug X si Y".
6. Tier 1 doit passer en local avant commit.

SI AMBIGUÏTÉ : lève une question dans la PR description.
NE JAMAIS : interpréter le comportement voulu depuis le code.
```

---

## 5. Garde-fous contre les tests verrouillés

Le problème : au fil des fix, les tests s'accumulent. Beaucoup deviennent obsolètes, tautologiques, ou verrouillent des bugs qu'on a fixés autrement. **Sans garde-fou, la suite devient un poids mort**.

### G1. Mutation testing comme gate qualité
- **Outil** : `mutmut run` en nightly.
- **Règle** : score mutation > 80% sur les fonctions pures (tier 1).
- **Si < 80%** : identifier les mutations survivantes, renforcer ou supprimer les tests correspondants.
- **Quand l'utiliser** : chaque fois que l'IA ajoute un test, mesurer le delta de mutation score.

### G2. Audit trimestriel des tests
Script `tests/audit_tests.py` (déjà présent) tourne chaque trimestre et produit un rapport :
- Tests qui n'ont jamais failé en 90 jours → candidats à suppression (peut-être tautologiques)
- Tests qui failent > 5% (flaky) → à réparer ou supprimer
- Tests > 30s individuels → à justifier ou refactorer
- Tests sans docstring → à documenter

L'humain revoit le rapport, décide quoi supprimer. **La suppression est aussi importante que l'ajout.**

### G3. Règle de suppression
Quand un invariant disparaît du catalogue (business change), les tests correspondants **DOIVENT être supprimés dans le même PR**. Sinon → tests orphelins qui verrouillent un comportement mort.

### G4. Revue humaine sur tests générés par IA
Tests générés par IA sont review-ables comme du code. Le reviewer pose ces questions :
1. "Ce test prouve-t-il ce qu'il prétend ?" (docstring clair, assertion précise)
2. "Si je mute `>=` en `>`, il casse ?" (utiliser mutmut mentalement)
3. "Ce test me gênera-t-il si je refactor ?" (teste un invariant, pas une implementation)
4. "Le nom du test décrit le scénario business ?" (pas `test_function_returns_30`)

Un test qui échoue à 2 de ces 4 questions → rejeté.

### G5. Linter custom sur les tests
Pre-commit hook Python qui refuse :
```python
# Anti-patterns bannis
assert x is not None                 # trop faible, préciser la valeur
mock.patch("...")                    # contourne la difficulté au lieu de la résoudre
time.sleep(N)                        # sans endpoint synchrone équivalent
@pytest.mark.skip("flaky")           # skip permanent = supprimer
def test_foo():                      # docstring manquant
    assert True
```

### G6. Distinction test / harness
Les utilitaires (`_reset_clock_all_nodes`, `_login_user`, `FakeApiService`) ne sont PAS des tests. Ils sont maintenus comme du code de prod : refactor libre, pas de verrouillage.

---

## 6. Outils et stack

| Outil | Rôle | Niveau |
|---|---|---|
| `pytest` | Runner | Tous |
| `hypothesis` | Property-based testing | Tier 1, 2 |
| `mutmut` | Mutation testing | Tier 1 (nightly) |
| `pytest-randomly` | Ordre aléatoire des tests (détecte couplings) | Tous |
| `pytest-testmon` | Re-run uniquement les tests impactés par un diff | Tier 1, 2 (accélération IA) |
| `pytest-xdist` | Parallélisation (après extraction logique pure) | Tier 1, 2 |
| `schemathesis` | Contract tests depuis OpenAPI | Tier 2 |
| `FastAPI TestClient` | Test endpoints sans serveur live | Tier 2 |
| `toxiproxy` | Injection de panne réseau | Tier 4 |
| Linter Ruff custom | Règles anti-tests-pourris | Pre-commit |

---

## 7. Questions ouvertes / décisions à prendre

1. **Qui est l'IA ?** Claude via API ? Un agent dédié ? Comment elle est branchée (webhook GitHub, polling d'issues) ?
2. **Niveau d'autonomie** : merge automatique si tous verts, ou review humain obligatoire ?
3. **Seuils mutation testing** : 80% est une suggestion. À calibrer selon le périmètre.
4. **Périmètre tier 1** : toute la logique métier extraite ? Aussi les validations Pydantic ? Les helpers de schéma ?
5. **Budget total CI par PR** : combien de minutes on s'autorise avant que le feedback devienne "trop lent pour l'IA" ?
6. **Gestion des flaky** : suppression automatique après N échecs en M jours ? Qui décide ?

---

## 8. Reprendre dans une nouvelle session

Checklist pour un Claude (ou autre) qui rouvre ce projet demain :

- [ ] Lire [.claude/CLAUDE.md](../.claude/CLAUDE.md) (contexte projet)
- [ ] Lire [tests/INVARIANTS.md](../tests/INVARIANTS.md) (source de vérité business)
- [ ] Lire [tests/audit_v2.json](../tests/audit_v2.json) (bugs connus, priorités)
- [ ] Lire ce document (stratégie CI + process IA)
- [ ] Consulter `git log` pour voir l'état récent
- [ ] Vérifier le statut des priorités dans audit_v2.json (section `priority_actions`)

**Priorités actuelles** (rappel) :
1. Extraire la logique métier en fonctions pures + 100 unit tests rapides
2. Corriger les bugs catalogués (INV-011, INV-018, INV-019, INV-031, INV-015b, INV-066, INV-082, INV-084, INV-085, BUG-03)
3. Tests de race conditions + property-based (hypothesis)
4. Mettre en place les 4 niveaux CI avec gating approprié
5. Tests error paths et config dynamique

**Point de départ recommandé** : étape 1 (extraction logique pure). Elle débloque toutes les autres.

---

## 8bis. Bugs CI connus — à réévaluer avant de fixer

> **Règle d'or** : avant de toucher à un de ces bugs, relire la rubrique correspondante,
> évaluer si le contexte a changé, et confirmer que le coût du fix est inférieur au coût
> de continuer à vivre avec. Cf. principe P4 (budget de tests/complexité fixé).

### CI-BUG-01 — Image runner GitHub Actions vieillit côté serveur

**Statut** : workaround appliqué (image bumpée 2.319.1 → 2.333.1, J4).
**Symptôme attendu de réapparition** : `Forbidden Runner version vX.Y.Z is deprecated and cannot receive messages` dans les logs du conteneur runner. Les workflows partent en timeout côté GitHub.
**Échéance probable** : 6-12 mois après chaque pin (GitHub déprécie les versions au fil du temps).
**Fix court (~10 min)** : bump manuel du tag dans `infra/runner/docker-compose.runner.yml` + `runner.sh down/up`.
**Fix long (~30 min, à évaluer)** : Renovate ou Dependabot configuré sur l'image. PRs auto, 1/mois.
**À réévaluer si** :
- Le bug réapparaît une 2e fois (= pattern, mérite l'auto)
- Le runner part sur OVH (downtime moins acceptable, monitoring + upgrade auto plus pertinents)
- L'équipe grossit (un humain qui rate le bump = blocage CI pour tous)

### CI-BUG-02 — Race xdist sur seed users SQLite (tier 2)

**Statut** : workaround appliqué (`pytest -n 1` sur tier 2, J6).
**Symptôme** : `sqlite3.IntegrityError: UNIQUE constraint failed: users.name` en CI multi-CPU. Invisible en local mono-CPU.
**Cause** : workers xdist partagent `os.environ["TEST_DB_FILE"]`, donc même fichier SQLite, donc race sur le seed users du lifespan FastAPI.
**Coût actuel** : tier 2 séquentiel = ~3s pour 4 tests. Acceptable.
**Fix court (~1h)** : DB par worker via `PYTEST_XDIST_WORKER` dans `tests/integration/conftest.py`.
**À réévaluer si** :
- Tier 2 dépasse ~30 tests OU sa durée totale dépasse 30s
- Décision de basculer sur Postgres service container (refactor du conftest de toute façon)
- Tests intégration commencent à hit des routes qui modifient les users (le state partagé devient un problème de correctness, pas juste de perf)

### Pattern à retenir pour les futurs bugs CI

Tout fix infra a un coût caché : maintenance, divergence entre runners local/cloud,
courbe d'apprentissage pour le suivant. Avant d'agir :

1. Le bug se manifeste-t-il **maintenant** ou **dans le futur** ? (futur → souvent OK d'attendre)
2. Le workaround a-t-il un coût récurrent ou one-shot ? (récurrent → fixer plus vite)
3. Le fix introduit-il de la complexité que personne d'autre que toi ne comprendra ? (oui → écrire la doc avant le fix)
4. Y a-t-il un message d'erreur clair quand le bug réapparaît ? (oui → coût de re-fix faible, attendre OK)

---

## 9. Références croisées

- [.claude/CLAUDE.md](../.claude/CLAUDE.md) — contexte projet, conventions TDD
- [tests/INVARIANTS.md](../tests/INVARIANTS.md) — catalogue des règles business
- [tests/audit_v2.json](../tests/audit_v2.json) — audit critique de la suite actuelle
- [tests/audit_results.json](../tests/audit_results.json) — métriques de durée des tests
- [tests/audit_tests.py](../tests/audit_tests.py) — script d'audit trimestriel
- [IMPROVEMENTS.md](../IMPROVEMENTS.md) — backlog historique du projet
- [README.md](../README.md) — vue d'ensemble utilisateur
- [ARCHITECTURE_SMS_VOIX.md](../ARCHITECTURE_SMS_VOIX.md) — architecture SMS/voix
- [docs/architecture_option_B_3vps_patroni.md](architecture_option_B_3vps_patroni.md) — architecture cluster

---

## 10. Dernière mise à jour

- **2026-04-17** : création du document. Synthèse de la session d'audit et d'alignement doc.
