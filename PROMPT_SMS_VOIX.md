# Prompt Claude Code — Implementation SMS + Appels Vocaux (Point 18)

## Contexte projet

Systeme d'alarmes critiques : backend FastAPI + app Android + cluster PostgreSQL HA (Patroni 3 noeuds).
Process TDD strict : RED (tests d'abord) → validation utilisateur → GREEN (implementation).
Regles detaillees dans `.claude/CLAUDE.md`.
Architecture detaillee dans `ARCHITECTURE_SMS_VOIX.md`.

## Ce qui existe deja

- **SmsQueue** : modele dans `models.py` (id, to_number, body, created_at, sent_at, error, retries)
- **`_enqueue_sms_for_user()`** : dans `escalation.py` (lignes 44-64), guard anti-doublon, enqueue au moment de l'escalade
- **Endpoints SMS internes** : dans `api/sms.py`, securises par `X-Gateway-Key`
  - `GET /internal/sms/pending` → liste les SMS non envoyes
  - `POST /internal/sms/{id}/sent` → marque un SMS comme envoye
  - `POST /internal/sms/{id}/error` → marque un SMS en erreur
- **Gateway actuel** : `gateway/sms_gateway.py` utilise gammu (subprocess) pour envoyer les SMS via cle USB GSM. Poll VPS1 puis VPS2 en fallback. **A REMPLACER**.
- **Health monitor** : `gateway/health_monitor.py` utilise gammu + Asterisk call files. **A REMPLACER**.
- **Config gateway** : `gateway/config.py` — variables VPS1_URL, VPS2_URL, GATEWAY_KEY, GAMMU_CONFIG, etc.
- **AlarmNotification** : table de liaison alarm_id/user_id (migration existante `_migrate_alarm_notifications`)
- **7 tests SMS** dans `tests/test_e2e.py` classe `TestSmsAndHealth` (lignes ~1545-1730)
- **3 tests redundancy SMS** dans `tests/test_e2e.py`
- **Escalade cumulative** : tous les utilisateurs notifies continuent de sonner, n'importe qui peut acquitter
- **Horloge injectable** : `clock.py` + endpoints `POST /test/advance-clock`, `POST /test/reset-clock`
- **FCM** : deja implemente (`fcm_service.py`, `send_fcm_to_user`), envoye a l'escalade
- **Leader election** : `leader_election.py`, seul le primary Patroni execute la boucle d'escalade

## Hardware cible

Waveshare SIM7600E-H 4G HAT connecte en USB au noeud on-site (NODE1).
- `/dev/sim7600_at` (symlink udev) → port AT commands (SMS, appels, TTS)
- `/dev/sim7600_audio` (symlink udev) → port audio USB (PCM 8kHz 16-bit mono, pour decodage DTMF)
- SIM Free Mobile 2€/mois (appels + SMS illimites)
- Le hardware n'est PAS encore disponible — tout doit etre testable avec des mocks

## Objectif

Implementer le circuit complet SMS + appels vocaux + acquittement multi-canal en TDD (RED puis GREEN).

---

## CHANTIER 1 — Backend : modele CallQueue + timer SMS/Appel par utilisateur

### Nouveau modele `CallQueue` dans `models.py`

Meme pattern que `SmsQueue` :
```python
class CallQueue(Base):
    __tablename__ = "call_queue"
    id = Column(Integer, primary_key=True)
    to_number = Column(String, nullable=False)
    alarm_id = Column(Integer, ForeignKey("alarms.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    tts_message = Column(String, nullable=False)    # texte a synthetiser
    created_at = Column(DateTime, default=func.now())
    called_at = Column(DateTime, nullable=True)       # quand l'appel a ete passe
    result = Column(String, nullable=True)            # "ack_dtmf", "ack_sms", "no_answer", "busy", "error"
    retries = Column(Integer, default=0)
```

Migration idempotente dans `database.py` (meme pattern que `_migrate_alarm_notifications`).

### Enrichir `AlarmNotification`

Ajouter 3 colonnes (migration idempotente) :
- `notified_at` : DateTime, default now — timestamp du moment ou l'utilisateur a ete ajoute aux notifies
- `sms_sent` : Boolean, default False — SMS deja enqueue pour cet utilisateur sur cette alarme
- `call_sent` : Boolean, default False — appel deja enqueue

### Nouveau parametre `SystemConfig`

- Cle : `sms_call_delay_minutes`, valeur par defaut : `"2"`
- Editable depuis le front (meme pattern que `escalation_delay_minutes`)

### Modifier `escalation.py`

**1) Alimenter `notified_at` dans `_add_notified_user()` :**
```python
def _add_notified_user(db, alarm, user_id):
    existing = ...
    if not existing:
        db.add(AlarmNotification(
            alarm_id=alarm.id,
            user_id=user_id,
            notified_at=clock_now()  # NOUVEAU
        ))
```

