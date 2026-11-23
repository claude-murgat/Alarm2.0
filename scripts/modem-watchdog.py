#!/usr/bin/env python3
# Watchdog modem SIM7600 : detecte un drop USB, recupere auto (PCI remove/rescan
# xHCI + restart gateway/4g-standby), et envoie UN SEUL email par episode.
# INV-146 : detecte aussi un modem PRESENT mais muet (gateway relancee en boucle) -> reset USB.
# INV-184 : n'agit que sur un modem deja vu sur cette machine, et une seule recup PCI par episode.
import os, sys, time, subprocess, smtplib, socket, glob
from email.message import EmailMessage
WDM = '/dev/cdc-wdm0'
XHCI = os.getenv('MODEM_XHCI', '0000:00:14.0')
PCI = '/sys/bus/pci'
CHECK = int(os.getenv('WATCHDOG_CHECK_SECONDS', '60'))
GRACE = int(os.getenv('WATCHDOG_GRACE_SECONDS', '20'))
HOST = socket.gethostname()
# /run : oublie au reboot, garde si le watchdog redemarre (redeploiement, crash).
SEEN_FILE = '/run/alarm-modem-seen'            # modem vu sur cette machine depuis son demarrage
RECOVERED_FILE = '/run/alarm-modem-recovered'  # recup PCI deja faite pour l'absence en cours
def log(m): print(f"[modem-watchdog] {m}", flush=True)
def absent_action(seen, recovered):
    """INV-184 : modem absent (confirme apres GRACE) -> 'ignorer' (jamais vu : machine sans modem),
    'recuperer' (1re absence de l'episode : une recup PCI + un email), 'attendre' (deja faite)."""
    if not seen: return 'ignorer'
    return 'attendre' if recovered else 'recuperer'
def touch(path):
    try: open(path, 'a').close()
    except OSError as e: log(f"{path} non inscriptible : {e}")
# INV-146 : la gateway sort quand le modem est muet (INV-126) ; 3 relances en 10 min = muet.
MUTE_RESTARTS, MUTE_WINDOW = 3, 600
def mute_detected(restart_times, now):
    return sum(1 for t in restart_times if now - t <= MUTE_WINDOW) >= MUTE_RESTARTS
