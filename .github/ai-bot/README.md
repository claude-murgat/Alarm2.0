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

### Phase actuelle (phase 2 / 5) — `workflow_dispatch` manuel

```bash
# 1. Crée une issue qui décrit le bug (référence INV-XXX si possible)
gh issue create --title "Bug INV-XYZ — description" --body "..."

# 2. Note le numéro d'issue retourné (ex: 42)

# 3. Déclenche le workflow
gh workflow run ai-bot.yml -f issue_number=42

# 4. Suis le run
gh run watch --exit-status
```

### Phase 3 (à venir) — trigger auto sur label

Il suffira de poser le label `ai:fix` sur une issue pour que le bot démarre.

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

### (c) Violation denylist (phase 3 à venir)

Si l'agent essaie quand même de modifier un fichier interdit, un step CI
post-run détecte la violation et fail le run sans créer de PR.

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
| `ai:abandoned` | bot | « J'ai renoncé, reprise humaine requise » |
| `ai:needs-human` | bot | « Fix nécessite un fichier denylist ou ambiguïté » |
| `ai:denied` | bot | « J'ai tenté une violation denylist, CI m'a stoppé » (phase 3) |

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

## Références

- Stratégie IA : [`docs/AI_STRATEGY.md`](../../docs/AI_STRATEGY.md)
- Catalogue d'invariants : [`tests/INVARIANTS.md`](../../tests/INVARIANTS.md)
- Audit tests (bugs catalogués) : [`tests/audit_v2.json`](../../tests/audit_v2.json)
- Convention TDD projet : [`.claude/CLAUDE.md`](../../.claude/CLAUDE.md)