**2) RETIRER `_enqueue_sms_for_user()` du bloc escalade** (ligne 176 actuelle). Le SMS n'est plus lie a l'escalade.

**3) NOUVEAU BLOC — entre le bloc escalade et le bloc on-call :**
```
# --- 2b. SMS + Call timer: enqueue pour les utilisateurs notifies ---
sms_call_delay_config = db.query(SystemConfig).filter(
    SystemConfig.key == "sms_call_delay_minutes"
).first()
sms_call_delay = float(sms_call_delay_config.value) if sms_call_delay_config else 2.0

for alarm in active_alarms:
    notifs = db.query(AlarmNotification).filter(
        AlarmNotification.alarm_id == alarm.id
    ).all()
    for notif in notifs:
        elapsed_since_notif = (now - notif.notified_at).total_seconds() / 60.0
        if elapsed_since_notif >= sms_call_delay:
            user = db.query(User).filter(User.id == notif.user_id).first()
            if user:
                if not notif.sms_sent:
                    _enqueue_sms_for_user(db, user, alarm)
                    notif.sms_sent = True
                if not notif.call_sent:
                    _enqueue_call_for_user(db, user, alarm)
                    notif.call_sent = True
    db.commit()
```

**4) Nouvelle fonction `_enqueue_call_for_user()` :**
Meme pattern que `_enqueue_sms_for_user` — guard anti-doublon, verifie phone_number present.
```python
def _enqueue_call_for_user(db, user, alarm):
    if not user.phone_number:
        return
    existing = db.query(CallQueue).filter(
        CallQueue.alarm_id == alarm.id,
        CallQueue.user_id == user.id,
        CallQueue.called_at == None,
        CallQueue.retries < 3,
    ).first()
    if existing:
        return
    tts = f"Alarme {alarm.severity} sur le site. {alarm.title}. Appuyez sur 1 pour acquitter. Appuyez sur 2 pour escalader."
    call = CallQueue(
        to_number=user.phone_number,
        alarm_id=alarm.id,
        user_id=user.id,
        tts_message=tts,
    )
    db.add(call)
```

### Nouveaux endpoints dans `api/sms.py` (ou nouveau fichier `api/calls.py`)

Securises par `X-Gateway-Key` (meme pattern que les endpoints SMS) :
- `GET /internal/calls/pending` → liste les appels non passes (called_at == None, retries < 3)
- `POST /internal/calls/{id}/result` → body `{"result": "ack_dtmf"|"ack_sms"|"no_answer"|"busy"|"error"}`
  - Si result == "ack_dtmf" ou "ack_sms" : acquitter l'alarme en DB (meme logique que `POST /alarms/{id}/acknowledge`)
- `POST /internal/alarms/{alarm_id}/ack-by-phone` → body `{"phone_number": "+336..."}` — acquittement par numero de telephone (pour ack par reponse SMS, le gateway ne connait que le numero)
  - Match phone_number → user → alarme active → acquitter

---

## CHANTIER 2 — Gateway `modem_gateway.py` (NOUVEAU, remplace gammu)

Fichier : `gateway/modem_gateway.py`

### Architecture du gateway

```
ModemGateway (process principal)
├── SerialConnection       : gere pyserial vers /dev/sim7600_at
├── SmsSenderThread        : poll /internal/sms/pending, envoie via AT+CMGS
├── CallSenderThread       : poll /internal/calls/pending, appelle via ATD, TTS, DTMF
├── SmsReceiverThread      : ecoute +CMTI, lit SMS entrants, acquitte si "1"/"OK"
├── DtmfDecoderThread      : lit /dev/sim7600_audio pendant un appel, Goertzel
└── HealthMonitorThread    : poll /health sur les 3 noeuds, alerte SMS si tous KO
```

