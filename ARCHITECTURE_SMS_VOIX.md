# Architecture SMS & Appels Vocaux — Alarm 2.0

> Document de référence pour l'implémentation. Dernière MAJ : 2026-04-11

## Décision finale

Cluster Patroni 3 nœuds (voir `docs/architecture_option_B_3vps_patroni.md`). Pas de cloud Twilio/OVH pour les notifications. Le module 4G (SMS + appels vocaux) est connecté en USB **uniquement sur le nœud on-site** (NODE1).

Les 3 nœuds backend (NODE1 on-site, NODE2 cloud, NODE3 cloud) répliquent la base PostgreSQL. La table `SmsQueue` / `CallQueue` est répliquée sur tous les nœuds. Le gateway SMS/voix tourne sur NODE1 et poll les SMS/appels en attente depuis le backend local (ou depuis les VPS cloud en fallback si le backend local est en standby).

---

## Hardware retenu

### Nœud on-site (NODE1)

Mini PC business (Lenovo ThinkCentre Tiny, HP ProDesk Mini, Dell OptiPlex Micro — au choix).
Fait tourner : Backend FastAPI + PostgreSQL (Patroni) + gateway SMS/voix. Module 4G branché en USB sur cette machine.

### Module 4G

| Élément | Détail |
|---------|--------|
| **Produit** | Waveshare SIM7600E-H 4G HAT |
| **Chipset** | SIMCom SIM7600E-H (le "-H" = support audio/voix/TTS) |
| **Lien achat** | https://www.waveshare.com/sim7600e-h-4g-hat.htm |
| **Revendeur EU** | Welectron (Allemagne) — ~70€ — https://www.welectron.com/Waveshare-14952-SIM7600E-H-4G-HAT_1 |
| **Connexion** | USB micro-B → mini PC (câble fourni) |
| **Bandes 4G** | B1/B3/B5/B7/B8/B20 (couverture France complète) |
| **Fallback** | 3G UMTS + 2G GSM (CSFB pour les appels vocaux) |
| **SIM** | Free Mobile 2€/mois — appels + SMS illimités en France |
| **Antenne** | SMA 4G incluse dans le kit, à positionner si possible près d'une fenêtre |

### Coût total récurrent

~2€/mois (forfait Free Mobile uniquement). Pas de coût à l'appel, pas de coût au SMS.

---

## Ports USB Linux

Le SIM7600E-H crée automatiquement 5 ports série via le driver `option` du kernel Linux :

| Port | Fonction | Usage Alarm 2.0 |
|------|----------|-----------------|
| `/dev/ttyUSB0` | Diagnostic | Non utilisé |
| `/dev/ttyUSB1` | NMEA (GPS) | Non utilisé |
| `/dev/ttyUSB2` | **AT commands** | SMS + appels + TTS |
| `/dev/ttyUSB3` | Modem PPP | Data 4G (backup réseau éventuel) |
| `/dev/ttyUSB4` | **Audio USB** | Flux PCM brut pour décodage DTMF |

### Règles udev (nommage persistant au reboot)

Waveshare documente la procédure : https://www.waveshare.com/wiki/Fixed_ttyUSB*

