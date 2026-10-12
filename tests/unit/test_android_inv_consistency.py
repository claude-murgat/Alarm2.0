"""Unit test (tier 1) — coherence numerotation invariants cote Android main/.

Bug d'origine (issue #164) : le commit 86f5f63 a renumerote la nouvelle regle
"refresh token persistant Gmail-style" de INV-082 vers INV-079 pour eviter
une collision (INV-082 designe deja `/config/escalation/bulk` atomique, cf
tests/INVARIANTS.md:520). Mais 14 references "INV-082" obsoletes
subsistaient cote Android, principalement dans :
  - des commentaires explicatifs (model/Models.kt, api/ApiService.kt,
    MainActivity.kt, AlarmPollingService.kt)
  - des messages AppLogger.log("Auth", "... (INV-082)") exportes par le
    bouton aide diagnostic du dashboard.

Pourquoi c'est critique : ces strings remontent dans les logs envoyes au
sysadmin lors d'un debug auth. Voir "Refresh KO HTTP 401 ... (INV-082)" alors
que INV-082 designe l'endpoint de config est trompeur — le diagnostic part
sur la mauvaise piste.

L'invariant verifie (meta-invariant de documentation) : aucun fichier .kt
sous android/app/src/main/ ne doit reference "INV-082" tant qu'Android
n'appelle pas /config/escalation/bulk. Toute occurrence est un residu de
renumerotation a corriger en "INV-079" (cf tests/INVARIANTS.md:449).
"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
ANDROID_MAIN = REPO_ROOT / "android" / "app" / "src" / "main"


def test_no_stale_inv082_in_android_main():
    """Aucune reference 'INV-082' ne doit subsister sous android/app/src/main/.

    INV-082 = `/config/escalation/bulk` atomique (INVARIANTS.md:520),
    endpoint non appele par l'app Android. Toute occurrence dans le code
    Android est donc un residu de la renumerotation INV-082 -> INV-079
    (cf INVARIANTS.md:449, refresh token Gmail-style).
    """
    assert ANDROID_MAIN.is_dir(), f"Repertoire introuvable : {ANDROID_MAIN}"

    offenders: list[str] = []
    for kt in sorted(ANDROID_MAIN.rglob("*.kt")):
        text = kt.read_text(encoding="utf-8")
        if "INV-082" not in text:
            continue
        rel = kt.relative_to(REPO_ROOT)
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "INV-082" in line:
                offenders.append(f"  {rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        f"{len(offenders)} reference(s) 'INV-082' obsolete(s) restante(s) "
        "sous android/app/src/main/ — renommer en 'INV-079' (refresh token "
        "persistant, cf tests/INVARIANTS.md:449) :\n" + "\n".join(offenders)
    )
