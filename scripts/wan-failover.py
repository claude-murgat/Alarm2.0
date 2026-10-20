#!/usr/bin/env python3
"""/opt/alarm/wan-failover.py — bascule box -> 4G quand la box est UP mais sans internet (INV-140).

La route vers la box (statique, metrique 0) ne disparait que si le cable est coupe :
box sans internet, la route 4G (metrique 300, alarm-4g-standby) ne servait jamais.
Toutes les INTERVAL_S, on ping par l'interface de la box ; apres FAILS_TO_SWITCH echecs
(et si la 4G repond) la route box passe en DEMOTED_METRIC ; apres OKS_TO_RESTORE succes
elle reprend la metrique 0. Ne fait rien sans route 4G (onsite-1). Stdlib uniquement.
"""
import re
import signal
import subprocess
import sys
import time

INTERVAL_S = 10
FAILS_TO_SWITCH = 3
OKS_TO_RESTORE = 3
DEMOTED_METRIC = 500
TARGETS = ("51.210.105.102", "1.1.1.1", "9.9.9.9")  # cloud OVH, Cloudflare, Quad9

_ROUTE = re.compile(r"^default via (\S+) dev (\S+)(?:.*?\bmetric (\d+))?")


def log(m):
    print(f"[wan-failover] {m}", flush=True)


def parse_default_routes(text: str) -> list[dict]:
    """Lignes `ip route show default` -> [{gw, dev, metric}] (metrique absente = 0)."""
    routes = []
    for line in text.splitlines():
        m = _ROUTE.match(line.strip())
        if m:
            routes.append({"gw": m.group(1), "dev": m.group(2), "metric": int(m.group(3) or 0)})
    return routes


def box_and_cell(routes: list[dict]):
    """Route box (premiere hors ww*) et route 4G (dev ww*), None si absente."""
    box = next((r for r in routes if not r["dev"].startswith("ww")), None)
    cell = next((r for r in routes if r["dev"].startswith("ww")), None)
    return box, cell


def count(ok: bool, fails: int, oks: int) -> tuple[int, int]:
    """Compteurs consecutifs (echecs, succes) apres une sonde."""
    return (0, oks + 1) if ok else (fails + 1, 0)


def decide(demoted: bool, box_fails: int, box_oks: int, cell_ok: bool):
    """'demote' | 'restore' | None. Jamais de bascule vers une 4G qui ne repond pas."""
    if not demoted and box_fails >= FAILS_TO_SWITCH and cell_ok:
        return "demote"
    if demoted and box_oks >= OKS_TO_RESTORE:
        return "restore"
    return None


def route_commands(action: str, box: dict) -> list[list[str]]:
    """Ajoute la nouvelle route avant de retirer l'ancienne : jamais sans route box."""
    new, old = (DEMOTED_METRIC, 0) if action == "demote" else (0, DEMOTED_METRIC)
    base = ["default", "via", box["gw"], "dev", box["dev"]]
    return [
        ["ip", "route", "replace", *base, "metric", str(new), "onlink"],
        ["ip", "route", "del", *base, "metric", str(old)],
    ]


# ── E/S (validees sur materiel, pas en tier 1) ──

def routes_now():
    out = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True).stdout
    return box_and_cell(parse_default_routes(out))


def reachable(dev: str) -> bool:
    return any(subprocess.run(["ping", "-c", "1", "-W", "2", "-I", dev, t],
                              capture_output=True).returncode == 0 for t in TARGETS)


def apply(action: str, box: dict):
    for cmd in route_commands(action, box):
        subprocess.run(cmd, capture_output=True)
    log(f"{action} : box {box['gw']} ({box['dev']}) -> metrique {DEMOTED_METRIC if action == 'demote' else 0}")


def restore_if_demoted():
    box, _ = routes_now()
    if box and box["metric"] == DEMOTED_METRIC:
        apply("restore", box)


def main():
    def stop(*_):
        restore_if_demoted()  # ne jamais laisser la box retrogradee derriere nous
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    restore_if_demoted()  # reliquat d'une execution precedente
    fails = oks = 0
    log(f"demarre : sonde toutes les {INTERVAL_S}s vers {', '.join(TARGETS)}")
    while True:
        box, cell = routes_now()
        if box and cell:
            fails, oks = count(reachable(box["dev"]), fails, oks)
            demoted = box["metric"] == DEMOTED_METRIC
            cell_ok = reachable(cell["dev"]) if (fails >= FAILS_TO_SWITCH and not demoted) else True
            action = decide(demoted, fails, oks, cell_ok)
            if action:
                apply(action, box)
                fails = oks = 0
            elif fails == FAILS_TO_SWITCH and not cell_ok:
                log("box sans internet mais 4G muette : pas de bascule")
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main()
