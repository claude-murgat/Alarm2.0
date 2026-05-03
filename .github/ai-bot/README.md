# `alarm-murgat-bot` — bot IA contributeur autonome

Bot IA qui transforme des bug reports GitHub en PRs (test RED + fix GREEN).
S'exécute dans un workflow GitHub Actions sous l'identité de la GitHub App
`alarm-murgat-bot` (App ID 3428066, installation 125174785).

- **Stratégie & design** : [`docs/AI_STRATEGY.md`](../../docs/AI_STRATEGY.md)
- **Workflow** : [`.github/workflows/ai-bot.yml`](../workflows/ai-bot.yml)
- **System prompt** : [`.github/ai-bot/prompt.md`](./prompt.md)
- **Source de vérité business** : [`tests/INVARIANTS.md`](../../tests/INVARIANTS.md)

---

## Ce que le bot fait

1. Lit une issue GitHub décrivant un bug (référence `INV-XXX` recommandée).
2. Consulte `tests/INVARIANTS.md` pour trouver l'invariant business concerné.
3. Écrit un **test RED** qui reproduit le bug, le lance, vérifie qu'il FAIL.
4. Écrit le **fix GREEN** minimal dans `backend/app/**`.
5. Ouvre une PR vers `master` sous son identité `alarm-murgat-bot[bot]`.
6. La CI (tier 1+2+3) tourne sur la PR ; un humain review et merge.

## Ce que le bot ne fait pas

- Toucher à la CI, l'infra, ou le catalogue d'invariants (denylist — cf plus bas).
- Merger sa propre PR (phase 4, à venir, conditionnée à label `ai:approved` humain).
- Deviner un comportement en lisant le code (P1 : tests from spec, pas du code).

---

## Comment déclencher le bot

### Trigger principal — label `ai:fix` (phase 3B opérationnelle)

```bash
# 1. Crée une issue qui décrit le bug (référence INV-XXX si possible)
gh issue create --title "Bug INV-XYZ — description" --body "..." --label "ai:fix"
# OU crée l'issue sans label, puis pose le label après :
gh issue edit <N> --add-label "ai:fix"

# 2. Le bot démarre automatiquement (~30s). Suis le run :
gh run list --workflow ai-bot.yml --limit 1
gh run watch <run-id> --exit-status
```

### Retry sur une PR bot existante

Le bot reprend une PR existante si :

- **`check_run.completed` avec conclusion `failure`** sur une PR bot → retry auto avec les logs CI failed dans le prompt
- **`issue_comment.created`** sur une PR bot contenant `/ai-retry <instructions>` OU mentionnant `@alarm-murgat-bot` → retry avec le commentaire dans le prompt
- **`workflow_dispatch` manuel** avec le même `issue_number` et une PR bot déjà ouverte → retry-manual

Commentaires sans ces mots-clés, ou posés par le bot lui-même, sont ignorés (anti-boucle).

### Fallback — `workflow_dispatch` manuel

```bash
gh workflow run ai-bot.yml -f issue_number=<N>
```

Utile pour tester manuellement ou re-déclencher sur une issue sans re-labeler.

---

## Comportements possibles du bot

### (a) Fix produit avec succès

- Branche `ai/issue-<N>/<run-id>` créée depuis `master`
- Commit sous `alarm-murgat-bot[bot]`
- PR ouverte vers `master` avec bloc `<!-- AI-BOT-METADATA -->` dans le body
- Commentaire sur l'issue source avec le lien PR
- CI tier 1+2+3 tourne sur la PR

### (b) Abandon propre (pas de diff)

Le bot abandonne dans ces cas :

- Le bug décrit **n'existe plus** dans le code (ex: déjà fixé par un commit précédent)
- L'invariant est ambigu dans `INVARIANTS.md`, nécessite clarification humaine
- Le fix nécessite un fichier denylist (voir plus bas)
- Le bug n'est pas reproductible avec les informations de l'issue

