# Runner GitHub Actions self-hosted Linux dédié émulateurs Android

Ce dossier provisionne un **5ᵉ runner self-hosted** sur **une VM Linux dédiée**,
distinct des 4 runners Docker `alarm-ci` provisionnés via
`infra/runner/docker-compose.runner.yml`.

## Pourquoi une VM Linux dédiée et pas un conteneur Docker

Les 4 runners actuels sont des conteneurs `myoung34/github-runner` qui tournent
sous Docker Desktop / WSL2 sur le PC Windows. Empiriquement (vérifié le
2026-04-26) : `/dev/kvm` n'est pas exposé dans ces conteneurs (WSL2 sans nested
virtualization), et le pattern DooD complique le routage `10.0.2.2` →
backend. Activer nested virt dans WSL2 est faisable mais documenté comme
fragile sur la combinaison Docker Desktop + Win11.

Une VM Linux dédiée :
- Active la nested virt **une seule fois** au niveau de l'hyperviseur de la VM (Hyper-V/VMware/KVM-on-Linux)
- Expose `/dev/kvm` nativement à l'OS de la VM, donc à tous les processus dont l'émulateur
- Fait tourner l'émulateur ET les conteneurs Docker du test sur le **même host**, donc le `10.0.2.2` de l'app pointe sur la VM, où les ports 8000/8001 sont bindés par les `docker compose up` du test → routage trivial, zéro plomberie

**Labels du runner** : `self-hosted, linux, alarm-android`. Distincts de
`alarm-ci` pour que tier 1+2+3 continuent d'aller sur les 4 runners Docker.
Seul `tier4-failback.yml` cible `alarm-android`.

## Spec VM recommandée

| Ressource | Min | Recommandé | Raison |
|---|---|---|---|
| vCPU | 4 | 6-8 | 3 émulateurs + cluster docker |
| RAM | 8 Gio | 12-16 Gio | 3 AVDs × 2 Gio + cluster ~2 Gio + OS + marge |
| Disque | 40 Gio | 60-80 Gio | system-images Android (~5 Gio) + AVDs (~10 Gio) + Docker images (~15 Gio) + journaux |
| OS | Ubuntu 22.04 LTS | Ubuntu 24.04 LTS | Stable, paquets Android dispos, supporté par actions/runner |
| Réseau | NAT/Bridge avec sortie internet | idem | Pull Docker images, GH webhook |

Hyperviseur : libre. Si Hyper-V (sur ce PC), le helper PS1 ci-dessous active
nested virt. Si VMware/VirtualBox/Proxmox/cloud : suivre la doc spécifique.

## Procédure complète

### Étape 0 — Provisionner la VM (hors scope de ce dossier)

À toi de créer une VM Ubuntu Server 22.04 ou 24.04, login user non-root sudoer.
Note l'IP / hostname et le user pour la suite.

### Étape 1 — Activer la nested virtualization (depuis le host Windows si Hyper-V)

VM **arrêtée**, dans une PowerShell **admin** sur le host Windows :

```powershell
.\enable-hyperv-nested-virt.ps1 -VMName "alarm-android-runner"
```

Ce script :
- vérifie que la VM est arrêtée
- active `Set-VMProcessor ... -ExposeVirtualizationExtensions $true`
- active le MAC address spoofing sur l'adaptateur réseau (souvent requis pour
  que les conteneurs Docker DANS la VM joignent le réseau)

Démarre la VM ensuite. Si autre hyperviseur, équivalent dans ses GUI/CLI :
- VMware : VM settings → Processors → cocher "Virtualize Intel VT-x/EPT"
- VirtualBox : `VBoxManage modifyvm "<name>" --nested-hw-virt on`
- Proxmox : ajouter `cpu: host,flags=+vmx` dans le `.conf` de la VM

### Étape 2 — Vérifier KVM dans la VM

SSH dans la VM puis :

```bash
ls -l /dev/kvm
# attendu : crw-rw---- 1 root kvm
```

Si absent : nested virt pas active au niveau hyperviseur. Re-vérifier étape 1.

### Étape 3 — Récupérer le token de registration runner

Token valide 1h, single-use. Depuis n'importe où authentifié `gh` claude-murgat :

```bash
gh api -X POST repos/claude-murgat/Alarm2.0/actions/runners/registration-token --jq .token
# copie la valeur, à passer à install-runner.sh
```

### Étape 4 — Bootstrap (install runner + JDK + SDK Android + Docker)

Dans la VM, après avoir cloné/copié ce dossier (ou téléchargé les 3 .sh) :

```bash
sudo bash install-runner.sh --token <TOKEN_DE_L_ETAPE_3>
```

Ce script idempotent :
- installe `openjdk-17-jdk`, `unzip`, `curl`, `wget`, `socat`, `libpulse0`
- installe Docker CE + docker compose plugin (skip si déjà présent)
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

### Étape 5 — Créer les 3 AVDs + leur snapshot warm-boot

Toujours dans la VM, **comme le user runner** (`sudo -u ghrunner -H bash setup-avds.sh`)
ou en tant que toi-même si tu as ajouté ton user au groupe `kvm`+`android-sdk` :

```bash
bash setup-avds.sh
```

Crée les AVDs `alarm_5552`, `alarm_5554`, `alarm_5556` (Pixel 5, API 30,
google_apis x86_64, 2 Gio RAM), boote chacun une fois headless et écrit le
snapshot warm-boot. Les runs nightly subséquents reprendront en ~30-60s au
lieu de 3-5 min cold-boot.

Durée : ~15-20 min cumulé (3 cold-boots successifs).

### Étape 6 — Vérifier l'install complète

```bash
bash verify.sh
```

Affiche un récap PASS/FAIL : KVM, Java, Android SDK, AVDs, snapshots, service
runner systemd, Docker. Tout en vert → prêt pour le 1er smoke-test workflow.

### Étape 7 — Smoke-test du workflow

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
