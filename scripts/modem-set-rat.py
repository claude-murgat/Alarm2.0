#!/usr/bin/env python3
# Remet le modem en selection reseau automatique (CNMP=2). L'ancien verrou WCDMA+LTE
# (CNMP=54) bloquait onsite-2 le 2026-10-21 : attache 4G refusee (EMM attach failed,
# CREG 3) sans repli 3G -> ni SMS ni appel ; en CNMP=2 inscrit en 3G en 30 s.
# Idempotent. Trouve un port AT LIBRE (saute celui de la gateway).
import serial, time, sys, glob, os, subprocess
def log(m): print(f"[modem-rat] {m}", flush=True)
for _ in range(20):
    if glob.glob('/dev/ttyUSB*'): break
    time.sleep(2)
def held(p):
    try: return subprocess.run(['fuser', p], capture_output=True).returncode == 0
    except Exception: return False
def at(port, cmd, w=2.0):
    s = serial.Serial(port, 115200, timeout=2)
    s.reset_input_buffer(); s.write((cmd + '\r').encode()); time.sleep(w)
    out = s.read(200).decode(errors='replace'); s.close(); return out
target = None
for p in ['/dev/ttyUSB2', '/dev/ttyUSB3', '/dev/ttyUSB1', '/dev/ttyUSB0']:
    if not os.path.exists(p) or held(p): continue
    try:
        if 'OK' in at(p, 'AT', 0.5): target = p; break
    except Exception: continue
if not target:
    log('aucun port AT libre+repondant'); sys.exit(1)
r1 = at(target, 'AT+CNMP=2', 2.5)
r2 = at(target, 'AT+CNMP?', 1.5)
log(f'port={target} set={r1.strip()!r} check={r2.strip()!r}')
sys.exit(0 if '+CNMP: 2' in r2 else 1)
