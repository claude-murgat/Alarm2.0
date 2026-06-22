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
    hôte : une gateway dont le µC s'est tu doit cesser de reporter, pour que
    le backend la sorte de `alive_gateways` au lieu de propager un état périmé.

    Retourne :
    - `None` si jamais reçu de sample (`latest_raw is None`) → pas de POST
    - `None` si dernier sample plus vieux que `liveness` (µC muet) → pas de POST
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