```bash
# 1. Identifier le ID_PATH
udevadm info /dev/ttyUSB2 | grep ID_PATH

# 2. Créer /etc/udev/rules.d/99-sim7600.rules
SUBSYSTEM=="tty", ENV{ID_PATH}=="<valeur>:1.2", SYMLINK+="sim7600_at"
SUBSYSTEM=="tty", ENV{ID_PATH}=="<valeur>:1.4", SYMLINK+="sim7600_audio"

# 3. Recharger
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Résultat : `/dev/sim7600_at` et `/dev/sim7600_audio` stables quel que soit l'ordre de branchement USB.

---

## Commandes AT vérifiées

Toutes documentées dans le manuel officiel SIMCom :
https://www.waveshare.com/w/upload/8/8f/SIM7500_SIM7600_Series_AT_Command_Manual_V1.12.pdf

### SMS

```
AT+CMGF=1              # Mode texte (pas PDU)
AT+CMGS="+33612345678"  # Destinataire, puis corps du message, terminé par Ctrl+Z (0x1A)
```

### Appel vocal sortant

```
ATD+33612345678;        # Appeler (le ; est obligatoire pour un appel voix)
ATH                     # Raccrocher
AT+CLCC                 # État de l'appel en cours
```

### TTS (synthèse vocale pendant l'appel)

```
AT+CTTS=2,"Alarme critique. Appuyez sur 1 pour acquitter."
# 2 = texte ASCII, 1 = texte UCS2
# Le message est joué au correspondant pendant l'appel actif
```

### Réception SMS entrants (acquittement par réponse SMS)

```
AT+CNMI=2,1,0,0,0      # Active les notifications de nouveau SMS (+CMTI)
AT+CMGL="ALL"           # Liste tous les SMS stockés
AT+CMGR=<index>         # Lire un SMS par index
AT+CMGD=<index>         # Supprimer un SMS lu
```

Le modem envoie une notification non-sollicitée quand un SMS arrive :
```
+CMTI: "SM",3           # Nouveau SMS stocké à l'index 3
```

Le gateway lit alors le SMS avec `AT+CMGR=3`, extrait le numéro expéditeur et le corps.

### DTMF — ⚠️ Limitation hardware

Le SIM7600 **ne supporte PAS** la détection DTMF matérielle (pas de AT+DDET).
Il peut *générer* des DTMF (AT+VTS) mais pas les *décoder* depuis le correspondant.

**Workaround retenu : décodage logiciel via le port audio USB.**

---

## Décodage DTMF logiciel

### Principe

Le port `/dev/ttyUSB4` (audio USB) expose le flux audio de l'appel en cours :
- Format : PCM brut, 8000 Hz, 16-bit signé, mono
- Lecture en Python via pyserial

L'algorithme de Goertzel détecte les 7 fréquences DTMF (697, 770, 852, 941, 1209, 1336, 1477 Hz) dans ce flux. C'est l'algorithme standard utilisé par tous les décodeurs DTMF embarqués.

### Référence d'implémentation

- Code USB audio SIM7600 : https://github.com/elementzonline/GSMModem/blob/master/SIM7600/Code/usb_audio_test.py
- Goertzel DTMF en Python : https://github.com/rymshasaeed/dtmf-decoder

### Flux de données pendant un appel

```
/dev/sim7600_at  ←→  AT commands (appeler, TTS, raccrocher)
                      ↕ (même puce SIM7600)
/dev/sim7600_audio →  flux PCM 8kHz → thread Python → Goertzel → touche détectée
```

---

## Séquence du gateway pour un utilisateur notifié

Quand le gateway détecte qu'un utilisateur doit recevoir un SMS + appel (via les queues), il exécute **séquentiellement** sur le modem unique :

```
1. SMS D'ABORD (pour que l'utilisateur puisse ack via le lien dans le SMS)
   AT+CMGF=1
   AT+CMGS="+336XXXXXXXX"\r
   "ALARME CRITIQUE: {titre}\r\nAcquitter: {url_app}"\x1A
   Attente "OK" ou "ERROR"
   Marquage sms sent_at en DB

2. APPEL IMMÉDIATEMENT APRÈS LE SMS (~2-5s plus tard)
   Re-vérifier en DB si l'alarme est toujours active (l'user a pu ack via l'app/SMS)
   Si toujours active :
     ATD+336XXXXXXXX;                          → appel sortant
     Attente décrochage (AT+CLCC polling, état "0" = actif)
     AT+CTTS=2,"Alarme critique sur le site. Appuyez sur 1 pour acquitter, 2 pour escalader."
     Thread parallèle lit /dev/sim7600_audio
     Goertzel détecte la touche appuyée :
       - "1" → acquittement enregistré en DB, ATH (raccrocher)
       - "2" → escalade forcée au palier suivant, ATH
       - Timeout 30s sans touche → raccrocher, marquer comme "non acquitté"
     ATH (raccrocher dans tous les cas)
