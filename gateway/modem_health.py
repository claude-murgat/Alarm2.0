"""Logique pure de sante du lien serie gateway <-> modem SIM7600.

Aucun import : testable en tier 1 sans pyserial (cf tests/unit/test_gateway_modem_health.py).
`send_at_command` ne leve jamais d'exception et renvoie "" quand le port serie
est mort (modem re-enumere sur l'USB) : ces fonctions ne doivent donc jamais
traiter une reponse vide comme un succes.
"""

# Keepalive AT toutes les 60s : 2 echecs consecutifs ≈ 2 min sans modem avant
# de sortir (un echec isole peut venir d'un modem momentanement occupe).
MAX_AT_FAILURES = 2


# INV-183 : modem absent -> nouvelle recherche toutes les 15 s, sans sortir. Present mais muet sur
# 2 recherches de suite -> sortie (INV-126) : la 1re peut tomber pendant le demarrage du modem.
MODEM_RESCAN_SECONDS = 15
MUTE_SCANS_BEFORE_EXIT = 2


def modem_search_step(on_usb: bool, answered: bool, mute_scans: int) -> tuple[str, int]:
    """INV-183 : (action, recherches muettes consecutives) apres une recherche du modem.
    `on_usb` : un port serie SIMCom est enumere ; `answered` : l'un d'eux repond OK a AT.
    'utiliser' | 'attendre' (absent, ou muet moins de MUTE_SCANS_BEFORE_EXIT fois) | 'sortir'
    (muet MUTE_SCANS_BEFORE_EXIT fois de suite : relance systemd, comptee par le watchdog INV-146)."""
    if answered:
        return "utiliser", 0
    if not on_usb:
        return "attendre", 0
    mute_scans += 1
    return ("sortir" if mute_scans >= MUTE_SCANS_BEFORE_EXIT else "attendre"), mute_scans


def call_was_dialed(atd_response: str) -> bool:
    """Le SIM7600 repond OK a `ATD<numero>;`. Toute autre reponse, y compris
    vide, signifie que l'appel n'est jamais parti."""
    return "OK" in atd_response.splitlines()


def at_failures_after(at_response: str, failures: int) -> int:
    """Compteur d'echecs consecutifs du keepalive apres une reponse a `AT`."""
    return 0 if "OK" in at_response.splitlines() else failures + 1


# INV-128 : keepalive toutes les 60 s -> 5 controles ≈ 5 min sans reseau avant
# de couper/rallumer la radio (ce qui a debloque onsite-2 le 2026-10-13).
UNREGISTERED_CHECKS_BEFORE_RECOVERY = 5


def parse_network(creg: str, cpsi: str, csq: str) -> dict:
    """Etat reseau a partir des reponses brutes AT+CREG? / AT+CPSI? / AT+CSQ.
    CREG stat 1 = inscrit, 5 = itinerance ; tout le reste = pas de reseau."""
    stat = next((l.split(",")[-1].strip() for l in creg.splitlines() if l.startswith("+CREG:")), "")
    if stat not in ("1", "5"):
        return {"registered": False, "tech": None, "rsrp": None, "csq": 0}
    fields = next((l.split(":", 1)[1].split(",") for l in cpsi.splitlines() if l.startswith("+CPSI:")), [""])
    tech = fields[0].strip() or None
    # CPSI LTE : ...,rsrq,rsrp,rssi,rssnr ; rsrp en 1/10 dBm
    rsrp = round(int(fields[11]) / 10, 1) if tech == "LTE" and len(fields) > 11 else None
    q = next((l.split(":")[1].split(",")[0].strip() for l in csq.splitlines() if l.startswith("+CSQ:")), "99")
    return {"registered": True, "tech": tech, "rsrp": rsrp, "csq": 0 if q == "99" else int(q)}


def reregister_due(unregistered_checks: int, tried: bool) -> bool:
    """Une seule tentative de reinscription par episode sans reseau."""
    return not tried and unregistered_checks >= UNREGISTERED_CHECKS_BEFORE_RECOVERY


def sends_allowed(at_failures: int, registered: bool) -> bool:
    """INV-145 : un SMS/appel n'est tente que modem joignable ET inscrit ; sinon il
    echouerait a coup sur et consommerait un des 3 essais (INV-064)."""
    return at_failures == 0 and registered


# INV-169 : canal audio bloque -> AT+CFUN=1,1 au plus une fois par 30 min (anti-boucle : la
# gateway sort apres chaque redemarrage et systemd la relance).
PCM_REBOOT_MIN_INTERVAL_S = 30 * 60
# Le SIM7600 quitte l'USB puis revient en ~15-25 s apres AT+CFUN=1,1 : sortir avant ferait
# echouer la re-detection en boucle (relances comptees par le watchdog INV-146).
MODEM_REBOOT_WAIT_S = 30


def pcm_reboot_allowed(last_reboot_at: float | None, now: float) -> bool:
    """INV-169 : redemarrage permis si aucun dans les 30 dernieres minutes. Une trace
    dans le futur (horloge recalee en arriere) ne bloque pas."""
    return last_reboot_at is None or not 0 <= now - last_reboot_at < PCM_REBOOT_MIN_INTERVAL_S


# INV-174 : la recherche du backend primary repartait du 1er noeud de la liste a chaque requete
# (un noeud mort en tete = un timeout par requete). Le dernier primary connu est sonde d'abord.
def probe_order(cached: str | None, urls: list[str]) -> list[str]:
    """L'URL memorisee d'abord (si encore configuree), puis les autres dans l'ordre configure."""
    return sorted(urls, key=lambda u: u != cached)


def probe_timeout(url: str, cached: str | None, known: float, other: float) -> float:
    """INV-174 : le primary memorise garde la tolerance des echanges (`known`, HTTP_TIMEOUT_SECONDS) :
    un leader vivant mais lent ne fait pas sauter le poll ; les autres candidats sont sondes a `other`."""
    return known if url == cached else other
def backends_down(healths) -> bool:
    """INV-171 : aucun backend ne peut ecrire. `healths` : par noeud, (code HTTP, corps de /health) ou
    None si injoignable. /health repond 200 meme sans leader (un 503 ferait reculer le canary) :
    c'est `cluster_writable` qui compte ; absent = backend d'avant, compte inscriptible."""
    return not any(h is not None and h[0] == 200 and h[1].get("cluster_writable") is not False
                   for h in healths)


def sms_alert_step(down: bool, last_alert: float, writable_passes: int, now: float,
                   cooldown: float) -> tuple[bool, float, int]:
    """INV-171 : (SMS a envoyer, date du dernier SMS, passages inscriptibles consecutifs). SMS si aucun
    backend ne peut ecrire, hors cooldown (strict). Le cooldown n'est rearme qu'apres 2 passages
    inscriptibles d'affilee : un backend qui (re)demarre se dit inscriptible jusqu'a son 1er controle."""
    if down:
        send = now - last_alert > cooldown
        return send, now if send else last_alert, 0
    return False, 0 if writable_passes >= 1 else last_alert, writable_passes + 1