def gateway_restarts():
    try:
        out = subprocess.run(['systemctl', 'show', 'alarm-sms-gateway', '-p', 'NRestarts', '--value'],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return int(out or 0)
    except Exception: return 0
def usb_reset_modem():
    """Reset USB du seul SIM7600 (authorized 0 -> 1) : a debloque onsite-2 le 2026-10-21."""
    for d in glob.glob('/sys/bus/usb/devices/*'):
        try:
            if open(f'{d}/idVendor').read().strip() != '1e0e': continue
        except OSError: continue  # interfaces USB : pas d'idVendor
        log(f"reset USB {d}")
        try:
            open(f'{d}/authorized', 'w').write('0'); time.sleep(3); open(f'{d}/authorized', 'w').write('1')
        except Exception as e: log(f"reset USB KO: {e}")
    time.sleep(25)
    subprocess.run(['systemctl', 'restart', 'alarm-modem-rat', 'alarm-sms-gateway', 'alarm-4g-standby'],
                   capture_output=True)
    log("modem-rat + gateway + 4g-standby relances")
def load_env(*paths):
    cfg = {}
    for p in paths:
        try:
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        k, v = line.split('=', 1); cfg[k.strip()] = v.strip().strip('"').strip("'")
        except OSError: pass
    return cfg
_env = load_env('/opt/alarm/.env.prod.node3', '/opt/alarm/.env.prod.secrets')  # SMTP_* identiques dans chaque .env.prod (node1/2 retires)
def cfg(k, d=None): return os.getenv(k) or _env.get(k, d)
ALERT_TO = cfg('MODEM_ALERT_EMAIL') or cfg('SMTP_FROM', 'direction_technique@charlesmurgat.com')
def modem_present():
    if not os.path.exists(WDM): return False
    try: return b'1e0e:' in subprocess.run(['lsusb'], capture_output=True, timeout=5).stdout
    except Exception: return True
def send_email(subject, body):
    h, p, user, pwd = cfg('SMTP_HOST'), cfg('SMTP_PORT', '587'), cfg('SMTP_USER'), cfg('SMTP_PASS')
    frm = cfg('SMTP_FROM') or user
    if not (h and user and pwd):
        log(f"SMTP incomplet -> email NON envoye: {subject}"); return False
    msg = EmailMessage(); msg['Subject'] = subject; msg['From'] = frm; msg['To'] = ALERT_TO; msg.set_content(body)
    try:
        with smtplib.SMTP(h, int(p), timeout=int(cfg('SMTP_TIMEOUT', '15'))) as s:
            s.starttls(); s.login(user, pwd); s.send_message(msg)
        log(f"email OK: {subject}"); return True
    except Exception as e:
        log(f"email KO: {e}"); return False
def recover():
    log("recup: PCI remove/rescan xHCI " + XHCI)
    try: open(f'{PCI}/devices/{XHCI}/remove', 'w').write('1')
    except Exception as e: log(f"remove KO: {e}")
    time.sleep(4)
    try: open(f'{PCI}/rescan', 'w').write('1')
    except Exception as e: log(f"rescan KO: {e}")
    time.sleep(8)
    subprocess.run(['systemctl', 'kill', '-s', 'KILL', 'alarm-sms-gateway'], capture_output=True)
    time.sleep(3)
    subprocess.run(['systemctl', 'restart', 'alarm-sms-gateway', 'alarm-4g-standby'], capture_output=True)
    log("gateway + 4g-standby relances")
def main():
    if '--test-email' in sys.argv:
        ok = send_email(f"[ALARME-MURGAT] Test watchdog modem ({HOST})", "Test d'alerte email — watchdog modem SIM7600 OK.")
        sys.exit(0 if ok else 1)
    log(f"demarre (check={CHECK}s xHCI={XHCI} alerte->{ALERT_TO}) — 1 recup PCI + 1 email par episode")
    # Revue PR #296 (F4) : tue entre remove et rescan, le controleur (modem ET Arduino) resterait retire.
    if not os.path.exists(f'{PCI}/devices/{XHCI}'):
        log(f"controleur {XHCI} absent au demarrage -> rescan PCI")
        try: open(f'{PCI}/rescan', 'w').write('1')
        except Exception as e: log(f"rescan KO: {e}")
    # En memoire ET dans /run : sans /run inscriptible, toujours au plus une recup par episode.
    recovered, idle_logged = os.path.exists(RECOVERED_FILE), False
    restarts, last_n, mute_mailed = [], gateway_restarts(), False
    while True:
        if modem_present():
            touch(SEEN_FILE)
            if recovered:
                log("modem present a nouveau — episode clos"); recovered = False
                try: os.remove(RECOVERED_FILE)
                except OSError: pass
            n, now = gateway_restarts(), time.time()
            if n > last_n: restarts.append(now)
            last_n = n
            if mute_detected(restarts, now):
                log("MODEM MUET (gateway relancee en boucle) -> reset USB + 1 email")
                usb_reset_modem(); restarts.clear(); last_n = gateway_restarts()
                if not mute_mailed:
                    mute_mailed = True
                    send_email(f"[ALARME-MURGAT] Modem SIM7600 muet sur {HOST} — reset USB auto",
                               f"Le modem SIM7600 etait present sur l'USB mais ne repondait plus (gateway relancee {MUTE_RESTARTS} fois en {MUTE_WINDOW // 60} min) sur {HOST}.\nReset USB du modem effectue automatiquement. Si l'alerte revient, power-cycle physique du modem.")
            elif not restarts or now - restarts[-1] > MUTE_WINDOW:
                mute_mailed = False  # episode clos : plus de relance depuis 10 min
            time.sleep(CHECK); continue
        time.sleep(GRACE)
        if modem_present(): continue
        action = absent_action(os.path.exists(SEEN_FILE), recovered)
        if action == 'ignorer' and not idle_logged:
            idle_logged = True
            log("aucun modem vu sur cette machine depuis son demarrage : rien a surveiller (INV-184)")
        elif action == 'recuperer':
            # Episode note AVANT la recup (reset PCI : Arduino du contact sec compris) : jamais deux.
            recovered = True; touch(RECOVERED_FILE); ts = time.strftime('%Y-%m-%d %H:%M:%S')
            log("MODEM ABSENT confirme -> recuperation (une seule par episode) + 1 email")
            recover(); time.sleep(35)
            if modem_present():
                send_email(f"[ALARME-MURGAT] Modem SIM7600 a droppe puis RECUPERE sur {HOST}",
                           f"Le modem SIM7600 a disparu du bus USB sur {HOST} a {ts}, puis a ete RECUPERE automatiquement (PCI rescan + restart gateway).\nContact sec / SMS / voix / secours 4G de nouveau operationnels. Aucune action requise.")
            else:
                send_email(f"[ALARME-MURGAT] Modem SIM7600 DOWN sur {HOST} — INTERVENTION REQUISE",
                           f"Le modem SIM7600 a disparu du bus USB sur {HOST} a {ts}. La recuperation automatique (reset PCI du controleur USB) a ECHOUE.\nIMPACT: SMS, appels vocaux et secours 4G de ce modem HORS SERVICE. Le reset PCI reinitialise tout le controleur USB, Arduino du contact sec compris : si le controleur n'est pas revenu, le contact sec de cette machine est coupe aussi (email « passerelle muette » sous 10 min).\nIntervention physique probablement requise (power-cycle du HAT SIM7600), ou modem volontairement retire / deplace sur une autre machine. Le watchdog attend son retour sans nouvelle tentative (INV-184).")
        time.sleep(CHECK)
if __name__ == '__main__': main()
