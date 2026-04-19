# Runner self-hosted GitHub Actions — alarm-murgat-bot

Runner Docker pour exécuter les workflows tier 3 (cluster Patroni) du pipeline CI.
Tier 1+2 tournent sur `ubuntu-latest` (GitHub cloud, gratuit).

---

## Architecture

```
GitHub repo Alarm2.0
   │ long-poll HTTPS sortant
   ▼
docker compose (cette config)
   ├── gh-runner #1  (label: self-hosted,linux,docker,alarm-ci)
   └── gh-runner #2  (matrix tier 3 worker 1+2)
        │ DooD via /var/run/docker.sock
        ▼
   docker compose des tests (Patroni cluster)
```

- **Image** : `myoung34/github-runner:2.319.1` (épinglée).
- **Auth** : GitHub App `alarm-murgat-bot` (App ID + clé privée `.pem`).
- **Mode** : `EPHEMERAL=true` — chaque conteneur runner se détruit après 1 job.
- **DooD** : socket Docker du host monté → les conteneurs des tests tournent à côté, pas dedans.

---

## Setup local (Windows / Linux / WSL2)

### Prérequis

- Docker Desktop (Windows) ou Docker Engine (Linux), version ≥ 24.
- 4 Go RAM disponibles (2 runners + futurs conteneurs Patroni).
- Avoir créé la GitHub App `alarm-murgat-bot` (voir docs/AI_STRATEGY.md jalon J3).

### Étape 1 — Récupérer la clé privée

Tu dois avoir téléchargé un fichier `*.private-key.pem` lors de la création de la GitHub App.

Renomme-le et place-le ici :
```
infra/runner/alarm-bot.private-key.pem
```

⚠️ **Ce fichier ne doit JAMAIS être committé** (déjà dans `.gitignore`).

### Étape 2 — Créer le fichier .env

```bash
cp infra/runner/.env.example infra/runner/.env
```

Édite `infra/runner/.env` et remplace `APP_ID=000000` par ton vrai App ID.

### Étape 3 — Lancer le runner

Depuis la racine du repo :

```bash
docker compose -f infra/runner/docker-compose.runner.yml --env-file infra/runner/.env up -d
```

### Étape 4 — Vérifier

1. Logs :
   ```bash
   docker compose -f infra/runner/docker-compose.runner.yml logs -f
   ```
   Tu dois voir `Listening for Jobs` dans les 30s.

2. UI GitHub :
   - https://github.com/claude-murgat/Alarm2.0/settings/actions/runners
   - 2 runners doivent apparaître **"Idle"** avec le label `alarm-ci`.

### Étape 5 — Arrêter / nettoyer

```bash
docker compose -f infra/runner/docker-compose.runner.yml down
```

Les runners se désenregistrent automatiquement de GitHub (mode ephemeral).

---

## Migration vers VM OVH (jalon J10)

Procédure identique :
1. `git clone` du repo sur la VM.
2. Copier `alarm-bot.private-key.pem` et `.env` sur la VM (jamais via Git).
3. `docker compose -f infra/runner/docker-compose.runner.yml --env-file infra/runner/.env up -d`.

C'est tout. Aucune adaptation du compose.

---

## Sécurité

- La clé `.pem` est l'équivalent d'un mot de passe permanent : si elle fuit, **regénère-la immédiatement** sur la page de l'App.
- Le socket Docker monté donne au conteneur runner un accès **root effectif** au host. Acceptable car repo privé + équipe de confiance. Inacceptable sur repo public.
- Le mode `EPHEMERAL` est essentiel — sans lui, les variables d'env / fichiers d'un job précédent peuvent fuiter dans le suivant.
