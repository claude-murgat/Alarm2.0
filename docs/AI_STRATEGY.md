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

> **Cette section décrit l'état IMPLÉMENTÉ.** Workflow de référence : [.github/workflows/pr.yml](../.github/workflows/pr.yml).
> Branche actuelle de travail : `ci/pipeline`.

Tous les tests doivent passer avant un merge. Les niveaux ne filtrent pas — ils **ordonnent** l'exécution pour que l'échec arrive vite et informativement.

### Tier 1 — Unit tests purs (~25s observé)
- **Cible** : logique métier extraite en fonctions pures (`evaluate_escalation`, `evaluate_ack_expiry`, `evaluate_oncall_heartbeat`, `evaluate_sms_call_timers`, `evaluate_ack_authorization`, `evaluate_alarm_creation`).
- **Dépendances** : aucune (pas de DB, pas de FastAPI, pas de cluster).
- **Runner** : `ubuntu-latest` (cloud GitHub gratuit). **Pas self-hosted** : pas besoin de Docker, on économise les ressources self-hosted pour le tier 3.
- **Localisation** : `tests/unit/` (87 tests aujourd'hui, marker `@pytest.mark.unit`).
- **Outils** : `pytest`, `pytest-xdist` (`-n auto`), `pytest-randomly`, `pytest-cov`, `hypothesis` (installé mais non utilisé pour l'instant).
- **Gating** : required status check GitHub Actions (branch protection master active).
- **Pre-commit hook** : pas en place (TODO si l'IA push souvent du code cassé).

### Tier 2 — Integration tests (~25s observé, croîtra)
- **Cible** : contrat API FastAPI via `TestClient`.
- **Dépendances** : Python seulement (TestClient = pas de serveur live, pas de Docker).
- **Backend de DB** : **SQLite temp**, fichier unique partagé (session-scoped). Postgres service container abandonné pour v1 (complexité non justifiée pour 4 tests). À reconsidérer si le tier 2 hit des features Postgres-specific (jsonb, advisory locks).
- **Runner** : `ubuntu-latest` (cloud).
- **Localisation** : `tests/integration/` (4 tests aujourd'hui, marker `@pytest.mark.integration`).
- **Outils** : `pytest`, `FastAPI TestClient`. Pas de `pytest-asyncio` (TestClient gère le sync). Pas de `schemathesis` (à ajouter si on veut des contract tests OpenAPI auto).
- **Parallélisation** : DÉSACTIVÉE (`-n 1`). Cause : race xdist sur seed users SQLite (bug CI-BUG-02). Réactiver avec DB par worker quand >30 tests.
- **Gating** : required status check.

### Tier 3 — E2E cluster (~7 min observé sur worker 1, ~6m37 pour 119 tests)
- **Cible** : scénarios business end-to-end contre un vrai backend FastAPI sur Patroni/etcd.
- **Dépendances** : Docker Compose (Patroni + etcd + backend + Mailhog), 1 cluster single-node par worker.
- **Runner** : `[self-hosted, linux, docker, alarm-ci]` (2 runners Docker locaux via `infra/runner/`).
- **Localisation** : `tests/test_e2e.py` + `tests/test_*.py` (sauf `unit/` et `integration/`). Pas de marker `@pytest.mark.e2e` à ce stade — sélection par chemin (ignore `tests/unit`, `tests/integration`).
- **Parallélisation** :
  - **Plan cible** : matrix `worker: [1, 2]` + `pytest-split --splits 2 --group N` → ~3 min par worker.
  - **État actuel (J8 itération 1)** : worker 2 désactivé temporairement, worker 1 fait `tests/test_e2e.py` seul. Pytest-split installé mais pas activé. Rampe progressive après green.
- **Ports** : worker 1 = 18xxx, worker 2 = 28xxx (cf `.env.ci-w1`, `.env.ci-w2`). Pas de conflit avec `.env.dev` (8xxx).
- **Bootstrap** : `scripts/ci-wait-cluster.sh` polling Patroni leader + backend `/health` (180s timeout). Remplace les `sleep` magiques.
- **Cleanup** : `scripts/ci-cleanup.sh` en `if: always()`. Cible UNIQUEMENT le projet du worker (pas de prune global).
- **Gating** : **PAS** required status check pour l'instant. La branch protection ne couvre que tier 1+2 le temps que tier 3 stabilise. À ajouter quand 100% green sur 5 runs consécutifs.
- **BACKEND_URL** : `http://host.docker.internal:18000` (resp. 28000). Le runner conteneurisé ne peut joindre les conteneurs sur le host qu'à travers `host.docker.internal:host-gateway` (cf CI-BUG-05).

### Tier 4 — Chaos / Failover (PAS IMPLÉMENTÉ)
- **Statut** : non implémenté. Tests `@pytest.mark.failover` existent dans `tests/test_e2e.py` mais skip volontaire en tier 3 via `--skip-failover`.
- **Plan futur** :
  - Workflow `nightly.yml` séparé (cron 2h du matin)
  - Tests qui cassent volontairement : `docker compose stop` primary, perte quorum etcd, latence `tc`/`toxiproxy`, disque plein, SIM7600 tué.
  - Pas de gating, notification si rouge.

### Parallélisation — état réel

#### Inter-niveaux : SÉQUENTIEL strict
`tier2.needs: tier1` et `tier3.needs: tier2`. Si tier 1 fail, tier 2/3 ne tournent pas. Économise les runners self-hosted et accélère le feedback IA (échec tier 1 visible en 25s au lieu d'attendre 7 min).

#### Intra-niveau
| Tier | Stratégie | État |
|---|---|---|
| Tier 1 | `pytest -n auto` (xdist) | ✅ Activé |
| Tier 2 | `-n 1` (séquentiel) | ⚠️ Race SQLite, à fixer si volume |
| Tier 3 | `pytest-split --splits 2 --group N` via matrix worker | ⚠️ Installé, désactivé J8 itération 1 |

#### Concurrency PR
`concurrency: { group: ci-${ref}, cancel-in-progress: true }` — chaque nouveau push annule le run précédent sur la même branche. Évite l'empilement quand l'IA itère vite.

### Temps total observé (sur PR)

- Tier 1 : ~25s
- Tier 2 : ~25s
- Tier 3 worker 1 : ~7 min (boot 30s + 119 tests 6m37)
- **Total séquentiel** : ~8 min, tenable pour l'IA.

### Schéma GitHub Actions

Le workflow réel évolue plus vite que cette doc. **Référence canonique** : [.github/workflows/pr.yml](../.github/workflows/pr.yml).

Structure résumée :
```
ci/pipeline branch
   ├── tier1_unit (ubuntu-latest, ~25s)
   └── needs → tier2_integration (ubuntu-latest, ~25s)
        └── needs → tier3_e2e (matrix [1,2], self-hosted alarm-ci, ~7 min)
            ├── compose up cluster (.env.ci-wN)
            ├── wait-cluster.sh
            ├── pytest tests/test_e2e.py
            ├── compose logs → artifact
            └── cleanup.sh (if always)
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

> Source de vérité : [requirements-dev.txt](../requirements-dev.txt) + [pyproject.toml](../pyproject.toml).

| Outil | Rôle | État | Notes |
|---|---|---|---|
| `pytest` 8.3.4 | Runner | ✅ Installé | Tous tiers |
| `pytest-xdist` 3.6.1 | Parallélisation intra-niveau | ✅ Installé | Activé tier 1, désactivé tier 2 (race SQLite) |
| `pytest-randomly` 3.16.0 | Ordre aléatoire (détecte couplings) | ✅ Installé | Activé tier 1+2, désactivé tier 3 (debug) |
| `pytest-cov` 6.0.0 | Coverage | ✅ Installé | Tier 1 uniquement |
| `pytest-split` 0.9.0 | Répartition tests entre matrix workers | ✅ Installé | Activable en tier 3 (J8 itération 2) |
| `hypothesis` 6.122.3 | Property-based testing | ⚠️ Installé, 0 usage | À utiliser pour invariants combinatoires |
| `FastAPI TestClient` (via fastapi) | Test endpoints sans serveur live | ✅ Installé | Tier 2 |
| `mutmut` | Mutation testing | ❌ Non installé | À ajouter quand on attaquera G1 (nightly) |
| `pytest-testmon` | Re-run tests impactés par un diff | ❌ Non installé | Optionnel, accélération IA |
| `pytest-asyncio` | Tests async natifs | ❌ Non installé | Pas nécessaire avec TestClient |
| `schemathesis` | Contract tests OpenAPI | ❌ Non installé | À évaluer quand le tier 2 grandit |
| `toxiproxy` | Injection panne réseau | ❌ Non installé | Tier 4 (chaos), pas implémenté |
| Linter Ruff custom (G5) | Règles anti-tests-pourris | ❌ Non configuré | À ajouter en pre-commit |
| `requests`, `pyjwt`, `pyyaml` | Helpers tests/CI | ✅ Installés | Tests + scripts diagnostic |

**Outils d'infra CI (hors pytest)** :
- `myoung34/github-runner:2.333.1` (image runner Docker, cf `infra/runner/docker-compose.runner.yml`)
- GitHub App `alarm-murgat-bot` (App ID 3428066, installation 125174785, repo claude-murgat/Alarm2.0)
- Branch protection sur `master` : required checks `Tier 1 — Unit (logique pure)` + `Tier 2 — Integration (FastAPI TestClient + SQLite)`. Tier 3 PAS required (le temps de stabiliser).

---

## 7. Questions ouvertes / décisions à prendre

### Tranchées (J1-J9)

1. ~~**Qui est l'IA ?**~~ → **Claude Code** (headless en CI + session interactive en local). Identité robot = GitHub App `alarm-murgat-bot` (App ID 3428066, installation 125174785). **Bot opérationnel** via `.github/workflows/ai-bot.yml` (phase 2/5 — `workflow_dispatch` manuel, Opus 4.7 headless). Voir `.github/ai-bot/README.md` pour l'utilisation. Phases 3 (trigger auto `ai:fix` + denylist CI) et 4 (merge auto `ai:approved`) à venir.
4. ~~**Périmètre tier 1**~~ → **logique métier extraite uniquement** (`backend/app/logic/*`). Pas de validations Pydantic ni helpers schéma à ce stade. 87 tests.
5. ~~**Budget temps CI par PR**~~ → ~8 min observé, OK. Plafond cible : 10 min. Au-delà → optimiser ou splitter.

### Encore ouvertes

2. **Niveau d'autonomie IA** : merge automatique si tous verts, ou review humain obligatoire ? **Décision provisoire** : review humain obligatoire tant que l'IA n'a pas fait 10 PRs propres consécutives.
3. **Seuils mutation testing** : 80% est une suggestion. À calibrer quand mutmut sera activé (pas avant que tier 1 soit complet).
6. **Gestion des flaky** : suppression automatique après N échecs en M jours ? Qui décide ?
7. **Tier 3 required ?** : pour l'instant non required (instable). À promouvoir required après 5 runs verts consécutifs.
8. **Hypothesis non utilisé** : installé mais 0 test property-based. Décider d'un premier test pilote (ex: invariant `evaluate_escalation` sur des entrées combinatoires) ou retirer la dep.

---

## 8. Reprendre dans une nouvelle session

Checklist pour un Claude (ou autre) qui rouvre ce projet demain :

- [ ] Lire [.claude/CLAUDE.md](../.claude/CLAUDE.md) (contexte projet)
- [ ] Lire [tests/INVARIANTS.md](../tests/INVARIANTS.md) (source de vérité business)
- [ ] Lire [tests/audit_v2.json](../tests/audit_v2.json) (bugs connus, priorités)
- [ ] Lire ce document **dans son intégralité, en particulier la section 8bis** (bugs CI déjà identifiés et workarounds appliqués — ne pas tomber dans les mêmes pièges)
- [ ] `git log --oneline -20` pour voir l'état récent
- [ ] `git branch --show-current` puis `gh run list --branch <branche> --limit 5` pour voir l'état CI
- [ ] Vérifier le statut des priorités dans audit_v2.json (section `priority_actions`)

**État au 2026-04-20** :
- ✅ Extraction logique pure : 95% complète (6 modules dans `backend/app/logic/`, 87 unit tests verts)
- ✅ Pipeline CI 3 tiers : opérationnel (tier 1+2 verts en cloud, tier 3 sur self-hosted Docker local — voir section 3)
- ✅ Branch protection master active (required tier 1+2+3w1+3w2)
- ✅ GitHub App `alarm-murgat-bot` + 2 runners self-hosted
- ⚠️ Tier 3 : 88/119 verts sur premier run, 12 fails à fixer (cf prompt session annexe)
- ❌ Tier 4 chaos : non implémenté
- ❌ Mutation testing (mutmut) : non installé
- ⚠️ **Bot IA contributeur autonome : phase 2/5 opérationnelle** :
  - ✅ Phase 1 — identité GH App → token → commit → push → PR validée (PR #2 mergée)
  - ✅ Phase 2 — spawn Claude Code CLI headless (Opus 4.7) validé bout-en-bout sur issue #6 (INV-031) : agent a correctement abandonné car bug déjà fixé, respect P5 TDD strict (PR #5 mergée)
  - ❌ Phase 3 — trigger auto `issues.labeled == 'ai:fix'` + denylist CI check + compteur itérations + `tee` live log (à faire)
  - ❌ Phase 4 — merge auto sur label `ai:approved` (à faire)
  - ❌ Phase 5 — tests pilotes sur vrais bugs encore présents (à lancer après audit INVARIANTS)
  - Actuellement : trigger = `workflow_dispatch` manuel (`gh workflow run ai-bot.yml -f issue_number=<N>`)
  - Voir `.github/ai-bot/README.md` pour l'utilisation détaillée

**Problème révélé par le premier pilote** : `tests/INVARIANTS.md` contient des invariants marqués 🐛 qui ont déjà été fixés dans le code (ex: INV-031 fixé par commit `cc35b7d`). **Audit complet du catalogue requis** avant de lancer les pilotes phase 5.

**Priorités originales** (encore valables, à réordonner selon l'état) :
1. Extraire la logique métier en fonctions pures + 100 unit tests rapides ← **fait à 95%**
2. Corriger les bugs catalogués (INV-011, INV-018, INV-019, INV-031, INV-015b, INV-066, INV-082, INV-084, INV-085, BUG-03)
3. Tests de race conditions + property-based (hypothesis)
4. Mettre en place les 4 niveaux CI avec gating approprié ← **3/4 fait**
5. Tests error paths et config dynamique

**Point de départ recommandé maintenant** :
- Si tu débarques **fraîche** : commence par lire la section 8bis pour comprendre les pièges CI déjà documentés. Puis regarde l'état actuel via `gh run list`.
- Si tu vises à **stabiliser le tier 3** : c'est l'enjeu actuel. Voir le prompt dédié donné à l'autre session (12 tests rouges à analyser).
- Si tu vises à **étoffer tier 2** : ajouter des tests intégration sur les contrats critiques (escalation, ack, alarme creation). Budget P4 = 5 par feature.

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

### CI-BUG-03 — Bit exécutable des scripts shell pas préservé sous Windows

**Statut** : fix appliqué (`git update-index --chmod=+x`, J8 itération 1).
**Symptôme** : `Permission denied` sur `./scripts/ci-cleanup.sh` ou `./scripts/ci-wait-cluster.sh` lors du run sur runner Linux. `chmod +x` local ne suffit pas — Git sous Windows ne propage pas le bit exec dans l'index.
**Cause** : `core.fileMode` souvent à `false` sous Git Windows. Le bit exec doit être marqué dans l'index via une commande dédiée.
**Fix permanent** : `git update-index --chmod=+x scripts/*.sh infra/runner/runner.sh` (mode 100755 dans l'index, vérifiable via `git ls-files --stage`).
**À réévaluer si** : tu ajoutes un nouveau script `.sh` — penser à `git update-index --chmod=+x` AVANT le premier push, sinon CI rouge.

### CI-BUG-04 — `RUN_AS_ROOT=false` casse l'accès docker.sock (DooD)

**Statut** : fix appliqué (`RUN_AS_ROOT=true`, J8 itération 2).
**Symptôme** : `permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock`.
**Cause** : le runner non-root ne peut pas lire le socket Docker monté (qui appartient à `root:docker`). On a choisi DooD via socket mount → root effectif quel que soit le user du runner.
**Fix** : `RUN_AS_ROOT: "true"` dans `infra/runner/docker-compose.runner.yml`. Acceptable car threat model DooD déjà acté (cf section 8bis intro).
**À réévaluer si** : on passe à un runner rootless (Docker rootless ou Sysbox) — alors revenir à `RUN_AS_ROOT=false` après avoir donné l'accès au socket via groupe docker. Pas avant que ce soit nécessaire.

### CI-BUG-05 — `localhost` ≠ host depuis runner conteneurisé (DooD)

**Statut** : fix appliqué (`extra_hosts: host.docker.internal:host-gateway` partout, J8 itération 3).
**Symptôme** : tests échouent à se connecter à `http://localhost:18000` (le backend lancé par le runner). `wait-cluster.sh` timeout sur "backend up" alors que les conteneurs sont healthy.
**Cause** : depuis le runner conteneurisé (DooD), `localhost` = loopback du runner, PAS du host. Les conteneurs des tests tournent sur le host (via socket mount). Pour les joindre depuis le runner, utiliser `host.docker.internal`.
**Fix** :
- `infra/runner/docker-compose.runner.yml` : `extra_hosts: ["host.docker.internal:host-gateway"]` sur le service runner
- `docker-compose.yml` (cluster Patroni) : pareil sur etcd, patroni, backend (pour Linux où `host.docker.internal` n'est pas natif)
- Workflow tier 3 : `BACKEND_URL=http://host.docker.internal:$PORT` (pas `localhost`)
**À réévaluer si** : on passe à un runner non-conteneurisé (`actions-runner` natif sur host) — alors revenir à `localhost`. Cf migration OVH.

### CI-BUG-06 — Tier 3 worker 2 désactivé temporairement

**Statut** : workaround actif (J8 itération 1, voir `.github/workflows/pr.yml`).
**Symptôme** : aucun (la matrix tourne sur worker 1, worker 2 émet un junit XML vide).
**Cause** : premier run tier 3 a échoué pendant la collection pytest avec une trace illisible (capture.py noise + `1 error in collection`). Réduction de scope pour isoler.
**Fix court (15-30 min)** : sur le worker 1 isolé qui passe (88/119 verts), réactiver progressivement :
1. Réintégrer les autres fichiers (`test_improvements.py`, `test_user_modes.py`, etc.) un par un, voir ce qui collecte mal.
2. Réactiver `pytest-randomly` (`-p no:randomly` enlever).
3. Réactiver matrix worker 2 + `pytest-split --splits 2 --group N`.
**À réévaluer** : DOIT être adressé. Workaround acceptable pour 1-2 itérations max, sinon on perd la moitié de la capacité tier 3 et on n'attrape pas les bugs sur les autres fichiers.

### CI-BUG-07 — `tests/test_e2e.py::test_data_persists_after_restart` chemin Windows hardcodé

**Statut** : en cours de fix (session annexe, J8 itération 5).
**Symptôme** : `FileNotFoundError: [Errno 2] No such file or directory: 'C:/Users/Charles/Desktop/Projet Claude/Alarm2.0'`.
**Cause** : chemin absolu Windows hardcodé dans le test. Marchait par hasard chez l'auteur, casse en CI Linux.
**Fix** : remplacer par chemin relatif au repo OU détection OS. Vérifier `grep -rn "C:/" tests/` pour d'autres occurrences.
**À réévaluer** : ajouter un linter custom pour interdire les chemins absolus dans les tests (cf G5 anti-patterns). Optionnel pour l'instant.

### CI-BUG-08 — Actions GitHub sur Node.js 20 dépréciées (échéance juin 2026)

**Statut** : warning, pas bloquant.
**Symptôme** : annotation jaune dans tous les runs : "Node.js 20 actions are deprecated... Will be forced to Node.js 24 by default starting June 2nd, 2026. Removed Sept 16th, 2026."
**Actions concernées** : `actions/checkout@v4`, `actions/setup-python@v5`, `actions/upload-artifact@v4`.
**Fix prévu** : attendre les versions majeures suivantes de ces actions (v5/v6/v5 respectivement) qui passeront sur Node.js 24. Ou forcer Node.js 24 maintenant via `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true` env var.
**À réévaluer** : printemps 2026, vérifier les nouvelles versions disponibles. Fix = bump des `@vN` dans le workflow, 5 min.

### CI-BUG-09 — Tier 3 worker cassé persistant entre runs (couplage PR)

**Statut** : fix appliqué (cleanup robuste + retry compose up, 2026-04-20, session 4).
**Symptôme observé** : runs consécutifs fail sur `tier3 worker N` avec `dependency failed to start: container ci-wN-patroni-1 exited (130)` ou `Network is unreachable sur host.docker.internal:N8000`. Worker 1 passe, worker 2 fail (ou inverse). Re-run seul ne change rien. **Toutes les PR suivantes sont bloquées** tant que l'état résiduel n'est pas nettoyé.
**Cause** : le `ci-cleanup.sh` en `if: always()` pouvait timeout silencieusement (compose down bloqué sur container zombie, volume pgdata verrouillé), laissant des résidus (containers en état Error, volumes orphelins, network _default non supprimé). Le run suivant tente de recréer ces ressources → conflit → Patroni exit 130.
**Fix appliqué** :
1. `scripts/ci-cleanup.sh` robustifié :
   - `timeout 30` / `timeout 10` sur chaque commande docker (empêche blocage illimité)
   - Vérification état résiduel après down (containers + volumes + networks)
   - Force-cleanup des résidus avec timeouts séparés
   - Annotation `::error::` GitHub Actions + `exit 2` si état persiste (le problème devient visible dans les logs du run même)
   - `set -uo pipefail` sans `-e` pour que chaque étape puisse être "défensive" sans kill le script au premier fail
2. `.github/workflows/pr.yml` : step `Boot cluster` avec **1 retry** — si compose up fail au 1er coup, force-cleanup + retry. Au 2e fail, abandon clean avec annotation.
3. Pattern applicable aux autres workers : le script est paramétré par `PROJECT` (ex: `ci-w1`, `ci-w2`).

**À réévaluer si** :
- Un nouveau symptôme apparaît que le retry ne résout pas (ex: runner Docker dans un état plus profond — nécessiterait restart du `gh-runner` conteneur)
- Le volume de résidus grossit malgré tout (signal d'un leak ailleurs, ex: test qui crée des volumes hors compose)
- Le temps de job rallongi trop (retry ~1 min supp quand activé — acceptable pour l'instant)

**Principe à retenir** : une PR ne doit **jamais** être bloquée par l'état d'un run précédent. Chaque job doit pouvoir se récupérer d'un état résiduel sans intervention humaine. Le cleanup doit être **idempotent, timeouté, et bruyant** (annotation visible) en cas d'échec partiel.

---

### Pattern à retenir pour les futurs bugs CI

Tout fix infra a un coût caché : maintenance, divergence entre runners local/cloud,
courbe d'apprentissage pour le suivant. Avant d'agir :

1. Le bug se manifeste-t-il **maintenant** ou **dans le futur** ? (futur → souvent OK d'attendre)
2. Le workaround a-t-il un coût récurrent ou one-shot ? (récurrent → fixer plus vite)
3. Le fix introduit-il de la complexité que personne d'autre que toi ne comprendra ? (oui → écrire la doc avant le fix)
4. Y a-t-il un message d'erreur clair quand le bug réapparaît ? (oui → coût de re-fix faible, attendre OK)

---

## 9. Références croisées

### Process / business
- [.claude/CLAUDE.md](../.claude/CLAUDE.md) — contexte projet, conventions TDD
- [tests/INVARIANTS.md](../tests/INVARIANTS.md) — catalogue des règles business
- [tests/audit_v2.json](../tests/audit_v2.json) — audit critique de la suite actuelle
- [tests/audit_results.json](../tests/audit_results.json) — métriques de durée des tests
- [tests/audit_tests.py](../tests/audit_tests.py) — script d'audit trimestriel

### Infra CI (mise en place J1-J9, branche `ci/pipeline`)
- [.github/workflows/pr.yml](../.github/workflows/pr.yml) — workflow 3 tiers (référence canonique)
- [pyproject.toml](../pyproject.toml) — config pytest (markers, pythonpath, coverage)
- [requirements-dev.txt](../requirements-dev.txt) — outils tests/CI
- [.gitattributes](../.gitattributes) — LF forcé sur scripts/Python (portabilité Linux)
- [.dockerignore](../.dockerignore) — réduit contexte build images
- [infra/runner/](../infra/runner/) — runner Docker self-hosted (compose, README, script wrapper)
- [scripts/ci-wait-cluster.sh](../scripts/ci-wait-cluster.sh) — polling Patroni leader + backend ready
- [scripts/ci-cleanup.sh](../scripts/ci-cleanup.sh) — cleanup ciblé sur 1 projet (pas de prune global)
- [.env.ci-w1](../.env.ci-w1), [.env.ci-w2](../.env.ci-w2) — config cluster CI ports décalés
- [tests/integration/conftest.py](../tests/integration/conftest.py) — TestClient + SQLite temp

### Projet (autre)
- [IMPROVEMENTS.md](../IMPROVEMENTS.md) — backlog historique
- [README.md](../README.md) — vue d'ensemble utilisateur
- [ARCHITECTURE_SMS_VOIX.md](../ARCHITECTURE_SMS_VOIX.md) — architecture SMS/voix
- [docs/architecture_option_B_3vps_patroni.md](architecture_option_B_3vps_patroni.md) — architecture cluster

---

## 10. Dernière mise à jour

- **2026-04-19** : refonte sections 3, 6, 7, 8, 8bis, 9 après mise en place CI 3 tiers (J1-J9). Ajout de 6 bugs CI (CI-BUG-03 à 08). État réel divergent du plan initial — sections marquées "implémenté" font foi.
- **2026-04-17** : création du document. Synthèse de la session d'audit et d'alignement doc.
