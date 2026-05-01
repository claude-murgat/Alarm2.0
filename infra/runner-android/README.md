# Runner GitHub Actions self-hosted Linux dédié émulateurs Android

Ce dossier provisionne un **5ᵉ runner self-hosted** sur **une VM Linux dédiée**,
distinct des 4 runners Docker `alarm-ci` provisionnés via
`infra/runner/docker-compose.runner.yml`.

## Pourquoi une VM Linux dédiée et pas un conteneur Docker

Les 4 runners actuels sont des conteneurs `myoung34/github-runner` qui tournent
sous Docker Desktop / WSL2 sur le PC Windows. Empiriquement (vérifié le
2026-04-26) : `/dev/kvm` n'est pas exposé dans ces conteneurs (WSL2 sans nested
virtualization), et le pattern DooD complique le routage `10.0.2.2` →
backend.

Une VM Linux dédiée :
- Active la nested virt **une seule fois** au niveau de l'hyperviseur (Proxmox dans notre cas)
- Expose `/dev/kvm` nativement à l'OS de la VM, donc à tous les processus dont l'émulateur
- Fait tourner l'émulateur ET les conteneurs Docker du test sur le **même host**, donc le `10.0.2.2` de l'app pointe sur la VM, où les ports 8000/8001 sont bindés par les `docker compose up` du test → routage trivial, zéro plomberie

**Labels du runner** : `self-hosted, linux, alarm-android`. Distincts de
`alarm-ci` pour que tier 1+2+3 continuent d'aller sur les 4 runners Docker.
Seul `tier4-failback.yml` cible `alarm-android`.

## Setup actuel

Cette installation tourne sur **une VM Proxmox dédiée** :
- Hôte : Proxmox VE
- VM : Debian 12 bookworm, 6 vCPU / 16 Gio RAM / 150 Gio disque
- Nested virt activée côté Proxmox via `cpu: host` (flag `+vmx` exposé)
- Vérification rapide dans la VM : `ls -l /dev/kvm` doit retourner un device char
  appartenant au groupe `kvm`, et `grep -E '(vmx|svm)' /proc/cpuinfo` doit matcher

Les scripts ci-dessous sont écrits pour cette config (Debian 12). Ils tournent
sans modif sur Debian 11/12/13 ; pour Ubuntu il faudra adapter le bloc Docker
de `install-runner.sh` (URL de repo).

## Procédure complète

### Étape 0 — Provisionner la VM (hors scope de ce dossier)

VM Debian 12, user non-root sudoer, sortie internet OK pour pull les
system-images Android et l'image runner.

Spec recommandée :

| Ressource | Min | Recommandé | Raison |
|---|---|---|---|
| vCPU | 4 | 6-8 | 3 émulateurs + cluster docker |
| RAM | 8 Gio | 12-16 Gio | 3 AVDs × 2 Gio + cluster ~2 Gio + OS + marge |
| Disque | 40 Gio | 60-80 Gio | system-images Android (~5 Gio) + AVDs (~10 Gio) + Docker images (~15 Gio) + journaux |

### Étape 1 — Vérifier KVM dans la VM

```bash
ls -l /dev/kvm
# attendu : crw-rw---- ... root kvm
grep -cE '(vmx|svm)' /proc/cpuinfo
# attendu : > 0
```

Si `/dev/kvm` est absent : nested virt pas active au niveau Proxmox.
VM arrêtée, dans `/etc/pve/qemu-server/<VMID>.conf`, vérifier qu'on a bien
`cpu: host` (ou `cpu: x86-64-v2-AES,flags=+vmx`). Démarrer la VM ensuite.

### Étape 2 — Récupérer le token de registration runner

