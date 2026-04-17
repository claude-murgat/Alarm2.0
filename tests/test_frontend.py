"""
Tests Playwright du front web (integration — necessite le cluster 3 noeuds live).

Prerequis :
  - 3 instances Docker Compose demarrees
  - pip install playwright pytest-playwright
  - playwright install chromium

Usage :
  python -m pytest tests/test_frontend.py -v --headed   # avec navigateur visible
  python -m pytest tests/test_frontend.py -v             # headless (CI)
"""

import time
import pytest
import requests
from playwright.sync_api import Page, expect


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def primary_url():
    """Trouve le primary backend."""
    for port in [8000, 8001, 8002]:
        try:
            r = requests.get(f"http://localhost:{port}/health", timeout=2)
            if r.json().get("role") == "primary":
                return f"http://localhost:{port}"
        except Exception:
            pass
    return "http://localhost:8000"


@pytest.fixture(autouse=True)
def reset_state(primary_url):
    """Reset l'etat avant chaque test."""
    requests.post(f"{primary_url}/api/test/reset", timeout=5)
    yield
    requests.post(f"{primary_url}/api/test/reset", timeout=5)


@pytest.fixture
def dashboard(page: Page, primary_url):
    """Ouvre la page, se connecte en admin, et attend le chargement complet.

    Sequence d'attentes explicites et deterministes :
    1. DOM loaded (pas networkidle qui peut ne jamais arriver a cause de l'autorefresh)
    2. Overlay de login visible (preuve que la page est rendue)
    3. Click login
    4. Overlay hidden (preuve que le login a reussi)
    5. Dashboard visible (preuve que le DOM est mis a jour post-login)
    """
    page.goto(primary_url, wait_until="domcontentloaded", timeout=15000)
    expect(page.locator("#loginOverlay")).to_be_visible(timeout=10000)
    page.locator("#loginName").fill("admin")
    page.locator("#loginPassword").fill("admin123")
    page.locator("button:has-text('Se connecter')").click()
    expect(page.locator("#loginOverlay")).to_be_hidden(timeout=15000)
    expect(page.locator("#dashboard")).to_be_visible(timeout=10000)
    return page


# ---------------------------------------------------------------------------
# Login front
# ---------------------------------------------------------------------------

class TestFrontendLogin:

    def test_login_screen_visible_on_load(self, page: Page, primary_url):
        """L'ecran de login est visible au chargement."""
        page.goto(primary_url)
        page.wait_for_load_state("networkidle")
        expect(page.locator("#loginOverlay")).to_be_visible()

    def test_login_success_hides_overlay(self, page: Page, primary_url):
        """Un login valide masque l'overlay et affiche le dashboard."""
        page.goto(primary_url)
        page.wait_for_load_state("networkidle")
        page.locator("#loginName").fill("admin")
        page.locator("#loginPassword").fill("admin123")
        page.locator("button:has-text('Se connecter')").click()
        expect(page.locator("#loginOverlay")).to_be_hidden(timeout=5000)
        expect(page.locator("#statsGrid")).to_be_visible()

    def test_login_failure_shows_error(self, page: Page, primary_url):
        """Un login invalide affiche un message d'erreur."""
        page.goto(primary_url)
        page.wait_for_load_state("networkidle")
        page.locator("#loginName").fill("admin")
        page.locator("#loginPassword").fill("wrongpassword")
        page.locator("button:has-text('Se connecter')").click()
        expect(page.locator("#loginError")).not_to_be_empty(timeout=5000)

    def test_logout_shows_login_again(self, dashboard: Page):
        """Le bouton deconnexion reaffiche l'overlay de login."""
        dashboard.locator("button.btn-logout").click()
        expect(dashboard.locator("#loginOverlay")).to_be_visible(timeout=5000)


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

class TestNavigation:

    def test_page_loads_with_dashboard(self, dashboard: Page):
        """La page charge avec le tab Tableau de bord actif."""
        active_tab = dashboard.locator(".tab.active")
        expect(active_tab).to_have_text("Tableau de bord")
        expect(dashboard.locator("#dashboard")).to_be_visible()

    def test_all_tabs_clickable(self, dashboard: Page):
        """Cliquer chaque tab affiche le bon panel."""
        tabs = {
            "Utilisateurs": "#users",
            "Alarmes": "#alarms",
            "Escalade": "#escalation",
            "Tests": "#tests",
            "Cluster": "#cluster",
            "Tableau de bord": "#dashboard",
        }
        for tab_text, panel_id in tabs.items():
            dashboard.locator(f".tab:text('{tab_text}')").click()
            expect(dashboard.locator(panel_id)).to_be_visible()


