"""Logique pure du contact sec (INV-120 V2) — sans dépendance lourde.

Extrait dans son propre module pour être testable en tier 1 (n'importe que la
stdlib), comme `locks.py`. Utilisé par les deux moniteurs de `modem_gateway.py` :
- `DryContactMonitorThread` (source modem : GPIO SIM7600 via AT+CGGETV)
- `HostDryContactMonitorThread` (source hôte : microcontrôleur USB qui pousse
  des lignes `DC:<0|1>`).
"""


def parse_dc_line(line: str) -> int | None:
    """Parse une ligne du firmware contact-sec hôte.

    Le µC (cf gateway/firmware/dry_contact_r4/) émet des lignes `DC:<0|1>` :
      DC:0 → contact fermé (repos)
      DC:1 → contact ouvert / fil coupé

    Retourne 0 ou 1, ou None si la ligne n'est pas un message DC valide
    (bruit série, boot du µC, ligne partielle…)."""
    if not line:
        return None
    line = line.strip()
    if not line.startswith("DC:"):
        return None
    val = line[3:].strip()
    if val == "0":
        return 0
    if val == "1":
        return 1
    return None


def raw_to_state(raw: int, normal_value: int) -> str:
    """Mappe une valeur brute (0/1) en état métier 'closed'|'open'.

    `normal_value` = valeur brute correspondant au repos (contact fermé NC) :
    - source modem : 1 (fermé tire la GPIO à 3V3 = 1)
    - source hôte  : 0 (fermé tire la GPIO du µC à GND = 0, INPUT_PULLUP)

    Un fil coupé / contact ouvert donne l'autre valeur → 'open' → alarme
    (détection de sabotage conservée quel que soit le câblage)."""
    return "closed" if raw == normal_value else "open"


def decide_report(
    latest_raw: int | None,
    latest_ts: float,
    now: float,
    liveness: float,
    normal_value: int,
) -> str | None:
    """Décide si la gateway doit POSTer un état au backend, et lequel.

    Sous-tend INV-122 (agrégation OR fail-to-alarm multi-gateway) côté gateway
    hôte : une gateway dont le µC s'est tu ne doit pas propager un état périmé ;
    elle rapporte `no_sensor` (INV-194), hors de l'agrégation côté backend.

    Retourne :
    - `None` si jamais reçu de sample (`latest_raw is None`) → `no_sensor`
    - `None` si dernier sample plus vieux que `liveness` (µC muet) → `no_sensor`
    - sinon `raw_to_state(latest_raw, normal_value)` (`'closed'` | `'open'`)

    La borne haute de fraîcheur est inclusive (`now - latest_ts <= liveness`) :
    un sample arrivé pile en limite est considéré frais — toute autre convention
    crée des silences artificiels au backend pour les déploiements où la cadence
    µC est proche de `liveness`.
    """
    if latest_raw is None:
        return None
    if now - latest_ts > liveness:
        return None
    return raw_to_state(latest_raw, normal_value)


# INV-198 : Arduino UNO R4 Minima, application et bootloader DFU (USB VID, PID)
R4_APP_USB_ID = (0x2341, 0x0069)
R4_DFU_USB_ID = (0x2341, 0x0369)
FLASH_AFTER_SECONDS = 15.0  # muet depuis l'ouverture du port (> DRY_CONTACT_LIVENESS_SECONDS)
FLASH_RETRY_SECONDS = 30 * 60.0


def flash_due(usb_id, seen_dc: bool, open_for: float, last_attempt: float | None, now: float) -> bool:
    """INV-198 : programmer l'Arduino avec le firmware du dépôt ?

    - au plus un essai par `FLASH_RETRY_SECONDS` et par passerelle : jamais de second dfu-util sur un
      bootloader qui vient d'échouer (le 2026-11-13, les essais enchaînés l'ont bloqué) ;
    - bootloader DFU seul, sans port série (application absente ou corrompue) : oui ;
    - sinon seulement un UNO R4 (`usb_id` = (VID, PID) du port) resté sans ligne `DC:` valide pendant
      `FLASH_AFTER_SECONDS` après l'ouverture du port : un Arduino qui parle n'est jamais reprogrammé.
    """
    if last_attempt is not None and now - last_attempt < FLASH_RETRY_SECONDS:
        return False
    if usb_id == R4_DFU_USB_ID:
        return True
    return usb_id == R4_APP_USB_ID and not seen_dc and open_for >= FLASH_AFTER_SECONDS


def armed_state(state: str, serial: str | None, armed: set) -> tuple[str, bool]:
    """INV-198 : (état à rapporter, armer l'Arduino maintenant ?) pour un état lu `closed` | `open`.

    Un Arduino (numéro de série USB) n'alarme qu'après avoir lu le contact fermé une fois : branché avant le
    câblage de son contact, il lit « ouvert » et rapporte `no_sensor` (pas de contact lisible, INV-194).
    Numéro inconnu : rien à retenir d'une relance à l'autre, état rapporté tel quel (fail-to-alarm)."""
    if serial is None or serial in armed:
        return state, False
    if state == "open":
        return "no_sensor", False
    return state, True