**IMPORTANT** : le port AT serie est partage entre les threads. Utiliser un `threading.Lock` pour serialiser les commandes AT. Un seul thread ecrit/lit le port AT a la fois.

### Sequence SMS sortant (`SmsSenderThread`)

```python
def send_sms(self, to_number: str, body: str) -> bool:
    with self.at_lock:
        self.serial.write(b'AT+CMGF=1\r')
        self._wait_ok()
        self.serial.write(f'AT+CMGS="{to_number}"\r'.encode())
        time.sleep(0.5)  # attendre le prompt ">"
        self.serial.write(body.encode() + b'\x1A')
        response = self._wait_response(timeout=30)
        return "OK" in response
```

### Sequence appel vocal (`CallSenderThread`)

```python
def make_call(self, call: dict) -> str:
    # 1. Appeler
    with self.at_lock:
        self.serial.write(f'ATD{call["to_number"]};\r'.encode())

    # 2. Attendre decrochage (polling AT+CLCC)
    if not self._wait_answer(timeout=30):
        with self.at_lock:
            self.serial.write(b'ATH\r')
        return "no_answer"

    # 3. Jouer TTS
    with self.at_lock:
        tts_cmd = f'AT+CTTS=2,"{call["tts_message"]}"\r'
        self.serial.write(tts_cmd.encode())

    # 4. Ecouter DTMF (via DtmfDecoderThread sur le port audio)
    self.dtmf_decoder.start_listening()
    key = self.dtmf_decoder.wait_for_key(timeout=30)
    self.dtmf_decoder.stop_listening()

    # 5. Raccrocher
    with self.at_lock:
        self.serial.write(b'ATH\r')

    if key == "1":
        return "ack_dtmf"
    elif key == "2":
        return "escalate"
    else:
        return "no_answer"
```

### Sequence ecoute SMS entrants (`SmsReceiverThread`)

```python
def run(self):
    # Activer notifications
    with self.at_lock:
        self.serial.write(b'AT+CNMI=2,1,0,0,0\r')
        self._wait_ok()

    while self.running:
        # Lire les donnees non sollicitees du port AT
        line = self._read_line_nonblocking()
        if line and "+CMTI:" in line:
            index = self._extract_index(line)  # "+CMTI: "SM",3" → 3
            sms = self._read_sms(index)        # AT+CMGR=3
            self._process_incoming_sms(sms)
            self._delete_sms(index)            # AT+CMGD=3

def _process_incoming_sms(self, sms):
    sender = sms["sender"]     # numero E.164
    body = sms["body"].strip().lower()
    if body in ("1", "ok", "oui", "ack"):
        # Acquitter via le backend
        self._ack_by_phone(sender)

def _ack_by_phone(self, phone_number):
    # POST /internal/alarms/active/ack-by-phone
    requests.post(
        f"{self.backend_url}/internal/alarms/active/ack-by-phone",
        json={"phone_number": phone_number},
        headers={"X-Gateway-Key": self.gateway_key},
    )
```

### Contrainte CSFB

Pendant un appel vocal (CSFB en 2G/3G), les SMS entrants peuvent etre retardes par le reseau. Le `SmsReceiverThread` les traitera des la fin de l'appel. Si l'utilisateur a deja acquitte par DTMF pendant l'appel, le SMS de reponse eventuel sera ignore (alarme deja acquittee en DB → endpoint retourne "deja acquittee").

### Mutex sur le port AT

Le `SmsReceiverThread` lit aussi le port AT (pour les +CMTI). Pendant un appel, le `CallSenderThread` utilise le port. **Solution** : le `SmsReceiverThread` ne lit que quand le `at_lock` est libre (tryLock). Pendant un appel, il ne tente pas de lire — les SMS entrants s'accumulent en memoire SIM et seront lus apres le raccroché.

---

## CHANTIER 3 — Decodeur DTMF Python

Fichier : `gateway/dtmf_decoder.py`

### Algorithme de Goertzel

Le SIM7600 ne supporte PAS AT+DDET (detection DTMF hardware). Le workaround est de lire le flux audio USB (`/dev/sim7600_audio`, PCM 8kHz 16-bit mono) et decoder les tonalites DTMF en logiciel.