Dans ce cas :
- Aucun commit produit
- Un commentaire détaillé est posté sur l'issue avec le résumé de l'agent et la raison
- Le workflow se termine en `failure` (signal : « pas de diff = abandon »)

### (c) Violation denylist (phase 3A)

Si l'agent essaie quand même de modifier un fichier interdit, un step CI
post-run détecte la violation et fail le run sans créer de PR. Label `ai:denied`
posé sur l'issue source.

### (d) Limite d'itérations atteinte (phase 3C)

Après 3 itérations sans succès (chaque itération = 1 run agent + CI qui fail),
le bot s'arrête. Label `ai:abandoned` posé sur la PR et l'issue source.
Reject reason : `iteration-limit-3-exceeded`. Message générique (pas de
diagnostic sur la cause du plateau).

### (e) Boucle détectée (phase 3D)

Quand le bot échoue **3 fois de suite avec les mêmes tests en failure**
(identiques ou sous-ensemble strict de l'itération précédente), le dispatch
rejette **avant** de lancer la 4ᵉ itération. Reject reason : `loop-detected`.

Le message d'abandon liste les tests en boucle et suggère des hypothèses
(invariant mal décrit, test RED qui attaque un symptôme, fix dans le mauvais
module). Label `ai:abandoned` aussi.

Algorithme (cf [`workflows/ai-bot.yml`](../workflows/ai-bot.yml) dispatch, step `loop_detect`) :
1. Sur event `check_run.completed` avec `conclusion=failure` (mode retry-ci) :
   télécharge les artifacts `tier*-reports` du run CI qui vient de fail
2. Parse les `<testcase>` en failure/error via [`scripts/parse_junit_failures.py`](./scripts/parse_junit_failures.py)
   → set de `classname::name`
3. Compare avec `last-fail-tests` stocké dans le body PR (itération N-1)
4. Si `current ⊆ last` → `fail-streak++`, sinon `fail-streak = 1`
5. Si `fail-streak >= 3` → abandon avec reason `loop-detected`

Fallback gracieux : si l'artifact junit est inaccessible ou vide, la détection
est skip (warning), l'iter-limit standard reprend le relais.

**Pourquoi** : avant phase 3D, le bot consommait les 3 itérations avant
d'abandonner sans diagnostic. Phase 3D remonte un signal clair quand le
pattern d'échec est stable → l'humain gagne du temps pour identifier la
cause racine (souvent un invariant ambigu ou un fix dans le mauvais module).

---

## Denylist — fichiers que le bot ne modifie PAS

Fichiers protégés par le prompt (phase 2) et par un check CI (phase 3) :

- `.github/workflows/**` — la CI elle-même
- `.github/ai-bot/**` — auto-reprogrammation interdite
- `.github/CODEOWNERS`
- `infra/**` — clés, config runners
- `scripts/ci-*.sh`
- `docker-compose.yml`, `.env*`
- `pyproject.toml`, `requirements-dev.txt`, `backend/requirements.txt`
- `tests/conftest.py`, `tests/integration/conftest.py`
- **`tests/INVARIANTS.md`** — source de vérité, seul l'humain la modifie
- `tests/audit_v2.json`
- `docs/AI_STRATEGY.md`, `android/INVARIANTS.md`
- `.claude/**`, `CLAUDE.md`, `.gitattributes`, `.gitignore`

Zone autorisée : `backend/**`, `tests/unit/**`, `tests/integration/**`,
`tests/test_*.py` (sauf conftest).

---

## Labels GitHub utilisés

| Label | Posé par | Sens |
|---|---|---|
| `ai:fix` | humain | « Dis au bot de traiter cette issue » (trigger auto phase 3) |
| `ai:approved` | humain | « Mergez auto cette PR » (phase 4) |
| `ai:abandoned` | bot | « J'ai renoncé, reprise humaine requise » (iteration-limit OU loop-detected) |
| `ai:needs-human` | bot | « Fix nécessite un fichier denylist ou ambiguïté » |
| `ai:denied` | bot | « J'ai tenté une violation denylist, CI m'a stoppé » (phase 3) |

## Raisons d'abandon (`reject_reason`)

| Reason | Phase | Signification | Action humaine typique |
|---|---|---|---|
| `iteration-limit-3-exceeded` | 3C | 3 itérations épuisées sans convergence | Lire les résumés d'itérations dans le body PR, commenter `/ai-retry <hint>` |
| `loop-detected` | 3D | 3 CI échecs consécutifs sur les **mêmes** tests | Examiner les tests listés : invariant ambigu ? Fix dans le mauvais module ? |
| `comment-from-bot` | 3B | Anti-boucle : commentaire écrit par le bot lui-même | Aucune (event ignoré silencieusement) |
| `pr-not-from-bot` | 3B | Event déclenché sur une PR humaine | Aucune |
| `label-not-ai:fix` | 3B | Label posé n'est pas `ai:fix` | Aucune |

Autres reasons (moins fréquents) : `check-conclusion-not-failure`,
`comment-no-trigger-keyword`, `comment-on-non-pr-issue`,
`check-run-has-no-pr`, `unsupported-event`, `pr-missing-metadata-issue`.

---

## Observer un run

### Pendant qu'il tourne

- Page [Actions](https://github.com/claude-murgat/Alarm2.0/actions) → workflow **AI Bot**
- Ou `gh run watch <run-id>` en CLI
- Les logs live affichent l'étape en cours (phase 3 ajoutera les tool calls de l'agent en temps réel via `tee`)

### Après qu'il est terminé

- Artifacts du run téléchargeables 14 jours :
  - `agent-input.md` : prompt envoyé à l'agent
  - `agent-output.jsonl` : stream JSON de tous les events (tool calls, messages)
  - `agent-summary.md` : résumé final de l'agent
  - `issue-body.txt` : body de l'issue lu

```bash
gh run download <run-id> --dir /tmp/bot-<run-id>
cat /tmp/bot-<run-id>/ai-bot-run-<run-id>/agent-summary.md
```

### Parser le stream JSON

Chaque ligne du `.jsonl` est un event. Types utiles :

```bash
# Tous les tool calls de l'agent
jq -c 'select(.type == "assistant" and .message.content != null) | .message.content[] | select(.type == "tool_use") | {name, input}' agent-output.jsonl

# Résumé final
jq -rs 'map(select(.type == "result")) | last | .result' agent-output.jsonl

# Coût / tokens
jq -rs 'map(select(.type == "result")) | last | {total_cost_usd, duration_ms, num_turns}' agent-output.jsonl
```

---

## Coûts observés

| Poste | Valeur typique |
|---|---|
| Durée d'un run complet | 3 à 15 min (selon complexité) |
| Tokens input Opus 4.7 | ~30k (avec prompt cache, après premier hit) |
| Tokens output | ~2 à 5k |
| Coût $ | 0€ tant que dans le quota Max |
| Minutes GitHub Actions | 0€ (repo public) |

---

## Dépannage

### « Le bot ne démarre pas après `gh workflow run` »

Vérifier les secrets : `ALARM_BOT_APP_ID`, `ALARM_BOT_PRIVATE_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`
doivent exister dans Settings → Secrets and variables → Actions.

### « Le bot abandonne systématiquement »

Lire le commentaire qu'il a posté sur l'issue. Causes fréquentes :
- Le bug est déjà fixé (cf. INV-031 pilote phase 2) → audit catalogue `INVARIANTS.md` requis
- Le body de l'issue est trop vague (pas de INV-XXX, pas de reproduction)
- Le fix demande un fichier denylist → intervention humaine

### « L'agent produit un diff mais la CI fail »

Normal — phase 3 ajoutera le retry automatique sur échec CI (jusqu'à 3 itérations).
Aujourd'hui, pour itérer : commenter sur la PR avec les hints, re-dispatcher le workflow
manuellement avec le même numéro d'issue.

---

## Historique pilotes

| Date | Issue | Invariant | Résultat | PR produite |
|---|---|---|---|---|
| 2026-04-20 | #6 | INV-031 (ACK 403) | ✅ **Abandon propre** — bug déjà fixé par commit `cc35b7d`, l'agent a refusé de forcer un RED→GREEN artificiel (respect P5 strict). Révélé que le catalogue était stale → audit complet (PR #9, #11). | Aucune (abandon volontaire) |
| 2026-04-21 | #13 | INV-082 (atomicité bulk) | ✅ **Test de verrouillage produit** — agent a reconnu le mode spécial "pas de bug à fixer, juste test de régression", a écrit 1 test concurrence. | #14 mergée |
| 2026-04-21 | #19 | INV-019 (positions 409) | ✅ **Fix code + test TDD strict** — issue humaine typique (symptôme + repro), agent a trouvé INV-019 sans ref, écrit test RED (prouve upsert silencieux), fix minimal 6 lignes `config.py` → 409 Conflict. Phase 4 merge auto validé. | #20 mergée via phase 4 |
| 2026-04-21 | #24 | INV-084 oncall_offline (délai paramétrable) | ✅ **Fix config dynamique** — migration hardcode `ONCALL_OFFLINE_DELAY_MINUTES` vers lecture `SystemConfig` à chaque tick. Constante renommée `_DEFAULT` en fallback. Aligné sur pattern existant. | #25 mergée via phase 4 |
| 2026-04-21 | #26 | INV-005 (monotonie escalation_count) | ✅ **Property-based hypothesis** — issue volontairement floue (style "Eric 2h du mat, pas dev"), agent a décodé le langage, audité 3 call sites muteurs, **première utilisation `hypothesis`** du projet (100 exemples aléatoires), mutation test manuel pour valider que le test détecte la violation. | #27 mergée via phase 4 |

**Bilan 5 pilotes** : 1 abandon propre (catalogue stale, pas un bug), 4 PRs mergées (test de verrouillage, fix code classique, migration config, test property-based). Tous via le cycle complet `ai:fix` → agent → `ai:approved` → phase 4 merge auto.

Bugs CI résolus pendant la stabilisation (visibles dans `docs/AI_STRATEGY.md §8bis`) :

- **CI-BUG-09** : cleanup silencieusement partiel entre runs → PRs bloquées en cascade. Fix : cleanup robuste + retry boot cluster (PR #10).
- **CI-BUG-10** : race condition tier 3 parce que `PROJECT=ci-wN` fixé par matrix, partagé entre runs parallèles. Fix : concurrency job-level par worker (PR #12).
- **CI-BUG-11** : `cancel-in-progress: true` annulait les runs utiles sur events `synchronize` (update-branch, gh pr merge). Fix : passage à `false`, le serialize tier 3 suffit à éviter le pile-up (PR #18).
- **Phase 4 bugs (3)** : ai-merge sans checkout, self-check dans rollup, up-to-date constraint. Tous fixés PR #21/22/23. Phase 4 validée en live sur les 3 pilotes qui ont fix+test.

Scale runners : 2 → 4 (2026-04-21) pour garantir le parallélisme intra-PR `tier3_e2e` (w1+w2 en simultané) même avec un autre run CI concurrent. RAM +~2 GB.

## Références

- Stratégie IA : [`docs/AI_STRATEGY.md`](../../docs/AI_STRATEGY.md)
- Catalogue d'invariants : [`tests/INVARIANTS.md`](../../tests/INVARIANTS.md)
- Audit catalogue (2026-04-20) : [`tests/INVARIANTS_AUDIT.md`](../../tests/INVARIANTS_AUDIT.md)
- Audit tests (bugs catalogués) : [`tests/audit_v2.json`](../../tests/audit_v2.json)
- Convention TDD projet : [`.claude/CLAUDE.md`](../../.claude/CLAUDE.md)
