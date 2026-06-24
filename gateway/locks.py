"""Verrou partage pour l'acces serie au port AT du modem SIM7600.

`at_lock` est un **RLock (reentrant)** et NON un `threading.Lock` : le thread
`SmsReceiverThread.run()` detient deja ce verrou (acquire non-bloquant) quand il
appelle `_handle_incoming_sms`, qui re-fait `with at_lock:` pour lire le SMS
entrant (commande `AT+CMGR`). Avec un `Lock` non reentrant, le thread se bloque
sur lui-meme (self-deadlock) et gele TOUTE la gateway — SMS, appels, contact sec
et secours 4G passent tous par ce verrou — des qu'un SMS entrant arrive.

Incident constate le 2026-06-17 17:47 sur onsite-2 : l'operateur a acquitte une
alarme par SMS ("1"), le `+CMTI` a bien ete detecte, puis la gateway s'est figee
~20 min (aucun log, verrou tenu indefiniment) jusqu'au redemarrage.

Extrait dans son propre module pour etre testable sans tirer les dependances
lourdes du gateway (pyserial, numpy) : cf tests/unit/test_gateway_locking.py.
"""
import threading

# Reentrant : autorise la re-acquisition par le meme thread (cf docstring).
at_lock = threading.RLock()