Frequences DTMF :
```
         1209 Hz  1336 Hz  1477 Hz
697 Hz     1        2        3
770 Hz     4        5        6
852 Hz     7        8        9
941 Hz     *        0        #
```

Chaque touche = une paire (frequence basse + frequence haute) simultanee.

### Implementation

```python
import numpy as np
import serial
import threading

DTMF_FREQS_LOW = [697, 770, 852, 941]
DTMF_FREQS_HIGH = [1209, 1336, 1477]
DTMF_MAP = {
    (697, 1209): '1', (697, 1336): '2', (697, 1477): '3',
    (770, 1209): '4', (770, 1336): '5', (770, 1477): '6',
    (852, 1209): '7', (852, 1336): '8', (852, 1477): '9',
    (941, 1209): '*', (941, 1336): '0', (941, 1477): '#',
}
SAMPLE_RATE = 8000
BLOCK_SIZE = 205  # ~25ms a 8kHz — bonne resolution pour DTMF (duree min 40ms)

def goertzel_mag(samples, freq, sample_rate):
    """Retourne la magnitude de Goertzel pour une frequence donnee."""
    N = len(samples)
    k = int(0.5 + N * freq / sample_rate)
    w = 2.0 * np.pi * k / N
    coeff = 2.0 * np.cos(w)
    s0 = s1 = s2 = 0.0
    for sample in samples:
        s0 = sample + coeff * s1 - s2
        s2 = s1
        s1 = s0
    return np.sqrt(s1*s1 + s2*s2 - coeff*s1*s2)

def detect_dtmf(samples):
    """Detecte une touche DTMF dans un bloc de samples PCM 8kHz."""
    mags_low = [(f, goertzel_mag(samples, f, SAMPLE_RATE)) for f in DTMF_FREQS_LOW]
    mags_high = [(f, goertzel_mag(samples, f, SAMPLE_RATE)) for f in DTMF_FREQS_HIGH]
    best_low = max(mags_low, key=lambda x: x[1])
    best_high = max(mags_high, key=lambda x: x[1])
    THRESHOLD = 1000  # a calibrer avec le hardware reel
    if best_low[1] > THRESHOLD and best_high[1] > THRESHOLD:
        return DTMF_MAP.get((best_low[0], best_high[0]))
    return None
```

### Anti-rebond

Une touche DTMF dure typiquement 100-500ms. Le decodeur doit :
- Detecter la meme touche sur au moins 3 blocs consecutifs (~75ms) avant de valider
- Ignorer les repetitions tant que la touche est maintenue (un evenement par appui)
- Reset quand aucune touche n'est detectee pendant 2 blocs

### Testable en isolation

Le decodeur doit etre testable SANS hardware :
- Generer des fichiers WAV avec des tonalites DTMF connues (numpy.sin)
- Lire le WAV comme si c'etait le flux du port audio
- Verifier la detection correcte de chaque touche 0-9, *, #
- Tester l'anti-rebond avec des tonalites longues et des doubles appuis

---

## CHANTIER 4 — Tests E2E

### Process TDD

1. Ecrire TOUS les tests RED d'abord
2. Attendre validation utilisateur
3. Implementer le code GREEN

### Tests backend — fichier `tests/test_sms_voice.py` (NOUVEAU)

Le gateway utilise du vrai hardware (pas disponible en CI). Les tests backend testent uniquement la logique cote backend (enqueue, timers, endpoints). Le gateway sera teste manuellement avec le hardware.

