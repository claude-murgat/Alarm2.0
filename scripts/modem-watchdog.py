#!/usr/bin/env python3
# Watchdog modem SIM7600 : detecte un drop USB, recupere auto (PCI remove/rescan
# xHCI + restart gateway/4g-standby), et envoie UN SEUL email par episode.
import os, sys, time, subprocess, smtplib, socket
from email.message import EmailMessage
WDM = '/dev/cdc-wdm0'
XHCI = os.getenv('MODEM_XHCI', '0000:00:14.0')
CHECK = int(os.getenv('WATCHDOG_CHECK_SECONDS', '60'))
GRACE = int(os.getenv('WATCHDOG_GRACE_SECONDS', '20'))
HOST = socket.gethostname()
def log(m): print(f"[modem-watchdog] {m}", flush=True)
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
_env = load_env('/opt/alarm/.env.prod.node2', '/opt/alarm/.env.prod.secrets')
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
    try: open(f'/sys/bus/pci/devices/{XHCI}/remove', 'w').write('1')
    except Exception as e: log(f"remove KO: {e}")
    time.sleep(4)
    try: open('/sys/bus/pci/rescan', 'w').write('1')
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
    log(f"demarre (check={CHECK}s xHCI={XHCI} alerte->{ALERT_TO}) — 1 email par episode")
    incident = False
    while True:
        if modem_present():
            if incident: log("modem present a nouveau — episode clos"); incident = False
            time.sleep(CHECK); continue
        time.sleep(GRACE)
        if modem_present(): continue
        if incident:
            # episode deja signale (1 email envoye) -> retente la recup EN SILENCE, pas de 2e mail
            log("toujours absent — nouvelle tentative de recup silencieuse")
            recover(); time.sleep(600); continue
        # NOUVEL episode : recup d'abord, puis UN SEUL email avec le resultat
        incident = True; ts = time.strftime('%Y-%m-%d %H:%M:%S')
        log("MODEM ABSENT confirme -> recuperation + 1 email")
        recover(); time.sleep(35)
        if modem_present():
            send_email(f"[ALARME-MURGAT] Modem SIM7600 a droppe puis RECUPERE sur {HOST}",
                       f"Le modem SIM7600 a disparu du bus USB sur {HOST} a {ts}, puis a ete RECUPERE automatiquement (PCI rescan + restart gateway).\nContact sec / SMS / voix / secours 4G de nouveau operationnels. Aucune action requise.")
            incident = False
        else:
            send_email(f"[ALARME-MURGAT] Modem SIM7600 DOWN sur {HOST} — INTERVENTION REQUISE",
                       f"Le modem SIM7600 a disparu du bus USB sur {HOST} a {ts}. La recuperation automatique (PCI rescan) a ECHOUE.\nIMPACT: contact sec, SMS, appels vocaux et secours 4G HORS SERVICE.\nIntervention physique probablement requise (power-cycle du HAT SIM7600). Le watchdog continue de retenter en silence (sans re-mailer).")
        time.sleep(CHECK)
if __name__ == '__main__': main()
