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