```
CLASS: TestSmsCallTimer
  test_sms_not_enqueued_before_delay
    - Creer alarme, avancer horloge de 1min (< delai 2min)
    - Verifier: aucun SMS dans SmsQueue pour user1
    - Verifier: aucun appel dans CallQueue pour user1

  test_sms_and_call_enqueued_after_delay
    - Creer alarme, avancer horloge de 3min (> delai 2min)
    - Verifier: 1 SMS dans SmsQueue pour user1
    - Verifier: 1 appel dans CallQueue pour user1

  test_sms_call_delay_configurable
    - Modifier sms_call_delay_minutes a 5 via POST /config/
    - Creer alarme, avancer horloge de 3min
    - Verifier: aucun SMS (3min < 5min)
    - Avancer de 3min supplementaires (total 6min)
    - Verifier: SMS present

  test_sms_not_enqueued_if_already_acked
    - Creer alarme, acquitter immediatement, avancer horloge de 3min
    - Verifier: aucun SMS dans SmsQueue (alarme acquittee)

  test_sms_call_per_user_timer
    - Creer alarme (user1 notifie a t+0)
    - Avancer horloge de 16min → escalade vers user2 (notifie a t+15min)
    - Verifier: SMS pour user1 present (notifie depuis 16min > 2min)
    - Avancer horloge de 1min supplementaire (total 17min, user2 notifie depuis 2min)
    - Verifier: SMS pour user2 present

  test_no_duplicate_sms_on_multiple_ticks
    - Creer alarme, avancer horloge de 3min, trigger 3 ticks d'escalade
    - Verifier: exactement 1 SMS dans SmsQueue pour user1 (pas de doublon)

  test_no_duplicate_call_on_multiple_ticks
    - Meme chose pour CallQueue

CLASS: TestCallQueueEndpoints
  test_get_pending_calls
    - Enqueuer un appel manuellement en DB
    - GET /internal/calls/pending → 1 appel retourne

  test_get_pending_calls_requires_gateway_key
    - GET /internal/calls/pending sans header X-Gateway-Key → 403

  test_post_call_result_ack_dtmf
    - Enqueuer un appel, creer une alarme active
    - POST /internal/calls/{id}/result {"result": "ack_dtmf"}
    - Verifier: appel marque, alarme acquittee en DB

  test_post_call_result_no_answer
    - POST /internal/calls/{id}/result {"result": "no_answer"}
    - Verifier: appel marque, alarme toujours active

  test_post_call_result_escalate
    - POST /internal/calls/{id}/result {"result": "escalate"}
    - Verifier: escalade forcee vers l'utilisateur suivant

CLASS: TestAckByPhone
  test_ack_by_phone_known_number
    - User1 a phone_number "+33600000001", alarme active assignee a user1
    - POST /internal/alarms/active/ack-by-phone {"phone_number": "+33600000001"}
    - Verifier: alarme acquittee, acknowledged_by == user1.name

  test_ack_by_phone_unknown_number
    - POST /internal/alarms/active/ack-by-phone {"phone_number": "+33699999999"}
    - Verifier: 404 (numero inconnu)

  test_ack_by_phone_no_active_alarm
    - Aucune alarme active
    - POST /internal/alarms/active/ack-by-phone {"phone_number": "+33600000001"}
    - Verifier: 404 (pas d'alarme active)

  test_ack_by_phone_already_acked
    - Alarme deja acquittee
    - POST /internal/alarms/active/ack-by-phone {"phone_number": "+33600000001"}
    - Verifier: 409 ou 200 (idempotent)

CLASS: TestSmsCallEscalationIntegration
  test_full_escalation_with_sms_call
    - Creer alarme
    - Avancer 3min → user1 recoit SMS + appel
    - Avancer 15min → escalade vers user2 + push FCM
    - Avancer 2min → user2 recoit SMS + appel
    - Verifier toute la sequence en DB

  test_ack_stops_all_sms_calls
    - Creer alarme, avancer 3min (SMS + appel enqueues)
    - Acquitter l'alarme
    - Verifier: pas de nouveau SMS/appel enqueue aux ticks suivants

  test_sms_call_after_ack_expiry
    - Creer alarme, acquitter, avancer 31min (ack expire)
    - L'alarme redevient active
    - Les flags sms_sent/call_sent sont reset → SMS + appel re-enqueues apres le delai
```

### Tests decodeur DTMF — fichier `tests/test_dtmf_decoder.py` (NOUVEAU)

```
CLASS: TestGoertzel
  test_detect_each_digit
    - Generer un signal PCM synthetique pour chaque touche (0-9, *, #)
    - Verifier que detect_dtmf() retourne la bonne touche

  test_no_detection_on_silence
    - Buffer de zeros → detect_dtmf() retourne None

  test_no_detection_on_noise
    - Buffer de bruit aleatoire → detect_dtmf() retourne None

  test_no_detection_on_single_freq
    - Signal avec une seule frequence (pas une paire DTMF) → None

  test_anti_bounce_single_event
    - 10 blocs consecutifs avec la meme touche → un seul evenement

  test_two_consecutive_keys
    - Touche "1" pendant 5 blocs, silence 2 blocs, touche "2" pendant 5 blocs
    - Verifier: 2 evenements ["1", "2"]
```

