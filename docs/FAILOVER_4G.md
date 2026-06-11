# Failover internet 4G (SIM7600E-H) — runbook + smoketest

> **Rôle 3 du module SIM7600** : connexion internet de secours sur les nœuds **on-site**.
> Voir le design dans [`SITE_SECURITY_NOTES.md`](../SITE_SECURITY_NOTES.md) (« Rôle 3 — Failover
> internet 4G ») et [`ARCHITECTURE_SMS_VOIX.md`](../ARCHITECTURE_SMS_VOIX.md) (§ Failover 4G).
> Ce document est le **runbook opérationnel** (activation concrète + smoketest), absent jusqu'ici.
>
> ⚠️ **Deux modes possibles selon le firmware/composition USB du module** :
> - **QMI** (`qmi_wwan` / `/dev/cdc-wdm0`, framing raw-ip, piloté par `qmicli`) — **c'est le mode du
>   matériel réel sur onsite-2**. Path concret en [§2bis](#2bis-activation-qmi-matériel-réel-onsite-2).
> - **ECM** (`cdc_ether` + DHCP, le module fait le dial seul) — décrit en [§2](#2-activation-ecm-nm-free).
>
> **Pour le smoketest, ne pas suivre les commandes à la main : lancer le script automatisé**
> [`scripts/smoketest-4g.sh`](../scripts/smoketest-4g.sh) (QMI, idempotent, exit-code gateable) — cf [§4](#4-smoketest).

## 0. Contexte et statut

- **Où** : nœuds on-site uniquement (`onsite-1` 172.16.1.121, `onsite-2` 172.16.1.120). Le module
  est physiquement branché et visible sur onsite-2 (cf [`PROVISIONING_ONSITE.md`](PROVISIONING_ONSITE.md) §1).
  **NODE3 cloud n'a pas de modem** (N/A, cf §22ter).
- **Mode réel (onsite-2)** : le module énumère en **QMI** (`qmi_wwan`), pas en ECM. Vérifier le mode
  avant toute chose :
  ```bash
  for c in /sys/class/net/ww*; do echo "$c -> $(readlink -f "$c/device/driver")"; done
  # .../qmi_wwan  → QMI (cas onsite-2)  → §2bis (qmicli)
  # .../cdc_ether → ECM                 → §2     (dhclient)
  ls /dev/cdc-wdm*    # présent ⇒ control QMI dispo
  ```
- **Principe** : le SIM7600E-H expose une interface réseau (`wwp0s20f0u4i5` sur onsite-2 — le nom
  dépend du port USB). On lui donne une **route par défaut de métrique 300** : Linux route
  automatiquement par le lien UP de plus basse métrique, donc la 4G ne sert que si fibre (métrique
  ~100) **et** Starlink (~200) sont tombées. Aucun script de bascule. En QMI, c'est `qmicli` qui monte
  le lien data (l'hôte pose ensuite l'IP/route) ; en ECM, le module dial seul et l'hôte fait du DHCP.
- **Standby « chaud »** : l'interface reste UP en permanence (registration + DHCP), métrique 300.
  Conso au repos ≈ 0, bascule instantanée. Le forfait data (~120 Go/mois) couvre largement un
  failover soutenu portant la réplication PostgreSQL + les push FCM.
- **Statut actuel : VALIDÉ et ARMÉ sur onsite-2 (2026-06-11).** Failover prouvé de bout en bout
  (`smoketest-4g.sh --failover` → PASS 14/14 : bascule réelle, cloud + FCM joignables via 4G). Tourne
  en **LTE Bande 20** (SIM Sosh activée + `CNMP=54` qui exclut la 2G → accroche la 4G). Persistance en
  place : standby chaud via `alarm-4g-standby.service` (`scripts/4g-standby.sh`, QMI, reconnecte tout
  seul, route métrique 300) + `alarm-modem-rat.service` (`scripts/modem-set-rat.py`, force `CNMP=54` au
  boot, avant la gateway). Survit aux reboots (services enabled). Coexiste avec la gateway SMS/voix
  (`modem_gateway.py`, port AT) car le data passe en QMI (`cdc-wdm0`) — interfaces séparées.
- **Correctif vs design existant** : l'esquisse `ARCHITECTURE_SMS_VOIX.md` utilise `nmcli`
  (NetworkManager) et `AT+NETOPEN`. Or les nœuds n'embarquent pas NetworkManager, et `NETOPEN`
  active la **pile TCP/IP interne du modem** (sockets via AT), **pas** un lien data porté par l'hôte.
  De plus, le module réel **n'est pas en ECM mais en QMI** : pas de DHCP, c'est `qmicli` qui ouvre la
  session WDS et l'hôte pose l'IP/route (cf §2bis). Ce runbook est la version NM-free, alignée sur le
  provisioning réel (`qmi_wwan` + `ModemManager` masqué).

---

## 1. Pré-requis

- Cluster prod up (cf [`PROVISIONING_ONSITE.md`](PROVISIONING_ONSITE.md) §23, bring-up 3-way 2026-05-10).
- `ModemManager` masqué (déjà fait au provisioning) — sinon il confisque les ports et se bat
  avec notre conf manuelle. Vérifier : `systemctl is-enabled ModemManager` → `masked`.
- Port AT stable `/dev/sim7600_at` (symlink udev, cf [`ARCHITECTURE_SMS_VOIX.md`](../ARCHITECTURE_SMS_VOIX.md) §
  règles udev) — sinon utiliser `/dev/ttyUSB2`.
- SIM avec forfait **data** active (~120 Go/mois) insérée et déverrouillée.
- Outil AT en ligne de commande : `sudo apt install -y picocom` (ou `socat`/`minicom`).

> Helper pour les commandes AT de ce runbook :
> ```bash
> at() { printf '%s\r' "$1" | sudo picocom -qrx 2000 -b 115200 /dev/sim7600_at; }
> ```

---

## 2. Activation ECM (NM-free)

> ⚠️ **N'utiliser cette section que si la recon (§0) montre `cdc_ether`.** Sur onsite-2 le module est
> en **QMI** → aller directement au [§2bis](#2bis-activation-qmi-matériel-réel-onsite-2). Cette section
> reste documentée pour un module qui énumérerait en ECM (autre firmware/composition USB).

### 2.1. Vérifier module, SIM, registration

```bash
at 'AT'                # → OK (modem répond)
at 'AT+SIMCOMATI'      # firmware (utile pour la compatibilité ECM/PID)
at 'AT+CPIN?'          # → +CPIN: READY (SIM déverrouillée)
at 'AT+CSQ'            # signal : RSSI >= ~10 souhaitable (99 = pas de signal)
at 'AT+COPS?'          # opérateur enregistré
at 'AT+CEREG?'         # registration LTE : stat 1 (home) ou 5 (roaming) = OK
```

### 2.2. APN + activation du contexte data

APN selon l'opérateur (SIM réelle onsite-2 = **Sosh**, MVNO Orange → `orange` ; Free Mobile → `free`).

```bash
at 'AT+CGDCONT=1,"IP","free"'   # définit l'APN sur le contexte 1
at 'AT+CGACT=1,1'               # active le contexte (le dial ECM est ensuite automatique)
at 'AT+CGPADDR=1'               # → +CGPADDR: 1,"x.x.x.x" : une IP attribuée = data OK
```

### 2.3. Identifier l'interface réseau ECM

Sur la plupart des firmwares SIM7600E-H, l'interface `cdc_ether` (ECM) est déjà énumérée :

```bash
ip -br link | grep -i wwp                 # → wwp0s20f0u4i5 (nom réel à noter)
IF=wwp0s20f0u4i5
readlink "/sys/class/net/$IF/device/driver"   # doit finir par /cdc_ether (= ECM)
```

> Si **aucune** interface `wwp*`/`usb*` n'apparaît : le modem n'est pas en composition USB ECM.
> Basculer la composition via `AT+CUSBPIDSWITCH` (la **valeur de PID dépend du firmware** — lire
> `AT+CUSBPIDSWITCH?` et le wiki Waveshare SIM7600E-H), puis recompter les interfaces.

### 2.4. IP + route par défaut métrique 300 (non-prioritaire)

Test à chaud (non persistant) :

```bash
sudo dhclient -v "$IF"
ip route show dev "$IF"            # le module (ECM) fait du NAT, IP privée type 192.168.x.x
# S'assurer que le défaut 4G est en métrique 300 (sous la fibre métrique ~100) :
sudo ip route replace default via "$(ip -4 route show dev "$IF" | awk '/default/{print $3}')" \
     dev "$IF" metric 300
ip route show default              # fibre metric 100 EN PREMIER, 4G metric 300 ensuite
```

### 2.5. Persistance

**a) Interface (ifupdown — défaut Debian server)** — `/etc/network/interfaces.d/4g-ecm` :

```
allow-hotplug wwp0s20f0u4i5
iface wwp0s20f0u4i5 inet dhcp
    metric 300
```

> Équivalent systemd-networkd si le nœud l'utilise — `/etc/systemd/network/30-4g-ecm.network` :
> `[Match] Name=wwp0s20f0u4i5` / `[Network] DHCP=ipv4` / `[DHCPv4] RouteMetric=300` + `UseDNS=no`.

**b) APN au boot (oneshot)** — `/opt/alarm/gateway/4g-ecm-up.sh` (mode 750, owner `alarm`) :

```bash
#!/usr/bin/env bash
set -euo pipefail
PORT=/dev/sim7600_at
send() { printf '%s\r' "$1" > "$PORT"; sleep 1; }
send 'AT+CGDCONT=1,"IP","free"'
send 'AT+CGACT=1,1'
```

Unit `/etc/systemd/system/alarm-4g-ecm.service` :

```ini
[Unit]
Description=SIM7600 ECM APN init (failover 4G)
After=dev-sim7600_at.device
Before=network-pre.target
Wants=network-pre.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/opt/alarm/gateway/4g-ecm-up.sh
[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now alarm-4g-ecm.service
```

### 2.6. DNS en secours uniquement

Le DHCP du module pousse souvent ses propres résolveurs. Pour éviter qu'ils ne masquent ceux de
la fibre : `UseDNS=no` (networkd) ou, en ifupdown, ne pas laisser dhclient écraser `resolv.conf`
pour cette interface (`supersede domain-name-servers` dans un hook dédié `$IF`). La résolution doit
rester primaire via le lien fixe ; la 4G ne fournit le DNS que si elle devient l'unique route.

---

## 2bis. Activation QMI (matériel réel onsite-2)

> **C'est le chemin réel sur onsite-2** (`qmi_wwan` / `/dev/cdc-wdm0`). Le contrôle data passe par
> `cdc-wdm0` (libqmi), ce qui **laisse les ports AT `/dev/ttyUSBx` libres pour la gateway SMS** (Rôle 1).
> Tout ceci est **automatisé par [`scripts/smoketest-4g.sh`](../scripts/smoketest-4g.sh)** ; les commandes
> ci-dessous documentent ce que le script fait pas à pas (pour debug / activation manuelle).

### 2bis.1. Prérequis QMI

```bash
sudo apt-get install -y libqmi-utils      # fournit qmicli
ls /dev/cdc-wdm0                           # control QMI présent
# ModemManager doit rester masqué (sinon il prend cdc-wdm0) :
systemctl is-enabled ModemManager          # → masked
```

### 2bis.2. Monter le lien data (raw-ip)

```bash
IF=$(for c in /sys/class/net/ww*; do case "$(readlink -f "$c/device/driver")" in
       *qmi_wwan*) basename "$c"; break;; esac; done)
WDM=/dev/cdc-wdm0

sudo ip link set "$IF" down
echo Y | sudo tee "/sys/class/net/$IF/qmi/raw_ip"     # framing raw-ip obligatoire en QMI
sudo ip link set "$IF" up

# Démarre la session WDS (APN Free = "free"), garde le CID pour pouvoir l'arrêter proprement :
sudo qmicli -d "$WDM" -p --wds-start-network="apn='free',ip-type=4" --client-no-release-cid
#  → note le « Network started ... handle: 'NNN' » et le « CID: 'M' »
```

### 2bis.3. Récupérer l'IP attribuée et poser la route métrique 300

```bash
sudo qmicli -d "$WDM" -p --wds-get-current-settings
#  → IPv4 address / gateway / subnet mask poussés par le réseau opérateur
IP=...; GW=...; PFX=...   # depuis la sortie ci-dessus (le script parse tout seul)
sudo ip addr replace "$IP/$PFX" dev "$IF"
sudo ip route replace default via "$GW" dev "$IF" metric 300   # sous la fibre, non-prioritaire
```

### 2bis.4. Arrêt propre

```bash
sudo qmicli -d "$WDM" -p --wds-stop-network=HANDLE --client-cid=CID
sudo ip route del default dev "$IF" 2>/dev/null
sudo ip addr flush dev "$IF"; sudo ip link set "$IF" down   # (sauf si on garde le standby chaud)
```

### 2bis.5. Persistance (standby chaud au boot)

Pas de DHCP en QMI : il faut un service oneshot qui rejoue 2bis.2–2bis.3 au démarrage.
La voie propre est **`qmi-network`** (fourni par libqmi-utils) piloté par systemd, ou un wrapper
maison appelant `qmicli`. À formaliser quand on promeut le failover hors smoketest (cf §6) — pour
l'instant le standby chaud s'obtient avec `scripts/smoketest-4g.sh --keep-up`.

---

## 3. Cohabitation avec la gateway SMS/voix (Rôle 1)

- Le lien data (control **QMI `cdc-wdm0`** sur le matériel réel, ou interface ECM) est
  **indépendant** du port AT `/dev/sim7600_at` : la gateway SMS/voix peut envoyer SMS/voix pendant que
  la 4G porte de l'IP. En QMI c'est même la raison du choix : `cdc-wdm0` ≠ `ttyUSBx`, zéro contention.
- **Appel vocal** : sur réseau LTE sans VoLTE, le modem fait du CSFB (bascule 2G/3G) et la data est
  suspendue le temps de l'appel. Dans la séquence d'escalade, push FCM (data) et appel (voix) ne
  sont jamais simultanés (push AVANT l'appel) — cf `ARCHITECTURE_SMS_VOIX.md` § cohabitation. Si
  Free fournit la VoLTE sur ce modem, la data n'est pas interrompue.

---

## 4. Smoketest

> But : prouver que le chemin 4G atteint internet **et** que la bascule fonctionne, sans attendre
> une vraie coupure fibre. Deux niveaux : **L1 non-disruptif** (à lancer n'importe quand) puis
> **L2 bascule contrôlée** (fenêtre de maintenance, avec auto-revert).
>
> **Accès shell préservé** : le SSH vers les nœuds on-site passe par le **LAN** (`172.16.1.12x` via
> `enp*`), pas par la route internet par défaut. Démonter la route internet ne coupe donc pas la
> session SSH. On garde quand même un filet auto-revert pour L2.

### 4.0. Script automatisé (recommandé) — [`scripts/smoketest-4g.sh`](../scripts/smoketest-4g.sh)

Le script fait **toute la séquence QMI** (§2bis) + les vérifs L1/L2 + le teardown, de façon
idempotente, et renvoie un **exit code gateable en CI** (0 = PASS, ≠0 = FAIL). Préférer ça aux
commandes manuelles ci-dessous.

```bash
# L1 — monte la 4G, teste l'accès internet sans toucher au routage de prod, démonte :
sudo bash scripts/smoketest-4g.sh

# L1 puis laisse la 4G en standby chaud (métrique 300, prête pour un vrai failover) :
sudo bash scripts/smoketest-4g.sh --keep-up

# L1 + L2 — bascule réelle (démote la fibre), auto-revert 180 s, puis restaure :
sudo bash scripts/smoketest-4g.sh --failover

# Overrides éventuels (APN/interface/cibles) :
APN=free IF=wwp0s20f0u4i5 sudo -E bash scripts/smoketest-4g.sh
```

Le script auto-détecte l'interface `qmi_wwan`, installe `libqmi-utils` au besoin, et capture la route
fibre de référence pour la restaurer en garde-fou. Les sous-sections 4.1–4.4 ci-dessous détaillent les
**mêmes vérifs en manuel** (debug, ou modules en ECM).

### 4.1. L1 — non-disruptif (bind sur l'interface 4G)

Force le trafic à sortir par la 4G **sans toucher au routage de prod** :

```bash
IF=wwp0s20f0u4i5
# (a) IP publique vue par la 4G — doit différer de l'egress fibre du site (31.204.85.180)
curl --interface "$IF" -s https://api.ipify.org ; echo
# (b) latence brute 4G
ping -I "$IF" -c4 1.1.1.1
# (c) le cloud OVH (NODE3) est joignable par la 4G
ping -I "$IF" -c4 51.210.105.102
# (d) le routage de prod n'a PAS bougé (doit sortir via la fibre 172.16.0.1) :
ip route get 1.1.1.1
```

**Pass L1** : (a) renvoie une IP opérateur ≠ `31.204.85.180`, (b)/(c) répondent, (d) montre toujours
la fibre. → le lien 4G fonctionne de bout en bout, prod intacte.

### 4.2. L2 — bascule contrôlée (avec auto-revert)

Pré-requis : standby chaud actif (§2.4 — fibre metric 100 + 4G metric 300 toutes deux présentes).

```bash
GW_FIBRE=172.16.0.1 ; DEV_FIBRE=enp1s0   # adapter par nœud (onsite-1 = enp0s31f6)

# (0) FILET DE SÉCURITÉ — restaure la fibre dans 180 s quoi qu'il arrive :
sudo systemd-run --on-active=180 --timer-property=AccuracySec=1s \
  ip route replace default via "$GW_FIBRE" dev "$DEV_FIBRE" metric 100

# (1) DÉCLENCHE la bascule : démote la fibre sous la 4G → la 4G (metric 300) devient la meilleure :
sudo ip route replace default via "$GW_FIBRE" dev "$DEV_FIBRE" metric 9000

# (2) VALIDATIONS pendant la bascule :
ip route get 51.210.105.102                       # doit sortir via wwp0s20f0u4i5
sudo wg show wg0 latest-handshakes                # handshake NODE3 < ~130 s
ping -c4 10.99.0.1                                 # NODE3 via le tunnel WG, sur 4G
sudo -u alarm patronictl -c /opt/alarm/patroni/patroni.yml list   # 3 membres, lag borné
curl -s http://10.99.0.1:8000/health              # backend cloud joignable via WG/4G
curl -s https://fcm.googleapis.com -o /dev/null -w '%{http_code}\n'  # Google FCM joignable

# (3) (optionnel golden path) lever une alarme de test et confirmer la réception du push sur device

# (4) RESTAURE tout de suite (ne pas attendre le timer) :
sudo ip route replace default via "$GW_FIBRE" dev "$DEV_FIBRE" metric 100
ip route get 1.1.1.1                               # de nouveau via la fibre
```

### 4.3. Critères de réussite

| # | Vérification | Attendu |
|---|---|---|
| 1 | `ip route get 51.210.105.102` (pendant bascule) | egress `dev wwp0s20f0u4i5` |
| 2 | `wg show wg0 latest-handshakes` | handshake NODE3 récent (< ~130 s) |
| 3 | `ping 10.99.0.1` | répond via le tunnel (perte < 5 %) |
| 4 | `patronictl list` | 3 membres visibles, replication lag borné |
| 5 | `/health` cloud + `fcm.googleapis.com` | joignables |
| 6 | Après restore : `ip route get 1.1.1.1` | de nouveau via la fibre, 4G idle |

### 4.4. Comptabilité data

```bash
# Avant / après L2 (delta = data consommée par le test) :
cat /sys/class/net/wwp0s20f0u4i5/statistics/rx_bytes \
    /sys/class/net/wwp0s20f0u4i5/statistics/tx_bytes
# Suivi mensuel recommandé : sudo apt install -y vnstat ; vnstat -i wwp0s20f0u4i5
```

> Penser à `vnstat` en place pour surveiller la conso si un vrai failover se prolonge (alerte si on
> approche le plafond du forfait).

---

## 5. Rollback / abort

- **Abandon en cours de test** : `sudo ip route replace default via 172.16.0.1 dev <DEV_FIBRE> metric 100`.
  Le filet `systemd-run` (§4.2-0) le fait de toute façon sous 180 s.
- **Désactiver complètement le failover** : `sudo systemctl disable --now alarm-4g-ecm.service`,
  retirer `/etc/network/interfaces.d/4g-ecm` (ou le `.network`), `sudo ifdown wwp0s20f0u4i5`.
- **Lister les timers transitoires restants** : `systemctl list-timers | grep run-`.

---

## 6. Reste-à-faire avant prod

- [ ] Exécuter `scripts/smoketest-4g.sh` (L1) sur onsite-2 (module présent), puis `--failover` (L2),
      puis répéter sur onsite-1.
- [ ] Confirmer le nom réel de l'interface 4G sur **chaque** nœud on-site (dépend du port USB) — le
      script l'auto-détecte mais le figer dans une conf de persistance demande la valeur stable.
- [ ] **Persistance QMI au boot** : industrialiser le standby chaud (oneshot `qmi-network`/wrapper
      `qmicli` sous systemd — cf §2bis.5). Aujourd'hui seul `--keep-up` le fait, non persistant au reboot.
- [ ] Valider la VoLTE Free sur ce modem (sinon CSFB suspend la data pendant les appels — déjà
      géré par l'ordre push-avant-appel de l'escalade).
- [ ] Health-check périodique « quelle route est active » + alerte conso `vnstat`.
