"""Policy d'exécution des commandes du bot SRE.

Chaque commande passée au bot est matchée contre une liste de regex classées en
niveaux de risque. Hors-allowlist → refus immédiat + escalade humaine.

L1 = lecture seule (logs, status, query SELECT, health)
L2 = restart "safe" (process avec auto-restart, gateway custom, scope local)
L3 = action à blast radius élevé mais réversible (reinit replica, switchover)
L4 = tout le reste = REFUS

Les commandes sont préfixées par leur cible : LOCAL ou un host SSH parmi
{node3, onsite-1, onsite-2}. Le préfixe est ajouté côté executor.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional


class Level(IntEnum):
    L1 = 1   # lecture seule
    L2 = 2   # restart safe / kill orphelin
    L3 = 3   # action prod réversible (reinit, switchover)
    L4 = 4   # hors allowlist - refus


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern
    level: Level
    description: str


# ── L1 : LECTURE SEULE ───────────────────────────────────────────────────────
_L1_PATTERNS = [
    (r"^docker (ps|stats|inspect|version|images|network ls|network inspect)\b",
     "Docker introspection"),
    (r"^docker logs( --tail \d+| --since \S+| -f)?\s+\S+$",
     "Docker logs"),
    # docker exec — décomposé pour éviter de masquer L3 (reinit, POST, etc.)
    (r"^docker exec \S+ psql .* -c \"SELECT [^\"]*\"\s*$",
     "docker exec — psql SELECT only"),
    (r"^docker exec \S+ patronictl( -c \S+)? (list|topology|show-config|history|version)\b",
     "docker exec — patronictl read-only"),
    # curl sans -X POST/PUT/DELETE/PATCH (autorisé en L1 ; sinon → L3 ou L4)
    (r"^docker exec \S+ curl (?!.*-X (POST|PUT|DELETE|PATCH))\S.*$",
     "docker exec — curl GET-only"),
    (r"^docker exec \S+ (ls|cat|head|tail|echo|grep|ps|env|date|hostname|wc|find) ",
     "docker exec — outil read-only"),
    (r"^docker exec \S+ etcdctl (--endpoints=\S+ )?(get|member list|endpoint health)\b",
     "etcdctl read-only"),
    (r"^systemctl (status|is-active|is-enabled|list-units|cat)\b",
     "Systemd introspection"),
    (r"^journalctl ",
     "Journal logs"),
    (r"^(ps|pgrep|ls|cat|head|tail|stat|file|du|df|free|uptime|date|hostname|uname|lsof|fuser|lsusb|lspci|dmesg|mount|wc|find|grep|env|chronyc tracking|chronyc sources|wg show|wdctl) ",
     "Outils introspection système"),
    (r"^(ps|pgrep|ls|date|hostname|uname|free|uptime|df|du|chronyc tracking|wg show|wdctl)$",
     "Outils introspection système (sans arg)"),
    (r"^curl -sS (-m \d+ )?(-i )?(-o /dev/null )?(-w \S+ )?https?://\S+(/[a-zA-Z0-9_\-/]*)?$",
     "HTTP GET — health/status check"),
    (r"^psql -h \S+ -U \S+ -d \S+ -c \"SELECT ",
     "Postgres SELECT"),
    (r"^cat /proc/\d+/(cmdline|status|stat|maps)$",
     "Lecture /proc info process"),
    (r"^ls -la? /proc/\d+/fd/?$",
     "Liste FDs d'un process"),
    (r"^ping -c \d+ (-W \d+ )?\S+$",
     "Ping"),
]

# ── L2 : RESTART SAFE / KILL ORPHELIN ────────────────────────────────────────
_L2_PATTERNS = [
    (r"^kill( -(15|TERM))?\s+\d+$",
     "Kill SIGTERM d'un PID"),
    (r"^docker restart [a-zA-Z0-9_\-]+$",
     "Restart container Docker (a Restart=always en général)"),
    (r"^systemctl restart (alarm-modem-gateway|alarm-sms-gateway|alarm-cd-pull)\.service$",
     "Restart d'un service alarm-* spécifique"),
    # Relance modem_gateway.py par nohup (cas onsite-2 sans service systemd)
    (r"^cd /opt/alarm/gateway && nohup env (DRY_CONTACT_ENABLED=\S+ |GATEWAY_ID=\S+ |GATEWAY_KEY=\S+ |NODE\d_URL=\S+ )+python3 modem_gateway\.py > /tmp/gw-\S+\.log 2>&1 &$",
     "Relance modem_gateway.py via nohup (workaround tant que pas de systemd unit)"),
]

# ── L3 : ACTION PROD RÉVERSIBLE ──────────────────────────────────────────────
_L3_PATTERNS = [
    (r"^docker exec \S+ patronictl (-c \S+ )?reinit \S+ \S+ --force$",
     "Patroni reinit d'un node (réversible : le replica resync depuis leader)"),
    (r"^docker exec \S+ patronictl (-c \S+ )?switchover (\S+ )?--candidate \S+ --force$",
     "Patroni switchover (réversible par switchover inverse)"),
    (r"^kill -9 \d+$",
     "SIGKILL d'un PID (plus violent que SIGTERM mais réversible par restart)"),
    # POST direct sur l'API Patroni quand patronictl ne route pas
    (r"^docker exec \S+ curl -sS -m \d+ -i -X POST -H \"Content-Type: application/json\" -d \"\{\\\"force\\\":true\\}\" http://localhost:8008/reinitialize$",
     "Patroni reinit via POST direct API (fallback)"),
]


def _compile(patterns: list[tuple[str, str]], level: Level) -> list[Rule]:
    return [
        Rule(re.compile(pat), level, desc)
        for pat, desc in patterns
    ]


RULES: list[Rule] = (
    _compile(_L1_PATTERNS, Level.L1)
    + _compile(_L2_PATTERNS, Level.L2)
    + _compile(_L3_PATTERNS, Level.L3)
)


@dataclass
class PolicyDecision:
    level: Level
    matched_rule_description: Optional[str]  # None si L4


def classify(command: str) -> PolicyDecision:
    """Retourne le niveau d'une commande shell. L4 = refus."""
    cmd = command.strip()
    for rule in RULES:
        if rule.pattern.match(cmd):
            return PolicyDecision(rule.level, rule.description)
    return PolicyDecision(Level.L4, None)


# ── Hosts SSH autorisés ──────────────────────────────────────────────────────
# Le bot ne peut SSH que vers ces hosts. La clé est choisie par l'executor.
ALLOWED_HOSTS = {
    "node3": {"user": "alarm", "host": "51.210.105.102", "port": 50922,
              "key_env": "SSH_KEY_NODE3"},
    "onsite-1": {"user": "alarm", "host": "172.16.1.121", "port": 22,
                 "key_env": "SSH_KEY_ONSITE_1"},
    "onsite-2": {"user": "alarm", "host": "172.16.1.120", "port": 22,
                 "key_env": "SSH_KEY_ONSITE_2"},
    "LOCAL": None,  # exécution sur la machine du bot (pas de SSH)
}