### Tests existants a adapter

Les 7 tests de `TestSmsAndHealth` dans `test_e2e.py` verifient l'enqueue SMS a l'escalade. Apres la modification, le SMS n'est plus enqueue a l'escalade mais apres le delai `sms_call_delay_minutes`. **Adapter ces tests** :
- Ajouter un `advance-clock` de 3min apres l'escalade pour declencher les SMS
- Ou modifier les assertions pour verifier le nouveau timing

---

## CHANTIER 5 — Nettoyage + config front

### Nettoyage gateway

- Remplacer `gateway/sms_gateway.py` par `gateway/modem_gateway.py`
- Remplacer `gateway/health_monitor.py` — le health monitor est integre dans `modem_gateway.py` (thread `HealthMonitorThread`)
- Adapter `gateway/config.py` :
  - Renommer VPS1_URL/VPS2_URL → NODE1_URL/NODE2_URL/NODE3_URL
  - Retirer GAMMU_CONFIG
  - Ajouter MODEM_AT_PORT (default `/dev/sim7600_at`)
  - Ajouter MODEM_AUDIO_PORT (default `/dev/sim7600_audio`)
  - Ajouter MODEM_BAUD_RATE (default 115200)

### Config front

Ajouter `sms_call_delay_minutes` dans l'interface de configuration existante (meme pattern que `escalation_delay_minutes`). Le front a deja une section config — ajouter un champ "Delai SMS/Appel (minutes)" editable.

### Documentation

- `IMPROVEMENTS.md` point #18 → marquer DONE
- `SITE_SECURITY_NOTES.md` — deja mis a jour
- `ARCHITECTURE_SMS_VOIX.md` — deja a jour

---

## Contraintes techniques

- **TDD** : ecrire TOUS les tests RED d'abord, attendre validation, puis GREEN
- **Pas de Thread.sleep** dans les tests backend : utiliser l'horloge injectable
- **Pas de vrai hardware** dans les tests : mocker le modem / tester uniquement la logique backend
- **Decodeur DTMF testable** en isolation avec des signaux synthetiques (numpy.sin)
- **Migration DB** idempotente pour CallQueue et AlarmNotification (meme pattern que les migrations existantes)
- **Gateway non containerise** : le gateway tourne en natif sur NODE1 (acces direct aux ports USB). Pas dans Docker.
- **Guard anti-doublon** sur tous les enqueue (SMS + Call) — ne jamais creer de doublon pour la meme alarme/utilisateur
- **Acquittement idempotent** : si l'alarme est deja acquittee, les endpoints d'ack retournent OK sans erreur

## Separation des responsabilites

| Couche | Role | Qui decide |
|--------|------|-----------|
| `escalation.py` (backend) | Enqueue SMS + Call quand le timer per-user expire | Backend (tick 10s) |
| `modem_gateway.py` (gateway) | Poll les queues, envoie SMS, passe appels, ecoute DTMF/SMS entrants | Gateway (process natif NODE1) |
| `dtmf_decoder.py` (gateway) | Decode les touches DTMF depuis le flux audio USB | Appele par CallSenderThread |
| Endpoints `/internal/*` (backend) | Interface entre gateway et backend (poll + ack) | API securisee X-Gateway-Key |

## Ordre d'implementation recommande

1. Tests RED `tests/test_sms_voice.py` (backend : timer, CallQueue, endpoints)
2. Tests RED `tests/test_dtmf_decoder.py` (decodeur Goertzel)
3. → PAUSE : validation utilisateur des tests
4. GREEN : modele CallQueue + migration
5. GREEN : enrichir AlarmNotification (notified_at, sms_sent, call_sent) + migration
6. GREEN : nouveau bloc dans escalation.py (timer per-user)
7. GREEN : endpoints /internal/calls/* + /internal/alarms/active/ack-by-phone
8. GREEN : config front sms_call_delay_minutes
9. GREEN : `gateway/dtmf_decoder.py`
10. GREEN : `gateway/modem_gateway.py` (threads SMS/Call/Receiver/Health)
11. Adapter les 7 tests SMS existants
12. Run all tests
