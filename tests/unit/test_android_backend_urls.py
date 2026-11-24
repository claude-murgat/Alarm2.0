"""INV-ANDROID-402 (revise le 2026-11-24) : ordre d'essai des backends de l'APK release.

Decision du proprietaire du 2026-11-24 (INV-199) : la facade https://alarme.charlesmurgat.com (n'importe
quel noeud sain) en tete, NODE3 en direct ensuite, les URL LAN en dernier. Espresso (test34) tourne sur
le build debug et ne voit jamais la liste release : seul ce test la verifie, en lisant build.gradle.kts.
Les notes de la release GitHub (android-release.yml) annoncent la meme liste, dans le meme ordre.
"""
import pathlib
import re

import pytest

# no_mutation : lit des fichiers du depot, sans toucher backend/app/logic.
pytestmark = [pytest.mark.unit, pytest.mark.no_mutation]

REPO = pathlib.Path(__file__).resolve().parents[2]
GRADLE = (REPO / "android/app/build.gradle.kts").read_text(encoding="utf-8")
RELEASE_WORKFLOW = (REPO / ".github/workflows/android-release.yml").read_text(encoding="utf-8")

FACADE = "https://alarme.charlesmurgat.com/"
NODE3 = "http://51.210.105.102:8000/"
LAN = ["http://192.168.1.50:8000/", "http://192.168.1.51:8000/",      # onsite-1/-2 (Izeron)
       "http://172.16.1.118:8000/", "http://172.16.1.119:8000/",      # alarm-1/-2 (Beaufort)
       "http://172.16.1.120:8000/", "http://172.16.1.121:8000/"]      # alarm-3/-4 (Beaufort avant le demenagement)


def release_urls():
    block = GRADLE[GRADLE.index("buildTypes {"):]
    entries = re.search(r'"BACKEND_URLS", "\\"" \+ listOf\((.*?)\)\.joinToString\(","\)', block, re.S).group(1)
    return re.findall(r'"(https?://[^"]+)"', entries)


def debug_urls():
    default = GRADLE[GRADLE.index("defaultConfig {"):GRADLE.index("buildTypes {")]
    return re.search(r'"BACKEND_URLS",\s*"\\"([^\\]+)\\""', default).group(1).split(",")


def test_release_tries_the_facade_first_then_node3_direct_then_the_lan():
    assert release_urls() == [FACADE, NODE3, *LAN]


def test_debug_still_targets_the_local_dev_cluster_only():
    assert debug_urls() == ["http://10.0.2.2:8000/", "http://10.0.2.2:8001/", "http://10.0.2.2:8002/"]


def test_release_notes_list_the_same_backends_in_the_same_order():
    notes = RELEASE_WORKFLOW[RELEASE_WORKFLOW.index("### Backends joints"):]
    notes = notes[:notes.index("release-notes.md")]
    assert re.findall(r"printf '\d+\. `([^`]+)`", notes) == release_urls()
