# alarm-sre-bot (POC)

Agent SRE conversationnel pour le système Alarme Murgat. L'utilisateur DM le
bot sur Slack, il diagnostique en autonomie et corrige (restart, kill, patroni
reinit…) sans toucher au code.

## Architecture

```
[Slack DM] ──► main.py (Socket Mode) ──► agent.py (Claude loop)
                                              │
                                              ├──► tools.py (run_command, ask_user, …)
                                              ├──► executor.py (SSH + LOCAL)
                                              │      │
                                              │      └──► policy.py (allowlist L1/L2/L3)
                                              │
                                              └──► audit.py (audit.log JSON append-only)
```

- **1 thread Slack = 1 incident** (`IncidentSession` en mémoire)
- **Le bot ne touche pas au code** (pas d'`Edit`/`Write`/`git`). Seul tool
  d'action = `run_command` matché contre une allowlist regex.
- **Hors allowlist** → refus + escalade Slack DM au sysadmin.

## Niveaux d'action (policy.py)

| Niv | Exemples | Politique |
|---|---|---|
| L1 | `docker logs`, `psql SELECT`, `curl /health`, `ps`, `journalctl` | Auto |
| L2 | `kill <pid>`, `docker restart <c>`, `systemctl restart alarm-*` | Auto + report user |
| L3 | `patronictl reinit`, `patronictl switchover`, `kill -9` | Auto + report user (annonce avant) |
| L4 | tout le reste | **Refus** → escalade |

Toutes les commandes sont sandbox-ées par regex. Pour étendre, modifier
`policy.py` et tester avec `python -c "from policy import classify; print(classify('docker restart x'))"`.

## Setup

```bash
cd /home/murgat/dev/alarm-sre-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Éditer .env avec les vraies valeurs (tokens Slack, clé Anthropic)
```

### Créer la Slack App

1. https://api.slack.com/apps → "Create New App" → "From scratch" → workspace
   "Charles Murgat".
2. **Socket Mode** : ON → générer un App-Level Token avec scope
   `connections:write` → coller dans `.env` comme `SLACK_APP_TOKEN=xapp-...`
3. **OAuth & Permissions** → Bot Token Scopes :
   - `chat:write`
   - `im:history`
   - `im:read`
   - `app_mentions:read`
4. **Event Subscriptions** : ON → Subscribe to bot events :
   - `message.im` (DM au bot)
   - `app_mention`
5. **Install App** → copier le Bot User OAuth Token (`xoxb-...`) dans
   `.env` comme `SLACK_BOT_TOKEN`.
6. DM le bot dans Slack pour lancer une session.

## Run

```bash
source .venv/bin/activate
python main.py
```

Logs runtime sur stdout, audit JSON dans `audit.log` (chemin configurable
via `AUDIT_LOG_PATH`).

## Test manuel rapide (sans Slack)

```bash
source .venv/bin/activate
python -c "
from policy import classify
for cmd in [
  'docker ps',
  'docker logs node3-backend-1',
  'kill 12345',
  'rm -rf /',
  'docker restart node2-patroni-1',
]:
  d = classify(cmd)
  print(f'{d.level.name:3} | {cmd[:60]:60} | {d.matched_rule_description}')
"
```

## Limitations connues (POC)

- **State perdu au restart du bot** : les sessions en mémoire disparaissent.
  Pour persister, sérialiser `SESSIONS` dans un fichier au stop / charger
  au start (ou Redis si on veut faire propre).
- **Pas de timeout d'incident** : un thread reste "ouvert" jusqu'à
  `finish`/`escalate`. À borner (ex: auto-close après 2h sans activité).
- **Pas de tests** : ajouter `tests/test_policy.py` au minimum avant prod
  (matrice cas autorisés / refusés).
- **Pas de rate limiting** : un user spammeur peut faire exploser la facture
  Anthropic. Limiter par user/jour.
- **Pas de garde sur la concurrence** : si plusieurs incidents simultanés,
  threading.Lock global sur `SESSIONS` mais pas sur l'exécution agent —
  ça peut tourner. À surveiller.

## Affiner après le POC

- Tests unitaires policy
- Persistance sessions
- Auto-close incidents inactifs
- Notebook/page web pour relire les audits par incident
- Métriques (Prometheus) : nb incidents, taux de résolution auto, taux d'escalade
- Mode "shadow" : le bot diagnostique mais n'exécute pas, juste propose — utile
  pour calibrer la policy en début