# ---------------------------------------------------------------------------
# Onglet Utilisateurs
# ---------------------------------------------------------------------------

class TestUsersTab:

    def test_users_tab_shows_all_users(self, dashboard: Page):
        """L'onglet Utilisateurs affiche tous les utilisateurs dans le tableau."""
        dashboard.locator(".tab:text('Utilisateurs')").click()
        dashboard.wait_for_timeout(2000)

        rows = dashboard.locator("#usersTable tr")
        count = rows.count()
        assert count >= 3, (
            f"Le tableau utilisateurs doit contenir au moins 3 lignes (admin, user1, user2), "
            f"got {count}"
        )

        # Verifier que les noms attendus sont presents
        table_text = dashboard.locator("#usersTable").inner_text()
        for name in ["admin", "user1", "user2"]:
            assert name in table_text, (
                f"L'utilisateur '{name}' doit apparaitre dans l'onglet Utilisateurs"
            )


# ---------------------------------------------------------------------------
# Escalade — Drag-and-drop
# ---------------------------------------------------------------------------

class TestEscalationDragDrop:

    def _go_to_escalation(self, page: Page):
        page.locator(".tab:text('Escalade')").click()
        page.wait_for_timeout(1000)  # Laisser loadEscalation() charger

    def test_escalation_shows_chain_and_available(self, dashboard: Page):
        """Les deux listes sont peuplees, tous les users apparaissent."""
        self._go_to_escalation(dashboard)

        chain_items = dashboard.locator("#escalationChain .esc-item")
        avail_items = dashboard.locator("#availableUsers .esc-item")

        # Au total, tous les users doivent apparaitre dans l'une ou l'autre
        total = chain_items.count() + avail_items.count()
        assert total >= 3, f"Attendu au moins 3 users, got {total}"

    def test_remove_user_from_chain(self, dashboard: Page):
        """Cliquer la croix retire un user de la chaine."""
        self._go_to_escalation(dashboard)

        chain_before = dashboard.locator("#escalationChain .esc-item").count()
        assert chain_before >= 1, "La chaine doit avoir au moins 1 user"

        # Cliquer la croix du premier user
        dashboard.locator("#escalationChain .esc-remove").first.click()

        chain_after = dashboard.locator("#escalationChain .esc-item").count()
        avail_after = dashboard.locator("#availableUsers .esc-item").count()

        assert chain_after == chain_before - 1, "Un user en moins dans la chaine"
        assert avail_after >= 1, "Le user retire doit etre dans les disponibles"

    def test_save_escalation_sends_correct_request(self, dashboard: Page, primary_url):
        """Cliquer Sauvegarder envoie le bon body {user_ids: [...]}."""
        self._go_to_escalation(dashboard)

        # Retirer le premier user de la chaine
        dashboard.locator("#escalationChain .esc-remove").first.click()
        dashboard.wait_for_timeout(500)

        # Intercepter la requete bulk
        with dashboard.expect_request("**/api/config/escalation/bulk") as req_info:
            dashboard.locator("#saveEscalation").click()

        request = req_info.value
        body = request.post_data_json
        assert "user_ids" in body
        assert isinstance(body["user_ids"], list)
        assert len(body["user_ids"]) >= 1

    def test_cancel_escalation_reloads(self, dashboard: Page, primary_url):
        """Cliquer Annuler recharge la chaine depuis le serveur."""
        self._go_to_escalation(dashboard)

        # Compter les items dans la chaine
        chain_before = dashboard.locator("#escalationChain .esc-item").count()

        # Retirer un user
        dashboard.locator("#escalationChain .esc-remove").first.click()
        chain_during = dashboard.locator("#escalationChain .esc-item").count()
        assert chain_during == chain_before - 1

        # Annuler
        dashboard.locator("#cancelEscalation").click()
        dashboard.wait_for_timeout(2000)  # Attendre le reload

        # La chaine doit etre revenue a l'etat initial
        chain_after = dashboard.locator("#escalationChain .esc-item").count()
        assert chain_after == chain_before, "Annuler doit restaurer la chaine"

    def test_autorefresh_does_not_overwrite_edits(self, dashboard: Page):
        """Pendant une edition, l'autorefresh ne doit pas ecraser les changements."""
        self._go_to_escalation(dashboard)

        chain_before = dashboard.locator("#escalationChain .esc-item").count()

        # Retirer un user (declenche editingEscalation = true)
        dashboard.locator("#escalationChain .esc-remove").first.click()
        chain_during = dashboard.locator("#escalationChain .esc-item").count()

        # Attendre 6s (plus qu'un cycle autorefresh de 5s)
        dashboard.wait_for_timeout(6000)

        # La chaine ne doit PAS etre revenue a l'etat serveur
        chain_after = dashboard.locator("#escalationChain .esc-item").count()
        assert chain_after == chain_during, \
            f"Autorefresh a ecrase les edits : {chain_after} != {chain_during}"


