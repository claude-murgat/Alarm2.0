"""
Tier 2 integration tests : INV-120 — Trigger d'alarme par contact sec NC local.

Source : tests/INVARIANTS.md INV-120 [H]
  "Un front montant sur le contact sec NC ... doit déclencher l'appel
   POST /internal/alarms/trigger (X-Gateway-Key), créant une alarme dont
   l'effet métier est strictement identique à un POST /api/alarms/send."

Couvert ici (5 tests, budget P4 respecté) :
  - 401 sans X-Gateway-Key (auth, cf INV-065)
  - 200 avec clé valide → alarme active créée + assignée pos 1 de la chaîne
  - 409 si une alarme est déjà active (idempotence, cf INV-001)
  - 200 + email direction technique si chaîne vide (cf INV-080)
  - 200 avec title/message custom (différents capteurs → labels différents)

Pas couvert ici (relève d'autres invariants ou du gateway-side) :
  - FCM (à tester en E2E, dépend de l'infra réelle)
  - Debounce / edge detection / cooldown : côté gateway (modem_gateway.py)

Phase : RED — ces tests DOIVENT ÉCHOUER sur le code actuel (endpoint absent),
puis passer après implémentation GREEN dans backend/app/api/alarms_internal.py.
"""
import pytest

pytestmark = pytest.mark.integration


GATEWAY_KEY = "test-gateway-key-INV-120"
GATEWAY_HEADERS = {"X-Gateway-Key": GATEWAY_KEY}


@pytest.fixture(autouse=True)
def _set_gateway_key(monkeypatch):
    """Le check du header lit os.getenv('GATEWAY_KEY') à chaque requête.
    On force une valeur déterministe pour les tests."""
    monkeypatch.setenv("GATEWAY_KEY", GATEWAY_KEY)


@pytest.fixture
def chain_restored(client, admin_headers):
    """Snapshot la chaine d'escalade au debut, la restaure via /bulk en fin
    de test. Indispensable pour les tests qui suppriment des entrees, sinon
    pytest-randomly peut faire qu'un test posterieur tombe sur une chaine
    vide qu'il n'attendait pas (cf seed_data = user1 pos1, user2 pos2, admin
    pos3)."""
    snapshot = client.get("/api/config/escalation", headers=admin_headers).json()
    user_ids_in_order = [
        e["user_id"] for e in sorted(snapshot, key=lambda x: x["position"])
    ]
    yield
    if not user_ids_in_order:
        return
    r = client.post(
        "/api/config/escalation/bulk",
        headers=admin_headers,
        json={"user_ids": user_ids_in_order},
    )
    assert r.status_code == 200, f"chain restore failed: {r.status_code} {r.text}"


def _reset_alarms(client, admin_headers):
    r = client.post("/api/alarms/reset", headers=admin_headers)
    assert r.status_code == 200, r.text


def test_trigger_without_key_returns_401(client, admin_headers):
    """INV-120 + INV-065 : pas de X-Gateway-Key → 401, pas d'alarme créée."""
    _reset_alarms(client, admin_headers)

    r = client.post("/internal/alarms/trigger")
    assert r.status_code == 401, (
        f"INV-065 : endpoint /internal/* sans X-Gateway-Key doit renvoyer 401, "
        f"got {r.status_code} {r.text}"
    )

    # Vérifier qu'aucune alarme n'a été créée
    r = client.get("/api/alarms/active", headers=admin_headers)
    assert r.status_code == 200
    assert r.json() == [], (
        f"INV-120 : un trigger refusé ne doit JAMAIS créer d'alarme. Got {r.json()}"
    )


