# `scripts/provision/` — mise en service d'une machine sur site

Gestes qui produisent la machine standard décrite dans
[`docs/STANDARD_MACHINE.md`](../../docs/STANDARD_MACHINE.md). Tirés des scripts réellement utilisés
pour alarm-3 et alarm-4 (2026-11-05), rendus réutilisables :

- **Aucun mot de passe** dans les scripts ni dans le dépôt : `sudo` et `ssh` les demandent à
  l'opérateur au moment voulu (ou clé SSH). Mot de passe de `alarm` et du BIOS : gestionnaire de mots
  de passe du propriétaire.
- **Plan** : la machine est reconnue par son nom (`hostname -s`) dans `STD_PLAN` de
  [`infra/onsite/standard.env`](../../infra/onsite/standard.env) (nœud, IP WireGuard, site). Une
  machine absente du plan est **refusée** avant toute modification.
- **Idempotents** : relancer un script sans risque ; un script qui s'arrête dit pourquoi (`STOP : …`).
- Codes : 0 fait, 1 arrêt (prérequis, refus), 2 arguments invalides.

## Ordre standard

| # | Script | Où | En tant que | Fait |
|---|---|---|---|---|
| 0 | — | poste | opérateur | ajouter la machine à `STD_PLAN` + `.env.prod.nodeN` (PR fusionnée et promue `:stable`) ; BIOS : `docs/STANDARD_MACHINE.md` §2-§3 ; Debian 13 : §4 |
| 1 | — | poste | opérateur | `ssh-copy-id alarm@<IP>` (mot de passe demandé une fois) |
| 2 | — | machine | `alarm` | `sudo apt-get install -y git && sudo install -d -o alarm -g alarm /opt/alarm && git clone https://github.com/claude-murgat/Alarm2.0.git /opt/alarm` |
| 3 | `00-base.sh` | machine | root | paquets de base, Docker CE (dépôt officiel), journaux Docker bornés |
| 4 | `10-harden.sh` | machine | root | ModemManager masqué, ufw (LAN actuel), fail2ban, mises à jour de sécurité, chrony, journal persistant, sysctl, watchdog, SMART, `alarm-cd-pull` |
| 5 | `infra/onsite/install.sh` | machine | root | passerelle, pile modem, DNS de secours, timers (`alarm-host-sync`, `alarm-node-check`…) |
| 6 | `20-wireguard-node.sh` | machine | root | clé WireGuard, `wg0` à l'IP du plan, NODE3 seul pair ; affiche la commande de l'étape 7 |
| 7 | `21-wireguard-node3.sh <10.99.0.X> <clé> <nom>` | **NODE3** | root | déclare le pair (relayé, sans `Endpoint`) ; puis relancer l'étape 6 : « OK poignée de main » |
| 8 | `30-secrets.sh alarm@<IP>` | **poste** | opérateur | `.env.prod.secrets` + clé Firebase en flux NODE3 → machine, clé SSH de l'orchestrateur CD, empreintes comparées |
| 9 | `40-join.sh` | machine | root | `start-prod-node.sh --pull N`, attente `streaming` |
| 10 | `50-gateway.sh` | machine | root | `GATEWAY_KEY`, `GATEWAY_SITE` du plan, `DRY_CONTACT_SOURCE=host` |
| 11 | `10-harden.sh --ssh` | machine | root | coupe le mot de passe SSH (clé obligatoire) |
| 12 | `sudo alarm-node-check` | machine | root | **que des OK** (INV-186) ; sinon corriger avant de déclarer la machine en service |

Lancement type sur la machine : `sudo bash /opt/alarm/scripts/provision/00-base.sh` (depuis le poste :
`ssh -t alarm@<IP> sudo bash /opt/alarm/scripts/provision/00-base.sh`). Sur NODE3 :
`ssh -t -p 50922 -i ~/.ssh/alarm_node3 alarm@51.210.105.102 sudo bash /opt/alarm/scripts/provision/21-wireguard-node3.sh …`.
`30-secrets.sh` joint NODE3 par `NODE3_SSH` (défaut : `ssh -p 50922 -i ~/.ssh/alarm_node3 … alarm@51.210.105.102`).

## Maintenance (hors ordre)

| Script | Où | Quand |
|---|---|---|
| `60-change-ip.sh <IP/préfixe> <passerelle>` | machine, root | changer l'IP LAN (refuse une adresse qui répond déjà), redémarre dans 5 s ; pièges : `docs/STANDARD_MACHINE.md` §9.3 |
| `80-bios-stage.sh <9020MA19.exe>` | machine, root | vérifie taille + SHA-256 du BIOS Dell et le dépose sur `/boot/efi` ; le flash se fait sur place (F12) |
| `90-uefi-repair.sh` | machine, root | démarrage UEFI cassé : `dpkg --configure -a`, `grub-install` (+ chemin de secours), `update-grub`, entrées Windows orphelines retirées ; ne redémarre pas |

## Tests

`tests/unit/test_provision.py` : les vrais scripts, contre un faux `/` (`ALARM_ROOT`) et de fausses
commandes (`PATH`) ; `ALARM_STANDARD_ENV` remplace `standard.env`. `test_node_check.py` : le contrôle
de conformité (INV-186).