```

Le SMS arrive sur le téléphone pendant que l'appel sonne (~2-5s de décalage). L'utilisateur peut acquitter par **4 canaux** :

- **DTMF** : Décrocher et appuyer sur 1 → acquittement immédiat via le gateway
- **Réponse SMS** : Répondre "1" ou "OK" au SMS → le gateway lit le SMS entrant et acquitte
- **App Android** : Ouvrir l'app (via le lien dans le SMS ou directement) → acquittement via l'API
- **Ignorer** → le gateway passe à l'utilisateur suivant dans la queue

### Écoute des SMS entrants (thread permanent)

En parallèle de l'envoi SMS/appels, le gateway tourne un **thread d'écoute SMS entrants** permanent :

```
1. AT+CNMI=2,1,0,0,0                    → activer notifications +CMTI
2. Thread boucle sur le port série AT :
   - Détecte "+CMTI: \"SM\",<index>"
   - AT+CMGR=<index>                    → lit le SMS (expéditeur + corps)
   - Match le numéro expéditeur avec un utilisateur notifié en DB
   - Si le corps contient "1" ou "OK" (case-insensitive) :
     → POST /internal/alarms/{id}/ack   → acquittement en DB
     → Log "Ack par SMS depuis +336XXXXXXXX"
   - AT+CMGD=<index>                    → supprime le SMS traité