def test_trigger_with_valid_key_creates_active_alarm(client, admin_headers):
    """INV-120 : POST /internal/alarms/trigger avec bonne clé crée une alarme
    active, sévérité 'critical' (cf CLAUDE.md "gravité toujours critical"),
    assignée au 1er user de la chaîne d'escalade."""
    _reset_alarms(client, admin_headers)

    r = client.post("/internal/alarms/trigger", headers=GATEWAY_HEADERS)
    assert r.status_code == 200, (
        f"INV-120 : trigger avec clé valide doit créer l'alarme et renvoyer 200, "
        f"got {r.status_code} {r.text}"
    )
    data = r.json()
    assert data["status"] == "active"
    assert data["severity"] == "critical", (
        "CLAUDE.md : toujours severity=critical pour le déclenchement hardware"
    )
    assert data["assigned_user_id"] is not None, (
        "INV-120 : l'alarme doit être assignée au pos 1 de la chaîne (chaîne non vide ici)"
    )
    assert data["title"], "title doit être non-vide (default appliqué si body absent)"
    assert data["message"], "message doit être non-vide (default appliqué si body absent)"

    # Visible dans /active (sanity)
    r = client.get("/api/alarms/active", headers=admin_headers)
    assert r.status_code == 200
    active = r.json()
    assert len(active) == 1 and active[0]["id"] == data["id"]


def test_trigger_returns_409_if_alarm_already_active(client, admin_headers):
    """INV-120 + INV-001 : si une alarme est déjà active, le second trigger
    renvoie 409. C'est l'idempotence qui protège du spam pendant la boucle
    d'escalade (le gateway logue mais ne retry pas)."""
    _reset_alarms(client, admin_headers)

    r = client.post("/internal/alarms/trigger", headers=GATEWAY_HEADERS)
    assert r.status_code == 200, r.text

    r = client.post("/internal/alarms/trigger", headers=GATEWAY_HEADERS)
    assert r.status_code == 409, (
        f"INV-001 unicité — second trigger pendant qu'une alarme tourne doit "
        f"renvoyer 409, got {r.status_code} {r.text}"
    )


def test_trigger_with_empty_chain_creates_alarm_and_sends_email(
    client, admin_headers, chain_restored
):
    """INV-120 + INV-080 : chaîne vide → alarme créée (sans assignment) + email
    direction technique envoyé. Strictement identique à l'effet de
    POST /api/alarms/send sur chaîne vide.

    Note isolation : ce test SUPPRIME les entrees de la chaine pendant son
    execution. Le fixture `chain_restored` snapshot + restore via /bulk
    pour ne pas polluer les tests suivants (pytest-randomly compatible)."""
    _reset_alarms(client, admin_headers)

    # Vider la chaîne d'escalade
    esc = client.get("/api/config/escalation", headers=admin_headers).json()
    for entry in esc:
        client.delete(f"/api/config/escalation/{entry['id']}", headers=admin_headers)
    assert client.get("/api/config/escalation", headers=admin_headers).json() == []

    # S'assurer que l'email config est par défaut
    client.post("/api/config/system", headers=admin_headers, json={
        "key": "alert_email", "value": "direction_technique@charlesmurgat.com"
    })

    r = client.post("/internal/alarms/trigger", headers=GATEWAY_HEADERS)
    assert r.status_code == 200, (
        f"INV-120 : trigger sur chaîne vide doit quand même créer l'alarme, "
        f"got {r.status_code} {r.text}"
    )
    data = r.json()
    assert data["status"] == "active"

    # Email envoyé (INV-080)
    r = client.get("/api/test/last-email-sent")
    assert r.status_code == 200, r.text
    email = r.json()
    assert email.get("sent") is True, (
        f"INV-080 : email direction technique doit être envoyé sur chaîne vide. Got {email}"
    )
    assert "direction_technique@charlesmurgat.com" in email.get("to", ""), (
        f"INV-080 : destinataire attendu, got {email.get('to')}"
    )


def test_trigger_accepts_custom_title_and_message(client, admin_headers):
    """INV-120 : le gateway peut spécifier title/message dans le body
    (différents capteurs externes → labels différents). Si absent, defaults appliqués."""
    _reset_alarms(client, admin_headers)

    r = client.post(
        "/internal/alarms/trigger",
        headers=GATEWAY_HEADERS,
        json={
            "title": "Capteur fumée salle technique",
            "message": "Détection fumée par capteur câblé NC sur gateway on-site",
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["title"] == "Capteur fumée salle technique"
    assert data["message"] == "Détection fumée par capteur câblé NC sur gateway on-site"
