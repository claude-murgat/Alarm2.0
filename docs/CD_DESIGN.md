# Design — Continuous Deployment V1 (Phase 3)

> **Statut** : design proposal, pas implémenté. Sortie d'une session du 2026-05-09.
> **Sources lues** : `docs/PROVISIONING_ONSITE.md`, `docs/AI_STRATEGY.md`, `infra/onsite/peers.md`,
> `docker-compose.yml` + `.env.prod.node{1,2,3}` + `scripts/start-prod-node.sh` (PR #51 mergée 2026-05-09),
> `.github/ai-bot/README.md`, `.github/workflows/{pr,ai-bot,tier4-failback}.yml`,
> `tests/INVARIANTS.md`, `android/INVARIANTS.md`.
>
> **Successeur** : Phase 4 CD V2 (auto-fix prod par bot IA après ≥10 PRs propres). Hors scope ici.

---

## 0. Contexte, contraintes, TL;DR

### Topologie cible (cf §22bis du provisioning)

```
                  Internet
                     │
           ┌─────────┼──────────┐
           │         │          │
       NODE3 cloud   NAT site (31.204.85.180)
       OVH OPNsense   │          │
       (10.99.0.1)    │          │
                  ┌───┴────┐ ┌───┴────┐
                  │onsite-1│ │onsite-2│
                  │10.99.0.2│ │10.99.0.3│
                  │i5 8 Go  │ │Cel 4 Go │
                  └─────────┘ └─────────┘
                  Mesh Wireguard 10.99.0.0/24 (3-way OK)
                  RTT LAN < 1 ms, vers NODE3 16.6 ms
```

Chaque nœud lance la **même stack** : etcd + patroni + backend FastAPI (+ mailhog désactivé en prod).
Le cluster Patroni (3 etcd, 3 Patroni, 3 backends) **n'a jamais encore été démarré en prod**.
Le `start-prod-node.sh <1|2|3>` fait `docker compose up --build -d` — donc rebuild local sur chaque nœud
à chaque déploiement. Pas de CD aujourd'hui.

### Invariants critiques à ne pas casser

Sélection en lecture de `tests/INVARIANTS.md` + `android/INVARIANTS.md` qui contraignent **directement** le design CD :

| ID | Catalogue | Contrainte sur le déploiement |
|---|---|---|
| INV-090 [C] | backend | Toujours **exactement 1 primary** Patroni → ne jamais perdre 2 etcd simultanément (perte quorum) |
| INV-091 [C] | backend | Failover < 60 s — un primary stoppé est tolérable si la bascule est rapide |
| INV-085 [C] 🐛 | backend | Perte quorum cluster → email direction technique. Un déploiement maladroit qui perd le quorum déclenche un email opérationnel ; à éviter |
| INV-076 [C] ❌ | backend | `ENABLE_TEST_ENDPOINTS=false` en prod : la promotion `:stable` doit garantir que cette variable est bien `false` dans l'image qu'on promeut (ou plutôt dans le `.env.prod.nodeX`, qui est OK aujourd'hui — mais à valider à chaque promo) |
| INV-094 [M] | backend | Persistance après restart : volumes `pg_data` et `etcd_data` doivent être préservés (le `docker compose down -v` est interdit pendant un déploiement — uniquement `up -d` sur l'image qui a changé) |
| INV-ANDROID-302 [C] | android | Perte heartbeat > **2 min** → sonnerie locale chez tous les opérateurs. **Conséquence directe** : à tout moment du déploiement, **au moins 1 backend doit être joignable** par les apps Android. Donc pas de redémarrage simultané des 3 backends. |
| INV-ANDROID-400 [C] | android | 3 échecs polling consécutifs (~9 s à 3 s polling) → `switchToNextUrl()`. Ce design **fonctionne avec un déploiement séquentiel** un nœud à la fois |
| INV-ANDROID-402 [C] | android | Rotation circulaire sur 3 URLs (primary + 2 fallbacks). Le client tolère parfaitement qu'1/3 backends soit down |

**Conclusion stratégique** : déploiement séquentiel obligatoire, **un nœud à la fois**. Jamais 2 nœuds en cours d'update simultanément (quorum etcd ET disponibilité backend pour les apps).

### Hors scope V1

- Reboot OS coordonné (déjà documenté §19 du provisioning, à instrumenter en parallèle).
- Migration de schéma DB (Alembic ou autre) — sera traitée séparément ; le CD V1 suppose des images backwards-compatibles sur le schéma.
- Déploiement de l'app Android (release Play Store reste manuel, hors topologie cluster).
- Auto-fix prod par bot IA (Phase 4).
- Multi-arch arm64 (Raspberry Pi pas dans la roadmap V1).

### TL;DR

| Sujet | Recommandation |
|---|---|
| **Build** | GHCR (`ghcr.io/claude-murgat/alarm-{backend,patroni}`), images x86_64, tag `:sha-<short>` à chaque push master |
| **Promotion** | Manuelle V1, `workflow_dispatch` avec input `sha`, prérequis : tier 1+2+3 verts + tier 1.5 mutation 100% + ≥ 24 h depuis le merge |
| **Pull** | Systemd timer **5 min** par nœud, comparaison digest distant `:stable`. Pull-only (le restart est une étape séparée et coordonnée) |
| **Canary** | NODE3 → onsite-2 → onsite-1, soak **10 min** entre chaque + healthcheck `/health` + replication lag `< 100 ms` + pas de churn leader Patroni |
| **Rollback** | Auto si `/health` 503 × 3 / 60 s OU restart-loop OU lag > 30 s. Garde **K=5** digests précédents par nœud, revert via `docker tag <prev-digest> alarm-backend:stable` puis `up -d` |
| **Observabilité** | Table Postgres `deployment_events` (réplication native), email à `direction_technique@charlesmurgat.com` sur rollback ou promo `:stable`. Pas de Grafana V1 |
| **Bot IA — mode normal** | Bot ne promeut pas `:stable`. Frontière de blast-radius pour les fix non-urgents |
| **Bot IA — mode urgence** | **Auto-promo activée si le service est mesurablement cassé** (cf §7). Filet de sécurité = suite de tests (tier 1.5 mutation 100 % bloquant). Fallback ultime = opérateur de garde appelle l'admin hors-bande |

---

## 1. Build des images

### Recommandation

**Registry** : **GHCR** (`ghcr.io/claude-murgat/alarm-backend`, `…/alarm-patroni`).
**Tags publiés** :
- `:sha-<short>` (7 caractères) sur **chaque push master**, traçabilité 1:1 avec le commit
- `:stable` (mutable, pointé par promotion manuelle, cf §2)
- `:rollback-<short>` (mutable, mis en place par l'auto-rollback, cf §5)

**Trigger** : workflow `.github/workflows/build.yml` déclenché sur :
- `push: branches: [master]` → build + push `:sha-<short>`
- `workflow_dispatch` avec `sha` → idempotent (re-build d'un sha existant si re-tag souhaité)

Pas de build sur PR (les images preview ne servent pas à grand-chose tant qu'on n'a pas de staging).

**Architecture** : x86_64 only, mais utiliser `docker buildx` dès le départ pour pouvoir ajouter `linux/arm64` plus tard sans changer le workflow.

**Services à builder** : `backend` (`./backend/Dockerfile`) + `patroni` (`./Dockerfile.patroni`). `etcd` et `mailhog` restent des images upstream pinnées dans `docker-compose.yml` (`quay.io/coreos/etcd:v3.5.17`, `mailhog/mailhog:latest` — à pinner sur un digest avant le passage en prod).

### Alternatives considérées

- **Docker Hub** : limites de pull anonymous (100/6h par IP) gênent les nœuds derrière le NAT site. Compte authentifié possible mais ajoute un secret. **Rejeté**.
- **Self-hosted registry sur NODE3 cloud OVH (75 Go libres)** : control total, latence faible pour onsite via WG, mais SPOF (NODE3 down = pas de pull → CD bloqué jusqu'à remise en route). À reconsidérer en V2 si on veut une indépendance vis-à-vis de GitHub. **Différé**.
- **Build local sur chaque nœud (statu quo)** : marche, mais (a) build sur Celeron 2c onsite-2 est lent, (b) 3 builds en série multiplient le risque qu'un échoue à mi-chemin et laisse le cluster désynchronisé. **Rejeté**.
- **Tag `:latest`** : ambigu (pointe sur quoi ? `:stable` ? le dernier sha ?). **Rejeté** — ne pas le publier du tout pour forcer l'explicit.
- **Tag `:semver`** : pas de release process formel pour l'instant, on push 5-10 commits par jour. **Différé** ; on pourra ajouter plus tard sans casser le `:sha-<short>`.

### Questions ouvertes

- **Q1.1** Visibility GHCR : public ou privé ? Le repo source est public, donc public est cohérent et évite les `imagePullSecrets`. À confirmer avec l'utilisateur — y a-t-il un secret/token dans l'image backend qui ne devrait pas être lisible par le monde ? (Vérification rapide : `firebase-service-account.json` est mounted depuis le host, **pas dans l'image** ; OK pour public.)
- **Q1.2** Retention GHCR : combien de `:sha-<short>` garder ? GHCR ne purge pas auto. Proposition : garder les 50 derniers + tous ceux qui ont jamais été `:stable`. À automatiser via un workflow nightly de cleanup.
- **Q1.3** Dockerfile.patroni utilise-t-il `apt`-pinning ou non ? À auditer pour garantir des builds reproductibles (sinon un même `:sha-<short>` rebuild peut produire un binaire différent).
- **Q1.4** Doit-on signer les images (`cosign` + GitHub OIDC) dès V1, ou différer en V2 ? Coût marginal en CI, gain : impossible pour un attaquant de pousser une fausse image avec le bon tag.

---

## 2. Modèle de promotion `:stable`

### Recommandation (V1.6, depuis 2026-05-28)

**Automatique** via trigger `workflow_run` du workflow `.github/workflows/cd-promote-stable.yml` :

- Se déclenche dès que le workflow **CI** termine en succès sur `master`
- Vérifie aussi **CD Build** et **CD Install Smoke** verts sur le même sha (wait 5 min max)
- Re-tag atomique sur GHCR : `docker buildx imagetools create -t .../alarm-backend:stable .../alarm-backend:sha-<short>` (idem patroni)
- `concurrency: cd-promote-stable, cancel-in-progress: false` : promos sérialisées, aucun sha qui passe CI n'est sauté

**Prérequis vérifiés par le workflow avant la promo** :

1. ✅ CI verte sur master @ sha (garanti par le trigger `workflow_run`)
2. ✅ CD Build + CD Install Smoke verts sur le même sha (poll 5 min max)
3. ✅ Le sha existe sur master (paranoia)
4. ✅ Pas de revert de ce sha dans `git log master`
5. ✅ Images `:sha-<short>` existent sur GHCR (cd-build a publié)

Si tous OK : re-tag :stable, POST `kind=manual_override` `actor=github-actions:auto-promote` dans `deployment_events` (si `vars.CD_API_BASE` configuré — V1 limitation : runner GH-hosted hors mesh WG, donc step skip aujourd'hui ; les events sont posés par les scripts canary depuis NODE3 ensuite).

**Philosophie "tout ce qui a passé CI est safe"** : le canary auto + soak 10 min + rollback auto (cf §4-5) absorbent le résidual risk. Le gate manuel V1.0 avait du sens quand canary était lui-même manuel — depuis V1.5 (PR #126, orchestrateur auto 3→2→1), il devient friction inutile.

**Qui peut promouvoir** : implicitement, **tout merger sur master**. Le bot IA `alarm-murgat-bot[bot]` ne peut pas merger sans label `ai:approved` humain (cf `.github/workflows/ai-merge.yml`) → garantit que le bot ne déclenche pas indirectement la promo. Check `actor != bot` reste actif dans le workflow en défense en profondeur (cf §7).

**Notification post-promo** : event SQL `manual_override` actor `github-actions:auto-promote` dans `deployment_events` (visible dans dashboard `/admin/deployments`) + step summary GH lisible. (V2 envisagé : email automatique, cf §7.)

### Alternatives considérées

- **Promo manuelle V1.0 (workflow_dispatch + input `confirm: PROMOTE`)** : abandonnée 2026-05-28. Le gate manuel était utile quand canary V1.0 était lui-même manuel — l'opérateur avait une décision "go/no-go" cohérente. Depuis V1.5 (canary auto, PR #126), il devient redondant. En pratique 3 PRs (#129, #130, #131) ont attendu 24h sans que personne ne surveille la fenêtre → friction sans bénéfice.
- **Cooldown 24h** : supprimé en V1.6. La fenêtre était censée laisser passer tier 4 nightly + observer une éventuelle régression — mais aucun observateur dédié n'existait, et le canary soak 10 min couvre déjà la régression rapide.
- **Promo auto après tier 1-4 verts + N heures d'observation** : tier 4 nightly couvre le failback E2E spécifiquement, pas le runtime applicatif. Pas nécessaire en gate.
- **Label PR `promote-stable`** : élégant mais couple la décision au moment du merge. Auto sur CI green = décision déléguée à la CI, plus simple.
- **Staging env dédié** : NODE3 seul avant onsite. Hors scope V1, repris en Q2.2.

### Questions ouvertes

- **Q2.1** ~~Cooldown 24 h~~ — supprimé 2026-05-28 (cf alternatives).
- **Q2.2** Staging env dédié (NODE3 seul pendant N heures avant onsite-1/2) — toujours valide. Discussion à rouvrir si une régression non détectée par CI atteint prod après V1.6.
- **Q2.3** Smoke tests post-promo (créer une alarme test, l'acquitter, vérifier logs) — toujours valide, V2.

---

## 3. Mécanisme de pull côté nœuds prod

### Recommandation

**Systemd timer** `alarm-cd-pull.timer` toutes les **5 min** par nœud, qui exécute `alarm-cd-pull.service` :

```bash
#!/bin/bash
# /usr/local/bin/alarm-cd-pull.sh
set -euo pipefail
LOCK="/run/alarm-cd-pull.lock"
exec 9>"$LOCK"
flock -n 9 || { echo "pull already running"; exit 0; }

REGISTRY="ghcr.io/claude-murgat"
for IMG in alarm-backend alarm-patroni; do
  REMOTE_DIGEST=$(docker buildx imagetools inspect "$REGISTRY/$IMG:stable" --format '{{.Manifest.Digest}}')
  LOCAL_DIGEST=$(docker image inspect "$REGISTRY/$IMG:stable" -f '{{index .RepoDigests 0}}' 2>/dev/null | cut -d'@' -f2 || echo "none")
  if [[ "$REMOTE_DIGEST" != "$LOCAL_DIGEST" ]]; then
    docker pull "$REGISTRY/$IMG:stable"
    # Journaliser dans deployment_events (cf §6) via psql sur le primary local
    /usr/local/bin/alarm-cd-event pull "$IMG" "$LOCAL_DIGEST" "$REMOTE_DIGEST"
  fi
done
```

**Important — pull et restart sont DEUX opérations distinctes** :
- Le timer ne fait **que pull**, jamais `docker compose up`. Pull est innocuous (juste télécharger).
- Le restart (canary) est piloté par un orchestrateur séparé (cf §4) qui appelle `start-prod-node.sh` au bon moment, dans le bon ordre.

**Lock file** `/run/alarm-cd-pull.lock` (`flock`) pour éviter (a) deux pulls concurrents, (b) un pull en plein milieu d'un `start-prod-node.sh` manuel.

**Pourquoi 5 min et pas 1 min** : 1 min augmente la charge GHCR API et n'apporte rien (la promo `:stable` est manuelle, pas une course de seconde). 5 min est une fenêtre acceptable entre promo et propagation. Si plus rapide est demandé, le workflow de promo peut **trigger** le pull via webhook (à instrumenter en V2).

### Alternatives considérées

- **Watchtower** : 3rd party qui pull + restart auto. Pas de canary order (tous les conteneurs du host updatent ensemble), pas de respect du quorum etcd, pas de logique d'auto-rollback compatible avec INV-091. **Rejeté** — incompatible avec un cluster Patroni.
- **Polling toutes les 1 min** : voir au-dessus, charge sans bénéfice.
- **Webhook GitHub → endpoint sur chaque nœud** : nécessite d'exposer un port internet sur chaque onsite (ou via WG depuis NODE3 cloud relais). Complexité réseau + secret à partager. **Différé V2**.
- **Pull bloquant (interactive)** : pas pertinent ici puisque ce n'est qu'un download, pas un restart.

### Questions ouvertes

- **Q3.1** Faut-il pull `:sha-<short>` aussi (pour chauffer le cache local au cas où on doive rollback rapidement) ou uniquement `:stable` + le précédent ? Proposition : `:stable` toujours + les 5 derniers `:sha-<short>` qui ont déjà été `:stable` (= les rollbacks possibles).
- **Q3.2** Si GHCR est temporairement injoignable (panne GitHub), le timer doit logger un warning sans casser. Délai max d'intolérance avant alerte ? Proposition : 6 h sans pull réussi → email.
- **Q3.3** Faut-il authentifier le pull (`docker login ghcr.io` avec un PAT lecteur) même si l'image est publique, pour éviter le rate-limit anonymous ? Probablement oui — coût ~0.

---

## 4. Ordre de canary

### Recommandation

**Ordre** : **NODE3 (cloud)** → **onsite-2** → **onsite-1**.

Justification :
- **NODE3 d'abord** : seul nœud hors-site, le moins critique pour les apps Android (qui ont 2 backends LAN restants en cas de souci). Si la nouvelle image plante, l'impact métier est minimal.
- **onsite-2 ensuite** : Celeron 2c / 4 Go RAM — le maillon faible. Si la nouvelle image consomme trop de mémoire ou de CPU, on le voit ici en premier (plus tôt qu'en dernier).
- **onsite-1 en dernier** : i5 4c / 8 Go, machine la plus puissante. Probablement le primary Patroni le plus souvent (Patroni élit en fonction des LSN). Le mettre à jour en dernier minimise le nombre de bascules de leader pendant le déploiement.

**Soak time** entre chaque étape : **10 min minimum**, prolongé tant que l'un des critères ci-dessous n'est pas vert :

| Critère | Source | Seuil |
|---|---|---|
| `/health` 200 OK | backend `:8000/health` | 100 % sur 10 min consécutives, sample toutes les 30 s |
| etcd peer health | `etcdctl --endpoints=10.99.0.{1,2,3}:2379 endpoint health` | les 3 endpoints `is healthy: true` |
| Patroni replication lag | `GET :8008/cluster` champ `lag` | < **100 ms** sur tous les replicas |
| Pas de churn leader | `GET :8008/cluster` champ `Leader` | Leader inchangé pendant les 5 dernières min |
| 5xx rate backend | comptage simple sur `nginx`/access log ou métrique custom | < 1 % sur 5 min |
| Pas de redémarrage de container | `docker inspect <id> | jq .RestartCount` | Stable depuis le début du soak |

**Promotion d'une étape à la suivante** :

- **V1 conservatif** : la 1re promotion (NODE3 → onsite-2) est **manuelle** — l'opérateur regarde le rapport de soak puis lance la commande qui bascule le 2e nœud. Idem onsite-2 → onsite-1.
- **V1.5 (à activer après 5 déploiements clean)** : promotion **auto** après soak vert. Bouton « pause » disponible.

L'orchestration tourne **depuis NODE3 cloud** (`alarm-cd-orchestrator.service` qui SSH par WG vers chaque nœud onsite pour déclencher `start-prod-node.sh` ; alternative : `systemctl --remote` via une socket Unix exposée — moins éprouvé).

### Alternatives considérées

- **Ordre inverse (onsite-1 → onsite-2 → NODE3)** : la machine la plus puissante en premier semble logique, mais expose le maillon métier (LAN site, primary probable) en premier. Si la nouvelle image plante, panne immédiate côté apps Android. **Rejeté**.
- **Soak fixe 1 h** : trop long pour V1, ralentit chaque déploiement à 3 h. À reconsidérer si on découvre des bugs latents qui n'apparaissent qu'après 30 min de charge.
- **Soak 5 min** : insuffisant pour observer la replication Patroni stabilisée et la consommation mémoire backend en régime établi.
- **Promotion auto dès V1** : risqué — pas de baseline metrics, pas d'alerting hors email. Préférable de garder l'humain dans la 1re boucle pendant les 5 premiers déploiements.

### Questions ouvertes

- **Q4.1** Si NODE3 est **actuellement primary Patroni** quand on commence le canary, faut-il forcer un `failover` Patroni vers onsite-1 ou onsite-2 **avant** de redémarrer NODE3, pour éviter une bascule pendant le restart ? `patronictl switchover --master node3 --candidate node1` est explicite et < 5 s. **Forte recommandation**.
- **Q4.2** Le replication lag < 100 ms est-il atteignable de façon fiable depuis NODE3 cloud (RTT 16.6 ms vers onsite) ? À mesurer une fois le cluster up. Si non, relâcher à 1 s pour NODE3 spécifiquement.
- **Q4.3** Si onsite-2 est **simultanément** le primary Patroni au moment où on veut l'updater (étape 2/3), même question — switchover préventif ? Ou bien on fait confiance à INV-091 (failover < 60 s tolérable) ?
- **Q4.4** Comment gérer un déploiement qui touche **uniquement le backend** vs un qui touche **patroni** (Postgres) ? Le restart patroni nécessite un pg_basebackup au boot du replica (cf compose `start_period: 60s`). Le canary doit tolérer cette fenêtre. Proposition : critère soak `replication lag` reporté de 90 s pour les déploiements qui touchent l'image patroni.

---

## 5. Auto-rollback

### Recommandation

**Triggers** (OR logique, n'importe lequel déclenche le rollback automatique de l'étape canary en cours) :

| Trigger | Source | Seuil | Anti-flapping |
|---|---|---|---|
| `/health` 503 répété | backend local | ≥ 3 sur 60 s consécutifs | Délai de grâce 60 s post-restart |
| Conteneur restart-loop | `docker inspect` | ≥ 3 restarts en 5 min | — |
| etcd local rouge | `etcdctl endpoint health` localhost | rouge ≥ 60 s | — |
| Replication lag explose | `GET :8008/cluster` | > 30 s sur ce nœud ≥ 60 s | — |
| 5xx rate backend | métrique | > 5 % sur 60 s | Délai de grâce 60 s post-restart |

**Mécanisme** :

Avant le `up -d` de la nouvelle image, le script canary :
1. Lit le digest local courant : `docker inspect ghcr.io/.../alarm-backend:stable -f '{{index .RepoDigests 0}}'`
2. Sauvegarde dans `/var/lib/alarm/cd/last-stable-digest` (1 ligne par image)
3. Tag local : `docker tag <current-digest> ghcr.io/.../alarm-backend:rollback-prev` (pour l'avoir prêt)
4. `docker compose --env-file .env.prod.nodeX up -d` avec la nouvelle image

En cas de rollback :
1. `docker tag <last-stable-digest> ghcr.io/.../alarm-backend:stable` (re-pointe le tag local)
2. `docker compose --env-file .env.prod.nodeX up -d` (relance avec l'ancienne image)
3. Logger `kind=rollback` dans `deployment_events` (cf §6) avec les 5 dernières lignes de log de la nouvelle image en `details_json`
4. Email à `direction_technique@charlesmurgat.com`
5. **Halte du canary** : les nœuds restants ne sont pas updatés, pour ne pas répliquer le bug
6. **Hystérésis** : pas de nouvelle tentative de pull `:stable` sur ce nœud pendant **1 h** (anti-boucle si le nouveau `:stable` est cassé)

**Rétention K = 5 digests** par image, par nœud :
- Stockage : `~600 Mo backend × 5 + ~400 Mo patroni × 5 ≈ 5 Go` — tenable même sur les 25 Go libres d'onsite-2
- Fichier index : `/var/lib/alarm/cd/digests.tsv` (`timestamp\timage\tdigest\tpromoted_at`)
- Pruning hebdomadaire : conserver les 5 plus récents qui ont jamais été `:stable`

**Restriction** : auto-rollback **par nœud uniquement**. Pas de rollback global du cluster ; chaque nœud rollback s'il détecte une régression locale, et le canary s'arrête. C'est cohérent avec le design séquentiel — au pire, on a 1 nœud sur ancienne version, 2 sur nouvelle, ce qui est la situation pendant un canary OK.

### Alternatives considérées

- **Conserver 2 digests seulement** : insuffisant si un bug latent apparaît après 2 promotions. K=5 = ~1-2 semaines d'historique. **K=5 retenu**.
- **Rollback sans hystérésis** : risque de boucle infinie pull → restart → rollback → pull. **Rejeté**.
- **Rollback global synchrone** : tentant pour cohérence, mais incompatible avec le quorum etcd (3 restarts simultanés = quorum perdu). **Rejeté**.
- **Trigger rollback sur replication lag > 5 s** : trop sensible pendant le pg_basebackup d'un replica. **30 s retenu**.

### Questions ouvertes

- **Q5.1** Si le nouveau `:stable` est cassé au point de planter le primary Patroni (qui était sur le nœud canary), le rollback du nœud peut **lui-même** déclencher une bascule de leader. Cette bascule peut elle-même temporairement faire passer `/health` à 503 sur les autres nœuds. **Ne pas confondre** un faux positif post-bascule avec un vrai rollback à propager. Solution proposée : période d'observation de 60 s post-rollback avant d'évaluer les autres nœuds.
- **Q5.2** Que faire si rollback **lui-même** échoue (l'ancienne image elle aussi cassée, par ex parce qu'un schéma DB a changé entre temps) ? Réponse défaut : alerte critique (email + log `kind=abort`), nœud laissé dans son état actuel, intervention humaine. **À documenter dans un runbook** avant V1 prod.
- **Q5.3** L'auto-rollback doit-il considérer la **télémétrie du client Android** (taux d'erreurs vu côté apps) ? Élégant mais nécessite une remontée client → backend. **Différé V2**.

---

## 6. Observabilité du déploiement

### Recommandation

**Persistance** : table Postgres `deployment_events` dans la même DB que les alarmes (`alarm_db`). Schéma proposé :

```sql
CREATE TABLE deployment_events (
  id           BIGSERIAL PRIMARY KEY,
  ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
  node         TEXT NOT NULL,                -- 'node1' | 'node2' | 'node3'
  image        TEXT NOT NULL,                -- 'alarm-backend' | 'alarm-patroni'
  kind         TEXT NOT NULL,                -- 'pull' | 'canary_start' | 'canary_promoted'
                                             -- | 'rollback' | 'abort' | 'manual_override'
  from_digest  TEXT,                         -- digest de l'image avant action
  to_digest    TEXT,                         -- digest de l'image après action
  status       TEXT NOT NULL,                -- 'success' | 'failure' | 'in_progress'
  actor        TEXT,                         -- 'systemd-timer' | github user | 'auto-rollback'
  details_json JSONB                         -- logs, lag, stack trace, etc.
);
CREATE INDEX ON deployment_events (ts DESC);
CREATE INDEX ON deployment_events (node, kind, ts DESC);
```

**Avantages** :
- Réplication native Patroni (cohérent avec INV-100 audit_events).
- Survit aux redémarrages de cluster (cf INV-094).
- Requêtable depuis n'importe quel backend pour le dashboard `/admin/deployments`.

**Notification** :
- **Email** à `direction_technique@charlesmurgat.com` sur :
  - `kind=rollback` (toujours)
  - `kind=abort` (toujours)
  - `kind=canary_promoted` avec `node='node1'` (= dernier nœud → déploiement complet)
  - `kind=manual_override` (l'humain a forcé un truc)
- Pas de webhook Slack V1 (pas configuré dans le projet).
- **Pas de Push FCM aux apps Android sur rollback**. INV-ANDROID-600/601 décrivent les FCM data messages — rajouter un type `deployment_status` serait techniquement faisable mais hors scope V1 (rationale : le rollback automatique remet l'app dans l'état précédent qui marchait, donc rien à signaler côté client ; et un rollback réussi ne devrait causer aucune dégradation perceptible).

**Dashboard** :
- Endpoint `/admin/deployments` (HTML simple, comme `index.html`) qui interroge la table et affiche : timeline des events, état canary actuel par nœud, historique des rollbacks.
- Endpoint `/api/deployment/state` (JSON) consommé par le dashboard.
- **Pas de Grafana V1** — installation prévue côté NODE3 cloud (cf §17 du provisioning) mais non bloquant pour CD V1.

**Auth** : table protégée par `is_admin=True` (INV-077). Pas de modif via API ; seul l'orchestrateur insère, via psql en local.

### Alternatives considérées

- **journald only** : pas requêtable cross-nœud, ne survit pas à un reboot prolongé (rotation 2 Go cf §8 du provisioning), pas accessible depuis le dashboard. **Rejeté** comme source unique mais **gardé en parallèle** (chaque action écrit aussi un log structuré dans journald local, double écriture pour debug).
- **GitHub Deployments API** : standard, mais (a) nécessite que l'orchestrateur soit GitHub-aware, (b) pas requêtable depuis le backend pour le dashboard interne, (c) pas de réplication ni de durabilité on-prem. **Rejeté** comme source de vérité, **éventuellement** miroir read-only V2.
- **Slack webhook** : aucun canal Slack du projet aujourd'hui. **Différé**.
- **Dashboard Grafana** : préférable mais nécessite l'installation côté NODE3 cloud. **Différé**, pas bloquant V1.

### Questions ouvertes

- **Q6.1** Faut-il loguer aussi les pulls **sans changement de digest** (les 95 % de hits du timer toutes les 5 min) ? Probablement non — bruit sans valeur. Proposition : logguer uniquement les pulls qui changent le digest local.
- **Q6.2** Quel email exact ? `direction_technique@charlesmurgat.com` est référencé pour les unattended-upgrades (§6 du provisioning). À confirmer s'il est bien la bonne adresse pour les events CD aussi.
- **Q6.3** Doit-on exposer `deployment_events` aux apps Android pour qu'elles affichent un bandeau « déploiement en cours » ? Probablement non V1 — l'app est invisible pour l'opérateur la plupart du temps, et le canary ne devrait pas être perceptible.
- **Q6.4** Combien de temps garder les events ? Proposition : 90 jours, idem audit_events (à confirmer). Pruning par batch nightly.

---

## 7. Articulation avec le bot IA (Phase 4 anticipée)

### Recommandation

**Deux modes de fonctionnement, distincts par état du système** :

#### Mode normal (système en bon état)

- ✅ Le bot peut **commiter** sur `master` via PR + `ai:approved` (mécanisme actuel).
- ✅ Le bot peut **merger** automatiquement (Phase 4 prévue) après ≥ 10 PRs propres consécutives, **sans poser le label** `ai:approved`.
- ❌ Le bot **ne déclenche pas** la promotion `:stable`. Réservé aux humains.
- ❌ Le bot ne modifie pas `infra/`, `.github/workflows/`, `docker-compose.yml`, `tests/INVARIANTS.md` (denylist `.github/ai-bot/denylist.txt` existante).

Le bot opère **avant** la frontière de promotion. Logique : un fix non-urgent peut attendre la validation humaine du matin sans coût opérationnel.

#### Mode urgence (système mesurablement cassé)

Si le service est **mesurablement dégradé** au moment où le bot publie un fix, la balance bénéfice/risque s'inverse : laisser le système cassé toute la nuit est **plus dangereux** que pousser un hotfix non-revu (le risque downside est plafonné par un service déjà KO).

**Conditions de déclenchement (toutes requises)** :

**Condition 0 — préfiltre réseau (obligatoire avant tout)**

Une panne réseau (NAT site down, partition WG, mauvaise route ISP) **ressemble** à une panne de service du point de vue d'un observateur unique. Si le détecteur croit à une panne logicielle alors que c'est du réseau, il déploie un hotfix qui ne résoudra rien et qui peut empirer (image légèrement différente sur un cluster déjà fragmenté = risque de désync).

Avant de juger l'état applicatif, le détecteur **doit prouver** que ses propres voies de communication fonctionnent :

- `ping -c 3 -W 2 1.1.1.1` ET `ping -c 3 -W 2 9.9.9.9` (Cloudflare + Quad9 — déjà sources NTP, cf §7 du provisioning) — **2/2** doivent répondre
- `ping -c 3 -W 2 10.99.0.2` ET `ping -c 3 -W 2 10.99.0.3` (mesh WG vers les 2 onsite) — **2/2** doivent répondre
- `curl -fsS --max-time 5 https://api.github.com/zen` (joindre GitHub depuis NODE3, prérequis pour re-tag GHCR ensuite)

Si **n'importe lequel** échoue → le détecteur est en **vue partielle**, ne peut pas conclure → **repli défensif : pas d'emergency**. Logue `kind=emergency_aborted_network` dans `deployment_events` pour traçabilité.

**Condition 1 — état système cassé (vue cross-nœud + vote majoritaire)**

Chaque backend expose un nouvel endpoint `GET /api/system/peer-health` qui retourne **sa vue à lui** des 2 autres :
```json
{
  "self": "node3",
  "peers": {
    "node1": {"reachable": true, "health": "200", "lag_ms": 12, "last_check": "2026-05-10T03:14:00Z"},
    "node2": {"reachable": false, "health": "timeout", "lag_ms": null, "last_check": "2026-05-10T03:14:00Z"}
  },
  "etcd_quorum_seen": true,
  "uptime_s": 84233,
  "container_restart_count": 0
}
```

Le détecteur lit les 3 vues (la sienne + les 2 autres via curl WG) puis applique une logique majoritaire — **un nœud est considéré cassé seulement si ≥ 2/3 vues le voient cassé** :

- *1a — backends 503* : un backend est "en 503" si **≥ 2/3** vues (incluant la sienne propre quand applicable) le voient ainsi sur **≥ 8/10** des derniers checks (30 s période). Trigger : ≥ 2/3 backends sont en 503 par majorité depuis ≥ **5 min**.
  - **Distinction nette** : le détecteur compte uniquement les **503 explicites avec body** (le backend a répondu mais s'auto-déclare malade). Les `connection refused`, `timeout`, `connection reset` sont **exclus** — ce sont des symptômes typiquement réseau, pas logiciels.

- *1b — quorum etcd perdu (durci)* : trigger uniquement si **etcd lui-même répond** mais reporte un état dégradé. Concrètement :
  - `etcdctl member list` répond sur **≥ 2/3** endpoints (= les processus etcd sont vivants)
  - ET `etcdctl endpoint status` reporte `LEADER=null` ou un raft term qui change > 3 fois en 60 s
  - Si etcd ne répond pas du tout (timeout, refused) → c'est probablement réseau ou container down → **inopérant**, on n'utilise pas ce critère
  - Logique : on veut le signal "etcd est conscient et nous dit qu'il est en panne", pas "je n'ai pas de nouvelles d'etcd"

- *1c — heartbeat (durci)* : le détecteur fait `SELECT MAX(last_heartbeat) FROM users WHERE escalation_position > 0` **directement sur le primary Postgres** (pas un replica). Trigger uniquement si :
  - La requête réussit (= primary est joignable et écrit)
  - ET le résultat est < `now() - 5 min`
  - Si la requête échoue (timeout, primary injoignable) → **inopérant**, c'est probablement 1b ou réseau
  - Logique : on veut le signal "le primary tourne mais aucune app ne ping", pas "le primary est inaccessible"

- *1d — restart-loop* : ≥ 2/3 backends rapportent `container_restart_count` croissant (≥ 3 en 5 min) **dans leur propre `peer-health`** (= ils ont relevé eux-mêmes l'incrément, pas une vue externe). Trigger reste : **≥ 2/3** par majorité.

Cette refonte garantit que :
- Une partition NODE3 ↔ site (NAT down) ne déclenche pas d'emergency : NODE3 voit onsite-1/2 timeout, mais 1a exclut les timeouts → cond 1 inopérante.
- Une partition entre les 2 onsite (RAR vu le même switch) ne déclenche pas non plus : majorité 2/3 nécessaire.
- Une vraie panne logicielle (image bug) sera vue par les 3 nœuds simultanément → vote unanime → trigger.

**Conditions 2 à 6** (inchangées par rapport à V1 du design) :

2. Une PR du bot (auteur = `alarm-murgat-bot[bot]`) a été **mergée auto** sur master il y a < **1 h**
3. CI complète verte sur ce sha : tier 1 + 1.5 (mutation 100 %) + 2 + 3
4. Le diff de la PR ne touche que la zone autorisée (denylist + restriction `backend/app/**` Python pur, cf §7 mode urgence)
5. `system_config.ai_emergency_promote_enabled = true` (kill switch). Détails :
   - **Default à l'installation : `false`** (opt-in explicite, pas opt-out)
   - **Modifiable uniquement par un admin humain** via la web UI `/admin/config` (session `is_admin=True`, INV-077) ou `psql` en local. **Le bot ne peut pas se l'auto-attribuer** (denylist + protection admin-only).
   - Lu par le détecteur à chaque tour (60 s) → désactivation prend effet en ≤ 1 min sans redémarrage
   - Chaque toggle loggué dans `audit_events` (INV-100) avec auteur + timestamp
   - Cas de panne primary Postgres : le détecteur ne peut pas lire la clé → repli défensif "considérer false". Pas d'emergency tant que la DB elle-même est en rade (ce qui est le bon comportement : on ne déploie pas un hotfix bot sur un cluster où on n'arrive même pas à lire son propre kill switch)
6. Aucun emergency déjà déclenché dans les **24 h** précédentes (anti-boucle dure)

**Workflow** :

L'orchestrateur sur NODE3 (qui fait déjà le canary §4) tourne aussi un détecteur toutes les 60 s. Si les 6 conditions sont vraies :

1. INSERT `deployment_events (kind='emergency_promote', actor='auto-emergency')` avec contexte
2. Re-tag GHCR : `:stable` ← `:sha-<bot>` (idem la promo manuelle, mais déclenchée par le détecteur)
3. Email **immédiat** à `direction_technique@charlesmurgat.com` (sujet : `[URGENCE] auto-promote bot — service KO`)
4. Trigger pull sur les 3 nœuds (sans attendre les 5 min du timer normal)
5. Canary séquentiel **express** : soak réduit à **3 min** par nœud (au lieu de 10) — on accepte un soak plus court parce que le statu quo est pire
6. Auto-rollback **toujours actif** : si le hotfix lui-même casse encore, on revient à l'image précédente, et on log `kind=abort`. Pas de 2e tentative auto

**Garde-fous** :

- L'humain peut désactiver à tout moment : `UPDATE system_config SET value='false' WHERE key='ai_emergency_promote_enabled'` (clé Postgres, propagée immédiatement par les 3 backends).
- Si le détecteur lui-même est sur un nœud cassé (NODE3 KO), il ne tourne pas → mode urgence ne se déclenche pas → repli sur l'humain. Acceptable (NODE3 cloud OVH a moins de causes de panne corrélées avec onsite).
- Limite **dure 1/24 h** : si le hotfix urgent du bot casse à son tour, le système ne re-tente pas en boucle. Email + fallback opérateur (cf ci-dessous).
- Mode urgence ne s'autorise **pas** à modifier l'image `patroni` (uniquement `backend`). Une régression Patroni est trop risquée pour Postgres et la replication.

**Filet de sécurité retenu** :

La confiance accordée au déploiement automatique repose **uniquement** sur la suite de tests, qui agit comme garantie de non-régression. Concrètement :

- **Tier 1.5 mutation testing 100 % bloquant** sur `backend/app/logic/` (déjà en place dans `pr.yml`) — chaque ligne de logique métier doit être tuée par au moins un test unitaire. C'est la défense la plus solide contre les bugs subtils non détectés par /health.
- **Tier 3 E2E** contre vrai cluster Patroni (déjà en place) — couvre les contrats API et les invariants business.
- **Tier 4 nightly chaos** (failback E2E) — exigé vert sur ce sha avant emergency.
- **Auto-rollback toujours actif** sur le canary (§5) — si le hotfix lui-même produit un bug observable (/health 503, restart-loop, lag), il est annulé automatiquement.

Si la suite de tests ne détecte pas un bug logique, le mode urgence ne le détectera pas non plus. C'est une **limite assumée** : on accepte que la qualité du déploiement automatique soit plafonnée par la qualité de la suite. Tier 1.5 mutation 100 % rend cette limite plus haute que la moyenne.

**Fallback opérateur (dernière ligne)** :

Si malgré le mode urgence le service reste cassé (rollback du hotfix s'est déclenché, ou Cond 1 reste vraie après expiration du cooldown 24 h), l'opérateur de garde — dont l'app Android sonne déjà localement via INV-ANDROID-302 — peut joindre l'admin par téléphone hors-bande (numéro perso non lié au système).

C'est intentionnellement **du bas-tech** :
- Pas d'astreinte humaine de première ligne (le mode urgence absorbe le coup)
- Mais un canal humain garanti existe pour le 1 % des cas où l'auto ne suffit pas
- L'opérateur sait qu'il y a panne (sa propre app sonne), il sait qui appeler (numéro pré-établi)
- Latence : 5-15 min selon disponibilité admin

**Ce que le mode urgence n'attrape PAS** (et bascule sur le fallback opérateur) :

- Une panne hardware totale (3/3 nœuds physiquement down). L'orchestrateur ne tourne nulle part. Opérateur appelle.
- Un bug bloquant que le bot **n'a pas su fix** (PR pas ouverte, ou ouverte mais CI rouge). Le mode urgence ne génère pas de fix, il ne fait que les déployer plus vite. Opérateur appelle.
- Un bug subtil non détecté par les indicateurs (ex : alarmes qui se créent mais ne s'escaladent pas — `/health` reste vert). Opérateur peut appeler s'il constate une absence d'alarme attendue.
- Un hotfix bot qui passe la CI mais ne fix pas le vrai bug (filet de sécu insuffisant). Cond 1 reste vraie après le déploiement → cooldown 24 h → opérateur appelle.

**Risques acceptés (transparence)** :

Cette décision a été prise après revue d'avocat du diable (sessions du 2026-05-10). Sont **explicitement acceptés** :

1. *Bot non éprouvé en prod nocturne* : la 1re vraie nuit où le mode urgence se déclenche est aussi le 1er test grandeur nature. Mitigation : restriction `backend/app/**` Python pur (zone safe), tier 1.5 mutation 100 % bloquant, auto-rollback actif.
2. *Pas de validation possible avant incident* : on ne peut pas simuler une vraie panne nocturne avec PR bot prête au bon moment. On verra la 1re fois.
3. *Hotfix qui passe la CI mais ne fix pas le vrai bug* : filet de sécu = qualité de la suite de tests. Pas la peine de dupliquer ailleurs. Si la suite est défaillante, le mode urgence l'est aussi — c'est cohérent, pas un risque ajouté.
4. *Effet de cliquet vers plus d'autonomie bot* : la décision actuelle reste révisable. `ai_emergency_promote_enabled=false` est l'override universel. Audit annuel recommandé.

**Métriques pour « PR propre consécutive »** :

Stockées dans `system_config` (clés Postgres, alignement avec INV-084) :
- `ai_clean_streak` (int) — nombre de PRs propres consécutives
- `ai_clean_streak_last_pr` (int) — # de la dernière PR comptée
- `ai_auto_merge_enabled` (bool) — feature flag global

**Une PR est « propre » ssi tous ces critères sont remplis** :
1. Auteur = `alarm-murgat-bot[bot]`
2. Tier 1 + 1.5 + 2 + 3 verts
3. Mergée (ou auto-mergée Phase 4) sans manual edit du commit (vérifiable : `git log --pretty=full master | head -1` doit montrer un seul author + committer = bot)
4. **Pas de revert** dans les **7 jours** suivant le merge
5. **Pas de hotfix** sur les fichiers touchés par cette PR dans les 7 jours suivants
6. Pas de déclenchement d'un auto-rollback sur les 7 jours suivants impliquant un sha qui inclut cette PR

Un job nightly `.github/workflows/ai-streak-evaluator.yml` :
- Évalue les PRs bot mergées il y a exactement 7 jours
- Met à jour `ai_clean_streak` (incrémente si OK, reset à 0 sinon)
- Si `ai_clean_streak >= 10` ET pas de bug remonté en prod → autorise auto-merge sans `ai:approved`

**Garde-fou** :
- Limite de **blast radius dure** : auto-merge ne marche QUE si le diff touche uniquement la zone autorisée (cf denylist actuel). Et **uniquement** si le diff fait < 100 lignes (anti-PR géante non revue).
- L'humain peut **désactiver** l'auto-merge à tout moment via `system_config.ai_auto_merge_enabled = false`.
- Toute auto-merge déclenchée logue un event `kind=ai_auto_merge` dans une table dédiée (ou `audit_events` existante).

### Alternatives considérées

- **Bot promeut `:stable`** : techniquement faisable, refusé politiquement V1 (alarme critique zero-bug → humain dans la boucle pour le pas dans la prod).
- **`ai_clean_streak` calculé en JSON dans `.github/`** : versionné = audit-friendly, mais pas live (besoin de PR pour bump). Pire UX pour l'humain qui veut désactiver. **Rejeté** (Postgres > fichier).
- **10 PRs propres = trop strict** : seuil arbitraire (tiré de AI_STRATEGY.md). À recalibrer après 30 PRs bot total ; si le taux propre est ~ 100 %, on peut baisser le seuil ; si ~ 50 %, le monter.

### Questions ouvertes

- **Q7.1** Définition exacte de « hotfix » dans le critère 5 : tout commit qui touche ≥ 1 fichier en commun avec la PR évaluée ? Ou seulement les commits avec `fix(` au début du message ? **À trancher**.
- **Q7.2** Si une PR bot fait 80 lignes mais touche 4 fichiers et un seul est dans la denylist (rare car la denylist devrait fail tôt), la PR est-elle « propre » ? Réponse défaut : non, fail le streak — mais à confirmer.
- **Q7.3** Doit-on ajouter un **gate humain post-Phase 4** pour la promo `:stable` du **premier** sha qui contient une PR auto-mergée bot ? Idée : labellisation `ai-touched` sur le sha, qui force l'humain à confirmer explicitement à la promo. **Forte recommandation** comme garde-fou supplémentaire.
- **Q7.4** Que se passe-t-il si l'humain pose le label `ai:approved` sur une PR bot **alors que** `ai_auto_merge_enabled=false` ? Le label `ai:approved` doit l'emporter (workflow Phase 4 actuel). À documenter explicitement.

---

## 8. Plan d'implémentation suggéré (séquence de PRs)

> Ordre proposé. Chaque PR est petite, indépendante, mergeable séparément, testable seule.

| PR | Titre | Fichiers touchés | Bloquant pour |
|---|---|---|---|
| 1 | `ci(cd): workflow build GHCR sur push master` | `.github/workflows/build.yml` (nouveau), `.dockerignore` (peut-être ajustement) | PR 2 |
| 2 | `infra(cd): variabilisation docker-compose pour image:tag` | `docker-compose.yml` (`build:` → `image: ${REGISTRY}/...:${IMAGE_TAG:-stable}`), `.env.prod.node{1,2,3}` (ajout `REGISTRY`/`IMAGE_TAG`) | PR 3 |
| 3 | `infra(cd): systemd timer pull avec lock` | `infra/onsite/systemd/alarm-cd-pull.service`, `…timer`, `scripts/alarm-cd-pull.sh` | PR 4 |
| 4 | `feat(cd): table deployment_events + endpoint admin` | `backend/app/models.py`, `backend/app/api/deployments.py` (nouveau), `backend/app/templates/deployments.html` (ou onglet dans `index.html`), migration Alembic | PR 5 |
| 5 | `feat(cd): orchestrateur canary séquentiel + soak time` | `scripts/alarm-cd-orchestrate.sh` (sur NODE3), config SSH par WG | PR 6 |
| 6 | `feat(cd): auto-rollback sur health/restart-loop/lag` | enrichissement de `alarm-cd-orchestrate.sh`, hooks healthcheck | PR 7 |
| 7 | `ci(cd): workflow promote-stable.yml manuel` | `.github/workflows/promote-stable.yml` | (V1.5) |
| 8 | `feat(cd): notification email rollback + promo` | `backend/app/notifications/cd_email.py`, branchement orchestrateur | (V1.5) |

**V1.5** (à activer après 5 déploiements clean) :
- 9 — ✅ `feat(cd): promotion canary auto après soak vert` — **fait 2026-05-22**.
  - `scripts/alarm-cd-orchestrate.sh` chaîne `alarm-cd-canary.sh` pour les 3 nœuds dans l'ordre §4 (NODE3 → onsite-2 → onsite-1), halt au 1er fail.
  - Idempotence : state file `/var/lib/alarm/cd/orchestrate.state` mémorise `<digest>\t<outcome>\t<ts>` ; no-op si même digest déjà déployé avec succès, cooldown 1 h après fail.
  - Préflight SSH 3 nœuds + lock `/run/alarm-cd-orchestrate/lock`.
  - Unit systemd `alarm-cd-orchestrate.{service,timer}` sur **NODE3 uniquement**, timer **disable par défaut** (opt-in opérateur après 5 cycles canary manuels validés).
  - Patch couplé sur `alarm-cd-canary.sh` : le check d'early-exit ne se limite plus à `cache vs registry` (qui devient toujours vrai après le pull-timer V1.5) — il vérifie aussi `running container vs cached :stable`, sinon la chaîne auto serait un no-op silencieux laissant la prod sur l'ancienne image.
- 10 — `feat(ai-bot): ai_clean_streak evaluator + auto-merge sans label après ≥ 10 propres`

**V2** (séparé) :
- Smoke tests automatiques post-promo
- Staging dédié (NODE3 ?)
- Grafana sur NODE3
- Multi-arch arm64
- Cosign signing
- Push FCM `deployment_status` aux apps

---

## 9. Questions ouvertes consolidées

> Tous les `Q*.*` à trancher avant ou pendant l'implémentation. **Décision par défaut entre parenthèses** quand applicable. Listées ici pour relecture facile.

### Build (§1)
- **Q1.1** GHCR public ou privé ? *(public, cohérent avec repo public)*
- **Q1.2** Retention `:sha-<short>` GHCR ? *(50 derniers + tous les ex-`:stable`)*
- **Q1.3** Reproductibilité `Dockerfile.patroni` (apt-pinning) ? *(à auditer)*
- **Q1.4** Cosign signing dès V1 ou V2 ? *(V2 sauf si l'utilisateur est sensible)*

### Promotion (§2)
- **Q2.1** Cooldown 24 h, ajustable ? *(oui, input `bypass_cooldown`)*
- **Q2.2** **Environnement de staging dédié — décision majeure non tranchée** *(forte reco : décider avant Phase 3)*
- **Q2.3** Smoke tests post-promo ? *(différé V2)*

### Pull (§3)
- **Q3.1** Pull `:sha-<short>` aussi pour rollback rapide ? *(oui, les 5 derniers ex-`:stable`)*
- **Q3.2** Délai max sans pull réussi avant alerte ? *(6 h)*
- **Q3.3** Authentification GHCR même si public ? *(oui, anti-rate-limit)*

### Canary (§4)
- **Q4.1** Switchover Patroni préventif avant restart du primary ? *(oui)*
- **Q4.2** Lag < 100 ms atteignable depuis NODE3 cloud ? *(à mesurer ; relâcher à 1 s si non)*
- **Q4.3** Switchover préventif onsite-2 si primary ? *(oui par symétrie)*
- **Q4.4** Différencier soak backend-only vs patroni ? *(soak +90 s pour patroni)*

### Rollback (§5)
- **Q5.1** Distinguer faux positif post-bascule vs vrai rollback ? *(observation 60 s post-rollback)*
- **Q5.2** Rollback du rollback (ancienne image cassée) — runbook ? *(oui, à rédiger avant prod)*
- **Q5.3** Inclure télémétrie Android dans triggers ? *(différé V2)*

### Observabilité (§6)
- **Q6.1** Logger pulls sans changement ? *(non, bruit)*
- **Q6.2** Adresse email confirmée ? *(à valider avec l'utilisateur)*
- **Q6.3** Bandeau « déploiement » dans l'app Android ? *(non V1)*
- **Q6.4** Rétention events ? *(90 j idem audit_events)*

### Bot IA (§7)
- **Q7.1** Définition exacte de « hotfix » ? *(à trancher)*
- **Q7.2** Mix denylist/zone autorisée si fichiers mixtes ? *(non, fail le streak)*
- **Q7.3** Label `ai-touched` sur sha pour gate manuel ? *(forte reco)*
- **Q7.4** Comportement si label `ai:approved` posé alors que `ai_auto_merge_enabled=false` ? *(label l'emporte ; à documenter)*
- **Q7.5** **Définition exacte de "service mesurablement cassé"** pour mode urgence : les seuils proposés (≥ 2/3 backends 503 sur 5 min, etc.) sont des points de départ. À calibrer après le 1er déploiement réel. Faux positifs trop coûteux (auto-promo qui casse pour rien) — préférer un seuil un peu trop strict qu'un peu trop laxe.
- **Q7.6** Mode urgence sur quels modules ? Restreint à `backend` seul (proposition par défaut), ou aussi `patroni` ? Argument pour exclure patroni : régression Postgres = risque INV-094/092 (perte données / replica désynchro), bien plus grave qu'un bug backend. Argument contre : si le bug bloquant est *dans* patroni, on ne peut pas le fix par mode urgence → besoin humain.
- **Q7.7** Soak express 3 min suffisant ? Retient le critère `/health` mais pas la stabilité replication Patroni (pg_basebackup peut prendre 30 s). Si le canary express touche un replica en cours de sync, le rollback peut se déclencher pour faux positif. Proposition : pour patroni, 5 min mini même en mode urgence.
- **Q7.8** **Préfiltre réseau (Cond 0) — quels endpoints exactement ?** Proposition : 1.1.1.1 + 9.9.9.9 + mesh WG 10.99.0.{2,3} + GitHub API. Mais (a) la dépendance à la résolution DNS de `api.github.com` ajoute un mode de défaillance ; (b) si l'utilisateur a un firewall corporate qui bloque ICMP sortant, les pings échouent en permanence → mode urgence jamais armé. À valider sur la conf réelle du site et OVH.
- **Q7.9** **Vote majoritaire 2/3 — comportement quand un nœud est physiquement coupé ?** Si onsite-2 est éteint (panne hardware), il ne répond pas à `/api/system/peer-health` → vue manquante. Proposition : un nœud injoignable compte comme "vue indisponible", pas comme "vue=cassé". Donc le vote tourne sur 2/3 vues disponibles, et il faut **2/2** d'accord. Devient plus strict, pas moins. À confirmer.
- **Q7.10** **Coût en perf de `/api/system/peer-health` qui ping en continu** : chaque backend doit pinger ses 2 voisins toutes les 30 s. Avec 3 nœuds × 2 voisins = 6 requêtes/30 s = 12 /min en interne. Négligeable, mais à mesurer si le mesh WG sature pour autre chose. Risque de feedback loop si peer-health lui-même devient lent et déclenche les triggers.

### Transversales
- **Q-X1** Déclenchement initial du cluster Patroni : la 1re fois que `start-prod-node.sh` tourne sur les 3 nœuds, **aucun** n'a d'image `:stable` locale. Le timer de pull doit donc avoir tourné au moins une fois avant (ou alors `start-prod-node.sh` accepte un fallback `IMAGE_TAG=sha-<short>`). **À documenter dans le bring-up séquentiel**.
- **Q-X2** Coexistence avec le bring-up actuel `docker compose up --build -d` (qui rebuild localement) : on garde le `--build` comme mode dev et on isole le mode prod via une env var `LOCAL_BUILD=true`, ou on supprime carrément le build local en prod ? Proposition : un nouveau script `scripts/start-prod-node.sh --pull` qui pull plutôt que build, sans toucher au comportement existant tant que les images ne sont pas dans GHCR.
- **Q-X3** Comment gérer une PR qui modifie **simultanément** le code backend ET le `docker-compose.yml` (ex: ajout d'un service) ? Le canary doit refléter ce double changement. Proposition : `docker-compose.yml` est désormais versionné via le clone `/opt/alarm` (déjà le cas), un `git pull` est fait dans `start-prod-node.sh` avant le `up -d`. Le pull-image timer ne suffit plus seul ; il faut aussi un pull-git timer (ou les coupler).

---

## 9bis. Activation V1.5 (runbook opérateur)

> Procédure à appliquer **uniquement après 5 cycles canary manuels validés** (cf §4 — "à activer après 5 déploiements clean"). Sans validation préalable, garder le timer désactivé : la chaîne canary reste pilotée à la main via `./scripts/alarm-cd-canary.sh --node N` un nœud après l'autre.

**Pré-requis (sur NODE3 uniquement)** :
1. `alarm-cd-pull.timer` actif sur les 3 nœuds (sinon l'image n'est jamais en cache pour le canary).
2. Clé SSH `alarm@NODE3` → `alarm@10.99.0.{1,2,3}` fonctionnelle sans password.
   **État constaté 2026-05-22** (vérif live SSH) : aucun de ces 3 paths ne marche aujourd'hui. À mettre en place avant activation :
   - **SSH-from-NODE3 vers onsite (10.99.0.2 + 10.99.0.3) timeout** : UFW sur onsite n'autorise SSH que depuis `172.16.0.0/16` (LAN site). Ajouter sur chaque onsite : `sudo ufw allow from 10.99.0.0/24 to any port 22 proto tcp comment 'SSH mesh WG (CD orchestrator)'`.
   - **SSH-from-NODE3 vers NODE3 lui-même (10.99.0.1) refused** : sshd sur NODE3 écoute sur `Port 50922` (adaptation cloud OVH cf provisioning §22ter), pas 22. Configurer `~/.ssh/config` du user `alarm` sur NODE3 : `Host 10.99.0.1\n  Port 50922`.
   - **Pas de clé SSH outbound sur NODE3** : `/home/alarm/.ssh/` ne contient que `authorized_keys`. Générer une paire dédiée : `sudo -u alarm ssh-keygen -t ed25519 -N "" -C "alarm-cd-orchestrator" -f /home/alarm/.ssh/id_ed25519`, puis déposer la pubkey dans `authorized_keys` de `alarm@onsite-1` et `alarm@onsite-2` (et sur NODE3 lui-même pour le self-loop du canary `--node 3`).
3. Fichier `/etc/alarm/cd.env` avec `GATEWAY_KEY=<même valeur que `.env.prod.secrets`>` ; mode `640 root:alarm`.

**Activation** :
```bash
# === Etape A — Prereqs SSH (depuis ~/poste admin~ et NODE3) ===
# A.1. UFW sur onsite-1 ET onsite-2 (le LAN UFW bloque par defaut le SSH depuis WG)
ssh -i ~/.ssh/alarm_onsite_1 alarm@172.16.1.121 \
  "sudo ufw allow from 10.99.0.0/24 to any port 22 proto tcp comment 'SSH mesh WG (CD orchestrator)'"
ssh -i ~/.ssh/alarm_onsite_2 alarm@172.16.1.120 \
  "sudo ufw allow from 10.99.0.0/24 to any port 22 proto tcp comment 'SSH mesh WG (CD orchestrator)'"

# A.2. Keypair outbound sur NODE3 (alarm) + ssh_config pour le self-loop sur 50922
ssh -p 50922 -i ~/.ssh/alarm_node3 alarm@51.210.105.102 '
  ssh-keygen -t ed25519 -N "" -C "alarm-cd-orchestrator" -f ~/.ssh/id_ed25519
  cat >> ~/.ssh/config <<EOF
Host 10.99.0.1
  Port 50922
EOF
  chmod 600 ~/.ssh/config
'
# Recuperer la pubkey :
ALARM_CD_PUB=$(ssh -p 50922 -i ~/.ssh/alarm_node3 alarm@51.210.105.102 'cat ~/.ssh/id_ed25519.pub')
# Distribuer aux 3 noeuds (incl. NODE3 lui-meme) :
ssh -i ~/.ssh/alarm_onsite_1 alarm@172.16.1.121 "echo '$ALARM_CD_PUB' >> ~/.ssh/authorized_keys && sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys"
ssh -i ~/.ssh/alarm_onsite_2 alarm@172.16.1.120 "echo '$ALARM_CD_PUB' >> ~/.ssh/authorized_keys && sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys"
ssh -p 50922 -i ~/.ssh/alarm_node3 alarm@51.210.105.102 "echo '$ALARM_CD_PUB' >> ~/.ssh/authorized_keys && sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys"
# Verif :
ssh -p 50922 -i ~/.ssh/alarm_node3 alarm@51.210.105.102 '
  for ip in 10.99.0.1 10.99.0.2 10.99.0.3; do
    ssh -o BatchMode=yes -o ConnectTimeout=5 alarm@$ip "echo OK on $(hostname)"
  done
'  # Doit afficher 3 lignes "OK on ..."

# === Etape B — Install scripts + units + sudoers + cd.env (sur NODE3 uniquement) ===
ssh -p 50922 -i ~/.ssh/alarm_node3 alarm@51.210.105.102 '
  cd /opt/alarm && git pull
  sudo install -m 755 scripts/alarm-cd-orchestrate.sh /usr/local/bin/alarm-cd-orchestrate
  sudo install -m 755 scripts/alarm-cd-canary.sh      /usr/local/bin/alarm-cd-canary.sh
  sudo install -m 755 scripts/alarm-cd-rollback.sh    /usr/local/bin/alarm-cd-rollback.sh
  sudo install -m 644 infra/onsite/systemd/alarm-cd-orchestrate.service /etc/systemd/system/
  sudo install -m 644 infra/onsite/systemd/alarm-cd-orchestrate.timer   /etc/systemd/system/
  sudo install -m 440 infra/onsite/sudoers/alarm-cd                     /etc/sudoers.d/alarm-cd
  sudo visudo -c                              # doit afficher "parsed OK"
  sudo install -d -m 750 -o root -g alarm /etc/alarm
  GATEWAY_KEY=$(grep "^GATEWAY_KEY=" /opt/alarm/.env.prod.secrets | cut -d= -f2)
  echo "GATEWAY_KEY=$GATEWAY_KEY" | sudo tee /etc/alarm/cd.env >/dev/null
  sudo chmod 640 /etc/alarm/cd.env && sudo chown root:alarm /etc/alarm/cd.env
  sudo systemctl daemon-reload
'

# === Etape C — Smoke (forcer un cycle pour valider) ===
ssh -p 50922 -i ~/.ssh/alarm_node3 alarm@51.210.105.102 '
  # Le service va detecter que :stable cache == running container sur les 3
  # noeuds -> chaine canary no-op (silent exit 0). Bonne preuve de bon
  # cablage SSH + cd.env + scripts sans rien casser.
  sudo systemctl start alarm-cd-orchestrate.service
  sudo journalctl -u alarm-cd-orchestrate -n 30 --no-pager
'

# === Etape D — Activation du timer (apres validation de l etape C) ===
ssh -p 50922 -i ~/.ssh/alarm_node3 alarm@51.210.105.102 '
  sudo systemctl enable --now alarm-cd-orchestrate.timer
  sudo systemctl is-enabled alarm-cd-orchestrate.timer   # enabled
  sudo systemctl list-timers alarm-cd-orchestrate.timer  # next fire dans < 15 min
'
```

**Désactivation (revert si V1.5 cause des problèmes)** :
```bash
# Sur NODE3 :
sudo systemctl disable --now alarm-cd-orchestrate.timer
sudo systemctl stop alarm-cd-orchestrate.service
# La chaine canary devient a nouveau manuelle (V1) — alarm-cd-pull.timer
# continue de mettre en cache, mais le restart est pilote a la main.
```

**Diagnostic** :
```bash
# Voir l'etat dernier deploiement orchestre :
cat /var/lib/alarm/cd/orchestrate.state   # <digest>\t<outcome>\t<unix_ts>
# Voir les events CD cross-noeud :
curl -fsS http://10.99.0.1:8000/api/deployments/state -H "Authorization: Bearer <admin_token>"
# Forcer un cycle (apres mise a jour du :stable sans attendre le tick timer) :
sudo systemctl start alarm-cd-orchestrate.service
```

---

## 10. Métriques de réussite Phase 3

Pour considérer Phase 3 « livrée », tous ces points doivent être verts :

1. ✅ Promotion `:stable` manuelle exécutable en < 30 s à partir d'un sha donné
2. ✅ Pull-timer détecte un nouveau `:stable` en ≤ 5 min sur les 3 nœuds
3. ✅ Canary séquentiel complet (3 nœuds) en < 1 h pour un déploiement standard backend-only
4. ✅ 5 déploiements consécutifs sans rollback (= confiance baseline)
5. ✅ 1 rollback déclenché volontairement (test de bout en bout) avec retour à l'état précédent en < 5 min, email reçu, event loggué
6. ✅ Aucun déclenchement de INV-085 (perte quorum cluster) pendant les 5 déploiements
7. ✅ Aucun déclenchement de INV-ANDROID-302 (heartbeat lost > 2 min côté apps) pendant les 5 déploiements
8. ✅ Dashboard `/admin/deployments` affiche l'historique correctement
9. ✅ Procédure de rollback documentée (runbook, où aller chercher quoi)
10. ✅ Question Q2.2 (staging) tranchée et appliquée
11. ✅ Mode urgence (§7) testé en simulation : 1 fix bot + service mesurablement cassé → auto-promo + canary express + retour à la normale en < 15 min, **OU** preuve que le rollback du hotfix s'est bien déclenché si le hotfix lui-même est cassé

À l'inverse, Phase 3 est explicitement **incomplète** sans :
- Auto-promo canary mode normal (V1.5)
- Auto-merge bot après 10 propres (V1.5)
- Staging dédié (V2)
- Grafana / Cosign / multi-arch (V2)

---

## 11. Annexe — Vue d'ensemble du flux de déploiement nominal

```
[1] Dev pousse sur master
   → workflow build.yml lance
   → publie ghcr.io/.../alarm-{backend,patroni}:sha-abc1234

[2] Tier 1+1.5+2+3 verts sur master
   → tier 4 nightly tourne dans la nuit, vert

[3] >= 24h plus tard, humain juge OK
   → workflow_dispatch promote-stable.yml avec sha=abc1234
   → vérifie tous les prérequis
   → re-tag ghcr.io/.../...:stable -> sha-abc1234
   → INSERT deployment_events (kind=manual_override, status=success, actor=<github_user>)
   → email "promu :stable"

[4] Sur NODE3 (10.99.0.1), le timer alarm-cd-pull.timer fire à T+0 à T+5min
   → détecte digest distant != local
   → docker pull
   → INSERT deployment_events (kind=pull, status=success, node=node3)

   (idem onsite-2, onsite-1 — les 3 nœuds ont la nouvelle image LOCALE en cache, sans encore l'utiliser)

[5] Orchestrateur (sur NODE3) déclenche canary étape 1
   → switchover Patroni si NODE3 est primary
   → start-prod-node.sh 3 (avec IMAGE_TAG=stable)
   → INSERT deployment_events (kind=canary_start, node=node3)
   → soak 10 min, monitoring critères §4

   Cas A — soak vert :
      → INSERT deployment_events (kind=canary_promoted, node=node3)
      → en V1 : attend confirmation humaine pour l'étape 2
      → en V1.5 : bascule auto vers étape 2

   Cas B — auto-rollback déclenché :
      → tag local :stable revient à digest précédent
      → start-prod-node.sh 3 redémarre avec ancienne image
      → INSERT deployment_events (kind=rollback, node=node3, details_json={trigger: "...", logs: [...]})
      → email
      → halt canary, ne pas updater onsite-2/1

[6] Étape 2 — onsite-2 (idem, avec switchover Patroni si primary)
[7] Étape 3 — onsite-1
[8] INSERT deployment_events (kind=canary_promoted, node=node1, status=success)
   → email "déploiement complet :stable=sha-abc1234"
```

### Annexe bis — flux mode urgence (nuit, bot fix bloquant)

```
[T+0]  03h12 — un bug bloquant se manifeste en prod (ex: /health 503 sur 2/3 backends)
        → opérateurs astreinte commencent à recevoir sonnerie locale (INV-ANDROID-302)
        → email INV-085 part vers direction technique

[T+10] 03h22 — bot IA reçoit l'issue (issue auto-créée par alerting OU report manuel)
        OU bot ré-applique son cycle sur une PR existante d'une issue précédente
        → écrit test RED, fix GREEN, ouvre PR
        → CI tourne (~8 min)

[T+18] 03h30 — CI verte, auto-merge Phase 4 si bot a 10 PRs propres déjà
        → workflow build.yml push :sha-fix01 sur GHCR
        → :stable reste sur l'ancien sha (cassé)

[T+20] 03h32 — détecteur emergency sur NODE3 cloud, tour 60 s
        → check les 6 conditions (cf §7 mode urgence)
        → toutes vraies : service KO depuis 20 min, PR bot fraîche, CI verte, etc.
        → INSERT deployment_events (kind='emergency_promote')
        → re-tag :stable -> sha-fix01
        → email URGENCE direction technique

[T+21] 03h33 — pull immédiat sur les 3 nœuds (skip timer normal)
        → canary express : NODE3 (toujours up, sinon pas de détecteur)
        → soak 3 min uniquement (vs 10 min normal)

[T+24] 03h36 — NODE3 vert, propage vers onsite-2
        → soak 3 min

[T+27] 03h39 — onsite-2 vert, propage vers onsite-1
        → soak 3 min

[T+30] 03h42 — déploiement complet :stable=sha-fix01
        → INSERT deployment_events (kind=canary_promoted, status=success, actor=auto-emergency)
        → email "URGENCE — service rétabli automatiquement"

L'humain au matin :
        → voit l'email URGENCE
        → vérifie deployment_events + logs CI
        → décide : OK on garde, OU revert manuel si le fix bot a un effet de bord pas urgent
```

Dans ce flux, on a remplacé 8 h de service KO + nuit blanche pour l'humain par 30 min de service KO + email à lire au réveil. Trade-off acceptable pour un système alarme critique, **à condition** que les conditions de déclenchement (§7) soient strictes et que le mode urgence reste opt-in (kill switch dans `system_config`).

---

## 12. Dernière mise à jour

- **2026-05-09** — création du document, design initial.
- **2026-05-10** — ajout du **mode urgence** §7 (auto-promo bot conditionnelle si service mesurablement cassé). Issu d'une question utilisateur sur le cas « panne nocturne, bot fix, personne pour valider ». Design : balance bénéfice/risque qui s'inverse quand le statu quo est déjà KO. Voir §11 Annexe bis pour le flux. Q7.5/7.6/7.7 ajoutées.
- **2026-05-10 (suite)** — durcissement mode urgence pour exclure les pannes réseau : ajout **Cond 0 préfiltre réseau** (Internet + mesh WG + GitHub joignables avant tout jugement applicatif), refonte **Cond 1** en **vue cross-nœud avec vote majoritaire 2/3** via nouvel endpoint `/api/system/peer-health`. Distinction explicite 503-explicite vs timeout/refused (réseau-like). Triggers 1b/1c durcis : ne déclenchent que si etcd/Postgres répondent et reportent eux-mêmes l'état dégradé, jamais sur leur silence. Q7.8/7.9/7.10 ajoutées.
- **2026-05-10 (final)** — décision tranchée après 2 rounds d'avocat du diable (mode urgence puis stratégie self-healing alternative). Self-healing **rejeté** (action sans diagnostic, code non-validé, pas de baseline, ROI faible vs SMS-réveil). Mode urgence **retenu** avec : (a) filet de sécurité explicite = suite de tests (tier 1.5 mutation 100 % bloquant), (b) fallback opérateur hors-bande = appel téléphonique vers admin, (c) section "Risques acceptés" formalisant les 4 risques pris en connaissance de cause. La complexité de Cond 0 + vote majoritaire est conservée parce qu'elle protège contre un faux positif net (pannes réseau), distinct des risques liés au bot lui-même.
- **2026-05-22** — **CD V1.5 livré (PR 9 §8)** : `scripts/alarm-cd-orchestrate.sh` + units systemd `alarm-cd-orchestrate.{service,timer}` chaînent automatiquement `alarm-cd-canary.sh` pour les 3 nœuds dans l'ordre §4. Idempotence par state file + cooldown 1 h post-fail. Patch couplé sur `alarm-cd-canary.sh` (check du container en cours d'exécution, pas seulement du cache vs registry — sinon no-op silencieux après pull-timer). Timer `alarm-cd-orchestrate.timer` livré **disable**, à activer explicitement par l'opérateur après 5 cycles canary manuels validés (`sudo systemctl enable --now alarm-cd-orchestrate.timer` sur NODE3). Sudoers `infra/onsite/sudoers/alarm-cd` étendu en miroir (`ALARM_CD_INSTALL`, `ALARM_CD_SYSTEMCTL`, `ALARM_CD_JOURNAL`). Smoke test `cd-install-smoke.yml` étend le job au nouvel orchestrateur (RuntimeDirectory/StateDirectory/EnvironmentFile présents, timer disable par défaut, pas de Permission denied, pas de duplication logs).
- **2026-05-22 (vérif live)** — état prod inspecté par SSH avant merge de PR 9. **Constat** : les 3 nœuds tournent `:stable` sha-de1131ac (promu 2026-05-19) depuis 3 jours, `alarm-cd-pull.timer` enabled + active partout, cluster Patroni sain (leader `node1`=onsite-1, replicas sync lag=0). **Pré-requis SSH-from-NODE3 manquants** : (a) `ufw` onsite n'autorise SSH que depuis LAN 172.16.0.0/16 — ajouter règle `from 10.99.0.0/24` ; (b) `/home/alarm/.ssh/` sur NODE3 n'a pas de keypair outbound ; (c) sshd NODE3 écoute `Port 50922` (adaptation cloud), nécessite `~/.ssh/config` pour le self-loop canary `--node 3`. Ces 3 prérequis ajoutés au runbook §9bis. Sans eux, l'orchestrate `Preflight SSH` exit 1 proprement (pas de risque de chaîne incomplète).