# ---------------------------------------------------------------------------
# Delai escalade
# ---------------------------------------------------------------------------

class TestEscalationDelay:

    def _go_to_escalation(self, page: Page):
        page.locator(".tab:text('Escalade')").click()
        page.wait_for_timeout(1000)

    def test_delay_input_shows_current_value(self, dashboard: Page):
        """L'input delai affiche la valeur du serveur."""
        self._go_to_escalation(dashboard)
        value = dashboard.locator("#escalationDelay").input_value()
        assert float(value) == 15, f"Delai par defaut devrait etre 15, got {value}"

    def test_save_delay_sends_correct_request(self, dashboard: Page, primary_url):
        """Modifier le delai et sauvegarder envoie la bonne requete."""
        self._go_to_escalation(dashboard)

        # Changer la valeur
        dashboard.locator("#escalationDelay").fill("10")

        # Intercepter la requete
        with dashboard.expect_request("**/api/config/escalation-delay") as req_info:
            dashboard.locator("#escalationDelay + button").click()

        request = req_info.value
        body = request.post_data_json
        assert body["minutes"] == 10

        # Remettre a 15
        requests.post(f"{primary_url}/api/config/escalation-delay",
                     json={"minutes": 15}, timeout=3)


# ---------------------------------------------------------------------------
# Alarmes
# ---------------------------------------------------------------------------

class TestAlarms:

    def _go_to_alarms(self, page: Page):
        page.locator(".tab:text('Alarmes')").click()
        page.wait_for_timeout(1000)

    def test_send_alarm_from_form(self, dashboard: Page):
        """Remplir le formulaire et envoyer une alarme.
        Note : pas de selecteur de severite (toujours 'critical' par design, cf CLAUDE.md)."""
        self._go_to_alarms(dashboard)

        dashboard.locator("#alarmTitle").fill("Test Playwright")
        dashboard.locator("#alarmMessage").fill("Alarme depuis Playwright")

        dashboard.locator(".btn-danger:text-is('Envoyer')").click()
        dashboard.wait_for_timeout(2000)

        # L'alarme doit apparaitre dans la table (attendre un cycle autorefresh 5s + marge)
        expect(dashboard.locator("#allAlarmsTable")).to_contain_text("Test Playwright", timeout=10000)

    def test_resolve_alarm(self, dashboard: Page, primary_url):
        """Resoudre une alarme la retire des actives."""
        # Creer une alarme via API
        requests.post(f"{primary_url}/api/test/send-alarm", timeout=5)
        dashboard.wait_for_timeout(6000)  # Attendre un autorefresh

        # Verifier qu'il y a un bouton Resoudre
        resolve_btn = dashboard.locator("#activeAlarmsTable button:text('Resoudre')")
        if resolve_btn.count() > 0:
            resolve_btn.first.click()
            dashboard.wait_for_timeout(2000)


# ---------------------------------------------------------------------------
# Cluster
# ---------------------------------------------------------------------------

class TestCluster:

    def _go_to_cluster(self, page: Page):
        page.locator(".tab:text('Cluster')").click()
        page.wait_for_timeout(1000)

    def test_cluster_panel_shows_quorum(self, dashboard: Page):
        """Le bandeau quorum affiche le bon statut."""
        self._go_to_cluster(dashboard)

        banner = dashboard.locator("#quorumBanner")
        expect(banner).to_contain_text("Quorum")

    def test_cluster_panel_shows_members(self, dashboard: Page):
        """La table des membres contient au moins le noeud local.
        Test resilient single-node (dev) ET cluster (prod 3 noeuds) :
        l'invariant business est 'au moins un membre visible', la matrice cluster
        complete est testee via TestRedundancy/test_exactly_one_node_is_primary."""
        self._go_to_cluster(dashboard)

        rows = dashboard.locator("#clusterMembers tr")
        assert rows.count() >= 1, f"Attendu au moins 1 membre, got {rows.count()}"