```

**Contrainte CSFB** : Pendant un appel vocal, le modem est en 2G/3G. Les SMS entrants peuvent être mis en file d'attente par le réseau et livrés à la fin de l'appel (~30s de retard max). Ce n'est pas bloquant : si l'utilisateur répond au SMS pendant l'appel, l'ack sera traité dès le raccroché. Si l'utilisateur acquitte par DTMF pendant l'appel, le SMS de réponse éventuel sera simplement ignoré (alarme déjà acquittée).

---

## Logique de déclenchement SMS + Appel

### Paramètres (éditables depuis le front)

| Paramètre | Clé SystemConfig | Défaut | Description |
|-----------|-----------------|--------|-------------|
| Délai SMS/appel | `sms_call_delay_minutes` | 2 | Minutes après notification push avant d'envoyer SMS + appel |
| Délai escalade | `escalation_delay_minutes` | 15 | Minutes avant d'escalader au palier suivant (uniforme pour tous les users) |

### Séquence pour chaque utilisateur notifié

```
Utilisateur ajouté aux notifiés (notified_at = now)
  │
  ├─ t+0                    → Push FCM (immédiat, comme aujourd'hui)
  │
  ├─ t+sms_call_delay_minutes → SMS + Appel vocal (si pas d'ack)
  │                             Le gateway envoie le SMS, puis appelle immédiatement après
  │
  └─ t+escalation_delay     → ESCALADE : utilisateur suivant ajouté aux notifiés
                               (et ce nouvel utilisateur a ses propres timers)
```

### Timeline concrète (défauts : sms_call=2min, escalade uniforme=15min)

```
ALARME CRÉÉE
│
├─ t+0      user1 (astreinte) reçoit Push FCM
├─ t+2min   user1 reçoit SMS + Appel vocal (si pas d'ack)
│
├─ t+15min  ESCALADE → user2 ajouté (user1 continue de sonner)
│           Si user1 offline : FCM wake-up high-priority tenté sur user1 au même tick
│           user2 reçoit Push FCM
│           user1 reçoit Push FCM (rappel cumulative)
├─ t+17min  user2 reçoit SMS + Appel (si pas d'ack)
│
├─ t+30min  ESCALADE → admin ajouté (15 min après user2)
│           admin reçoit Push FCM
│           user1 + user2 reçoivent Push FCM (rappel)
├─ t+32min  admin reçoit SMS + Appel (si pas d'ack)
│
├─ t+45min  REBOUCLAGE → retour user1
│           ...
│
└─ Acquittement par UN DES NOTIFIÉS (app, DTMF, réponse SMS) → TOUT s'arrête
```

### Implémentation dans escalation.py

La boucle d'escalade (tick toutes les 10s) fait déjà le FCM + enqueue SMS à l'escalade.
Changements nécessaires :

1. **Enrichir `AlarmNotification`** avec `notified_at` (timestamp), `sms_sent` (bool), `call_sent` (bool)
2. **Nouveau bloc dans le tick** (entre le bloc escalade et le bloc on-call) :
   ```
   Pour chaque alarme active :
     Pour chaque utilisateur notifié (alarm_notifications) :
       Si not sms_sent ET (now - notified_at) >= sms_call_delay_minutes :
         → enqueue SMS + enqueue Call
         → marquer sms_sent = True, call_sent = True
   ```
3. **Retirer `_enqueue_sms_for_user`** du bloc escalade actuel (ligne 176) — le SMS n'est plus lié à l'escalade mais au timer par utilisateur
4. **Ajouter `_enqueue_call_for_user`** similaire à `_enqueue_sms_for_user` avec guard anti-doublon

Le FCM reste envoyé à l'escalade (comportement actuel inchangé).
Le SMS + Appel sont envoyés par le nouveau bloc basé sur `notified_at + sms_call_delay`.

---

## Chantiers d'implémentation (TDD)

### Chantier 1 — Nouveau gateway SMS/Voix (remplace gammu)

Fichier : `gateway/modem_gateway.py` (nouveau)

- Classe `ModemGateway` : gère la connexion pyserial au SIM7600
- Méthodes : `send_sms()`, `make_call()`, `play_tts()`, `listen_dtmf()`
- Thread audio dédié pour le décodage DTMF Goertzel
- **Thread écoute SMS entrants** : écoute permanente des `+CMTI`, lit le SMS, matche l'expéditeur avec un utilisateur notifié, acquitte si "1" ou "OK"
- Poll `GET /internal/sms/pending` (existant) + nouveau `GET /internal/calls/pending`
- Poll d'abord le backend local (NODE1), fallback sur VPS2/VPS3 si NODE1 est en standby Patroni
- Même logique que l'actuel `sms_gateway.py` (VPS1 → VPS2) mais étendue à 3 nœuds
- Endpoint d'acquittement interne : `POST /internal/alarms/{id}/ack?user_id=X` (appelé par le gateway quand ack DTMF ou SMS reçu)
- Gestion des erreurs AT (retry, reset modem si bloqué)

### Chantier 2 — Backend : déclenchement SMS + Appels par timer

- Nouveau modèle `CallQueue` (similaire à `SmsQueue`)
- Endpoints : `GET /internal/calls/pending`, `POST /internal/calls/{id}/result`
- Enrichir `AlarmNotification` : ajouter `notified_at` (datetime), `sms_sent` (bool, défaut False), `call_sent` (bool, défaut False)
- Nouveau paramètre `SystemConfig` : `sms_call_delay_minutes` (défaut 2, éditable depuis le front)
- `escalation.py` : nouveau bloc dans le tick — pour chaque notifié, si `now - notified_at >= sms_call_delay` et pas encore envoyé → enqueue SMS + Call
- `escalation.py` : retirer `_enqueue_sms_for_user` du bloc escalade (le SMS est maintenant déclenché par le timer, pas par l'escalade)
- `_enqueue_call_for_user()` avec guard anti-doublon (même pattern que `_enqueue_sms_for_user`)
- L'horloge injectable existante permet de tester les délais sans sleep

### Chantier 3 — Décodeur DTMF Python

Fichier : `gateway/dtmf_decoder.py` (nouveau)

- Implémentation Goertzel sur buffer PCM 8kHz
- Détection des touches 0-9, *, #
- Anti-rebond (une touche = un événement, même si maintenue)
- Testable en isolation avec des fichiers WAV de test

### Chantier 4 — Tests E2E

- Backend : adapter les 7 tests SMS existants (`TestSmsAndHealth`) + ajouter tests appels
- Mock du modem côté tests backend (pas de vrai hardware pendant les tests)
- L'horloge injectable simule les délais d'escalade
- Tests du décodeur DTMF : fichiers WAV avec des tonalités connues → vérifier détection

### Chantier 5 — Nettoyage

- Remplacer `gateway/sms_gateway.py` (gammu) par le nouveau `modem_gateway.py`
- Remplacer `gateway/health_monitor.py` (gammu + Asterisk) — le health monitor reste mais utilise le SIM7600 au lieu de gammu
- Adapter `gateway/config.py` : ajouter NODE1/NODE2/NODE3 URLs, retirer gammu/Asterisk configs
- Mettre à jour `SITE_SECURITY_NOTES.md` section P2+P3
- Mettre à jour `IMPROVEMENTS.md` point #18

---

## Dépendances Python (nouvelles)

```
pyserial          # Communication série avec le SIM7600
numpy             # Goertzel (FFT partielle sur les fréquences DTMF)
```

---

## Failover 4G : backup internet pour les push FCM

### Principe

Le SIM7600E-H peut simultanément servir de gateway SMS/voix (via `/dev/sim7600_at`) ET de connexion internet de secours (via son port data `/dev/ttyUSB3` ou mode ECM/RNDIS sur interface `usb0`/`wwan0`).

Si la fibre tombe sur le site (et Starlink aussi), le nœud on-site (NODE1) utilise la 4G pour :
- Rester connecté au cluster Patroni (réplication PostgreSQL vers NODE2/NODE3)
- Envoyer les push FCM si NODE1 est le primary
- Permettre au gateway SMS/voix de poller les VPS cloud si NODE1 est en standby

### Configuration réseau Linux (NetworkManager)

```bash
# Fibre = prioritaire (métrique basse)
nmcli connection modify "Fibre" ipv4.route-metric 100

# Starlink = backup 1
nmcli connection modify "Starlink" ipv4.route-metric 200

# 4G SIM7600 = dernier recours
# Le SIM7600 en mode ECM crée une interface réseau (usb0 ou wwan0)
# Activation data : AT+CGDCONT=1,"IP","free" puis AT+NETOPEN sur /dev/sim7600_at
nmcli connection modify "4G-SIM7600" ipv4.route-metric 300
```

Linux route automatiquement vers la connexion de métrique la plus basse qui est UP. Aucun script de bascule custom nécessaire.

### Cohabitation voix + data sur un seul modem

Pendant un appel vocal, le SIM7600 fait du CSFB (bascule 2G/3G) → la data 4G est suspendue. Mais dans la séquence d'escalade, ce n'est pas un conflit :

```
t+0     Push FCM  → data 4G disponible (pas d'appel en cours)     ✅
t+2min  SMS       → AT commands, indépendant de la data            ✅
t+5min  Appel     → CSFB, data pause → mais FCM déjà parti         ✅
```

Les trois canaux (push, SMS, voix) ne se chevauchent jamais dans la séquence d'escalade.

### Data Free 2€

Le forfait Free 2€ inclut 1 Go de data/mois. Un push FCM ≈ 500 octets. Même avec 5 alarmes/nuit × 3 paliers × 30 jours = 450 push → ~225 Ko/mois. Marge immense.

### Chantier additionnel

Ajouté au chantier 1 : configurer le SIM7600 en mode ECM + créer le profil NetworkManager avec métrique 300. Ajouter un health-check périodique (ping serveurs FCM) pour logger quelle route est active.

---

## Risques identifiés

| Risque | Impact | Mitigation |
|--------|--------|------------|
| Modem SIM7600 bloqué (firmware hang) | Plus de SMS ni appels | Watchdog : si AT ne répond pas en 5s → reset GPIO ou cycle USB power |
| CSFB lent (bascule 4G→2G pour appel) | Délai 3-8s avant connexion appel | Acceptable pour un système d'alarme non temps-réel |
| Décodage DTMF bruité | Fausse détection ou non-détection | Seuils Goertzel calibrés + anti-rebond + demande de confirmation ("Vous avez appuyé 1, confirmé ?") |
| Free Mobile réseau saturé | SMS/appels retardés | Push FCM via Starlink/fibre reste le canal primaire ; SMS/voix = canal de secours |
| CSFB suspend la data pendant un appel | Push FCM impossible pendant un appel 4G | Séquence d'escalade envoie le push AVANT l'appel — jamais simultanés |
| Fibre + Starlink tombent ensemble | Plus d'internet fixe | Failover auto sur 4G SIM7600 (métrique 300), push FCM passe par la data Free |
| Un seul modem = un seul appel à la fois | Appels séquentiels, pas parallèles | Acceptable : escalade séquentielle par design |

---

## Documentation de référence

- Manuel AT SIM7500/SIM7600 V1.12 : https://www.waveshare.com/w/upload/8/8f/SIM7500_SIM7600_Series_AT_Command_Manual_V1.12.pdf
- Wiki Waveshare SIM7600E-H : https://www.waveshare.com/wiki/SIM7600E-H_4G_HAT
- Nommage udev persistant : https://www.waveshare.com/wiki/Fixed_ttyUSB*
- Code audio USB Python : https://github.com/elementzonline/GSMModem/blob/master/SIM7600/Code/usb_audio_test.py
- TTS library SIM7600 : https://github.com/zahidaof/SIM7600_TTS
- Goertzel DTMF Python : https://github.com/rymshasaeed/dtmf-decoder
- Issue DTMF (limitation confirmée) : https://github.com/Xinyuan-LilyGO/T-SIM7600X/issues/43
