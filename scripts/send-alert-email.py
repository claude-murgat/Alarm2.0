#!/usr/bin/env python3
# /opt/alarm/send-alert-email.py — petit envoi d'email d'alerte reutilisable.
# Memes secrets SMTP que le watchdog modem (/opt/alarm/.env.prod.*), meme chemin
# d'envoi (STARTTLS + login). N'invente rien : c'est le send_email du watchdog
# extrait pour etre appelable depuis n'importe quel script (ex: disk-check.sh).
#
#   echo "corps du message" | /opt/alarm/send-alert-email.py "Sujet"
#   /opt/alarm/send-alert-email.py "Sujet" --to admin@example.com < corps.txt
#
# Sort 0 si l'email part, 1 si SMTP incomplet ou erreur d'envoi. stdlib only —
# aucune dependance (smtplib + email du stdlib python3).
import os, sys, glob, smtplib
from email.message import EmailMessage


def load_env(paths):
    cfg = {}
    for p in paths:
        try:
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        k, v = line.split('=', 1)
                        cfg[k.strip()] = v.strip().strip('"').strip("'")
        except OSError:
            pass
    return cfg


# Glob (et non un chemin code en dur node2) : marche sur n'importe quel noeud,
# onsite comme node3 cloud. Tri => .env.prod.secrets (suffixe alpha apres nodeN)
# est charge en dernier et a donc priorite sur les fichiers par-noeud.
_env = load_env(sorted(glob.glob('/opt/alarm/.env.prod.*')))


def cfg(k, d=None):
    return os.getenv(k) or _env.get(k, d)


def main():
    args = sys.argv[1:]
    to_override = None
    if '--to' in args:
        i = args.index('--to')
        try:
            to_override = args[i + 1]
        except IndexError:
            print("[send-alert-email] --to sans adresse", file=sys.stderr)
            return 2
        del args[i:i + 2]
    if not args:
        print("[send-alert-email] usage: send-alert-email.py 'Sujet' [--to addr] < corps",
              file=sys.stderr)
        return 2
    subject = args[0]
    body = sys.stdin.read() if not sys.stdin.isatty() else ''

    h, p = cfg('SMTP_HOST'), cfg('SMTP_PORT', '587')
    user, pwd = cfg('SMTP_USER'), cfg('SMTP_PASS')
    frm = cfg('SMTP_FROM') or user
    to = (to_override or cfg('ALERT_EMAIL') or cfg('MODEM_ALERT_EMAIL')
          or cfg('SMTP_FROM') or user)
    if not (h and user and pwd and to):
        print(f"[send-alert-email] SMTP incomplet -> email NON envoye: {subject}",
              file=sys.stderr)
        return 1

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = frm
    msg['To'] = to
    msg.set_content(body or subject)
    try:
        with smtplib.SMTP(h, int(p), timeout=int(cfg('SMTP_TIMEOUT', '15'))) as s:
            s.starttls()
            s.login(user, pwd)
            s.send_message(msg)
        print(f"[send-alert-email] email OK -> {to}: {subject}")
        return 0
    except Exception as e:
        print(f"[send-alert-email] email KO: {e}", file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