Token valide 1h, single-use. Depuis n'importe où authentifié `gh` claude-murgat
(la VM elle-même fait l'affaire si `gh auth login` y est fait) :

```bash
gh api -X POST repos/claude-murgat/Alarm2.0/actions/runners/registration-token --jq .token
```

### Étape 3 — Bootstrap (install runner + JDK + SDK Android + Docker)

Dans la VM, depuis le repo cloné (ou ce dossier copié) :

```bash
sudo bash install-runner.sh --token <TOKEN_DE_L_ETAPE_2>
```

Ce script idempotent :
- installe `openjdk-17-jdk`, `unzip`, `curl`, `wget`, `socat`, `libpulse0`
- installe Docker CE + docker compose plugin (skip si déjà présent — repo Debian)
- ajoute le user runner (créé si absent : `ghrunner`) au groupe `kvm` et `docker`
- télécharge Android cmdline-tools + accepte les licenses + install
  `platform-tools`, `emulator`, `platforms;android-30`, `build-tools;34.0.0`,
  `system-images;android-30;google_apis;x86_64`
- exporte `ANDROID_HOME` dans `/etc/profile.d/android.sh`
- télécharge actions/runner v2.333.1 dans `/opt/actions-runner-android/`
- enregistre le runner avec les labels `self-hosted,linux,alarm-android`
- l'installe comme service systemd `actions.runner.<repo>.alarm-android-1`
- démarre le service

Durée : ~10-20 min selon réseau (download SDK + system-image, ~2 Gio total).

Vérification : `https://github.com/claude-murgat/Alarm2.0/settings/actions/runners`
doit montrer un nouveau runner `alarm-android-1` avec le statut `Idle`.

### Étape 4 — Créer les 3 AVDs + leur snapshot warm-boot

Toujours dans la VM, **comme le user runner** :

```bash
sudo -u ghrunner -H bash setup-avds.sh
```

Crée les AVDs `alarm_5552`, `alarm_5554`, `alarm_5556` (Pixel 5, API 30,
google_apis x86_64, 2 Gio RAM), boote chacun une fois headless et écrit le
snapshot warm-boot. Les runs nightly subséquents reprendront en ~30-60s au
lieu de 3-5 min cold-boot.

Durée : ~15-20 min cumulé (3 cold-boots successifs).

### Étape 5 — Vérifier l'install complète

```bash
bash verify.sh
```

Affiche un récap PASS/FAIL : KVM, Java, Android SDK, AVDs, snapshots, service
runner systemd, Docker. Tout en vert → prêt pour le 1er smoke-test workflow.

### Étape 6 — Smoke-test du workflow

Depuis n'importe où authentifié `gh` :

```bash
gh workflow run "Tier 4 - Failback E2E (nightly)" --repo claude-murgat/Alarm2.0
gh run watch --repo claude-murgat/Alarm2.0
```

Le 1ᵉʳ run vérifie bout-en-bout. Si KO : examiner les artifacts
`tier4-failback-reports-<run_id>` (logcat par émulateur, compose logs,
junit XML).

## Maintenance

- **Runner offline** : `sudo systemctl restart actions.runner.*alarm-android*`. Si persistant : ré-exécuter `install-runner.sh` (idempotent, désinstalle proprement avant).
- **Upgrade version runner** : éditer `RUNNER_VERSION` en haut de `install-runner.sh`, ré-exécuter.
- **AVD corrompu** : `avdmanager delete avd -n alarm_<port>` puis `bash setup-avds.sh` recrée.
- **Disk full** : `docker system prune -af` et `rm -rf $HOME/.android/avd/<unused>.avd`.
- **Cleanup runner zombie côté GH UI** : `gh api repos/claude-murgat/Alarm2.0/actions/runners --jq '.runners[] | select(.status=="offline") | .id' | xargs -I {} gh api -X DELETE repos/claude-murgat/Alarm2.0/actions/runners/{}`.

## Voir aussi

- `.github/workflows/tier4-failback.yml` — workflow qui consomme ce runner
- `infra/runner/docker-compose.runner.yml` — les 4 runners Docker existants
  (label `alarm-ci`, indépendants de celui-ci)
- `docs/AI_STRATEGY.md` — stratégie tiers de validation CI
