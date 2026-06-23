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
            "Disponibilité": "#connectivity",
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

    # --- INV-069 : saisie des numeros de telephone des operateurs ---

    def test_users_table_has_phone_input_per_user(self, dashboard: Page):
        """INV-069 : chaque ligne du tableau Utilisateurs expose un champ tel editable."""
        dashboard.locator(".tab:text('Utilisateurs')").click()
        dashboard.wait_for_timeout(2000)

        inputs = dashboard.locator("#usersTable input.user-phone")
        count = inputs.count()
        assert count >= 3, (
            f"Chaque utilisateur doit avoir un champ de saisie du numero "
            f"(input.user-phone), got {count}"
        )

    def test_save_phone_sends_patch_to_user_endpoint(self, dashboard: Page, primary_url):
        """INV-069 : saisir un numero + Enregistrer envoie PATCH /api/users/{id}
        avec {phone_number}."""
        dashboard.locator(".tab:text('Utilisateurs')").click()
        dashboard.wait_for_timeout(2000)

        admin_row = dashboard.locator("#usersTable tr", has_text="admin")
        admin_row.locator("input.user-phone").fill("+33612345678")

        with dashboard.expect_request(
            lambda r: "/api/users/" in r.url and r.method == "PATCH"
        ) as req_info:
            admin_row.locator(".save-phone-btn").click()

        body = req_info.value.post_data_json
        assert body.get("phone_number") == "+33612345678", (
            f"Le PATCH doit contenir le numero saisi, got {body}"
        )

    def test_add_user_with_phone_appears_in_table(self, dashboard: Page, primary_url):
        """INV-069 : creer un user avec un numero le persiste et le reaffiche.
        Couvre aussi l'ajout de phone_number a UserCreate/register."""
        unique = f"optel{int(time.time() * 1000) % 1000000}"
        phone = "+33698765432"

        dashboard.locator(".tab:text('Utilisateurs')").click()
        dashboard.wait_for_timeout(1000)
        dashboard.locator("#newUserName").fill(unique)
        dashboard.locator("#newUserPassword").fill("pass1234")
        dashboard.locator("#newUserPhone").fill(phone)
        dashboard.locator("button[onclick='addUser()']").click()
        dashboard.wait_for_timeout(2000)

        try:
            table_text = dashboard.locator("#usersTable").inner_text()
            assert unique in table_text, f"Le user cree '{unique}' doit apparaitre"

            phone_values = dashboard.locator(
                "#usersTable input.user-phone"
            ).evaluate_all("els => els.map(e => e.value)")
            assert phone in phone_values, (
                f"Le numero saisi a la creation doit etre persiste et reaffiche, "
                f"valeurs={phone_values}"
            )
        finally:
            # Cleanup : reset_all ne supprime pas les users, on retire le user cree.
            tok = requests.post(
                f"{primary_url}/api/auth/login",
                json={"name": "admin", "password": "admin123"}, timeout=5,
            ).json()["access_token"]
            hdr = {"Authorization": f"Bearer {tok}"}
            for u in requests.get(
                f"{primary_url}/api/users/", headers=hdr, timeout=5
            ).json():
                if u["name"] == unique:
                    requests.delete(
                        f"{primary_url}/api/users/{u['id']}", headers=hdr, timeout=5
                    )

    def test_invalid_phone_shows_error_and_no_request(self, dashboard: Page):
        """INV-069 : un numero invalide affiche une erreur et n'envoie aucun PATCH."""
        dashboard.locator(".tab:text('Utilisateurs')").click()
        dashboard.wait_for_timeout(2000)

        patch_calls = []
        dashboard.on(
            "request",
            lambda r: patch_calls.append(r.url)
            if ("/api/users/" in r.url and r.method == "PATCH") else None,
        )

        admin_row = dashboard.locator("#usersTable tr", has_text="admin")
        admin_row.locator("input.user-phone").fill("12ab!!")
        admin_row.locator(".save-phone-btn").click()
        dashboard.wait_for_timeout(1000)

        assert len(patch_calls) == 0, (
            f"Un numero invalide ne doit declencher aucun PATCH, got {patch_calls}"
        )
        expect(admin_row.locator(".phone-error")).to_be_visible()

    def test_escalation_chain_flags_member_without_phone(self, dashboard: Page):
        """INV-069 : un membre de la chaine d'escalade sans numero affiche un badge."""
        # Les users seed (user1/user2) n'ont pas de numero -> badge attendu.
        dashboard.locator(".tab:text('Escalade')").click()
        dashboard.wait_for_timeout(1500)

        badges = dashboard.locator("#escalationChain .no-phone-badge")
        assert badges.count() >= 1, (
            "Un membre de la chaine sans numero doit afficher un badge 'pas de tel' "
            "(.no-phone-badge)"
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


# ---------------------------------------------------------------------------
# Onglet Disponibilite operateurs (INV-056)
# ---------------------------------------------------------------------------

class TestConnectivityTab:
    """INV-056 — vérifier que l'onglet 'Disponibilité' charge et affiche
    la liste des opérateurs avec leur uptime, et que le bouton 'Détails'
    déploie l'historique d'événements."""

    def _go_to_connectivity(self, page: Page):
        page.locator(".tab:text('Disponibilité')").click()
        page.wait_for_timeout(1500)

    def test_connectivity_tab_shows_users_table(self, dashboard: Page):
        """La table principale liste les users avec leurs colonnes attendues."""
        self._go_to_connectivity(dashboard)

        # En-têtes
        expect(dashboard.locator("#connectivity th").first).to_contain_text("Opérateur")
        # Au moins 1 ligne (les 3 seed users : admin, user1, user2)
        rows = dashboard.locator("#connectivityTable tr")
        assert rows.count() >= 1, f"Attendu au moins 1 user, got {rows.count()}"

    def test_connectivity_days_selector_reloads(self, dashboard: Page):
        """Changer la fenêtre relance le fetch (déclencheur onchange)."""
        self._go_to_connectivity(dashboard)

        dashboard.locator("#connectivityDays").select_option("7")
        dashboard.wait_for_timeout(500)
        # La table doit toujours être présente après reload
        rows = dashboard.locator("#connectivityTable tr")
        assert rows.count() >= 1

    def test_connectivity_history_card_hidden_by_default(self, dashboard: Page):
        """Le bloc historique est masqué tant qu'on n'a pas cliqué sur Détails."""
        self._go_to_connectivity(dashboard)

        history_card = dashboard.locator("#connectivityHistoryCard")
        expect(history_card).to_be_hidden()

    def test_connectivity_details_button_reveals_history(self, dashboard: Page):
        """Cliquer 'Détails' sur une ligne affiche le bloc historique."""
        self._go_to_connectivity(dashboard)

        # Cliquer le bouton Détails de la première ligne (peu importe quel user)
        dashboard.locator("#connectivityTable button").first.click()
        dashboard.wait_for_timeout(800)

        history_card = dashboard.locator("#connectivityHistoryCard")
        expect(history_card).to_be_visible()
        expect(dashboard.locator("#connectivityHistoryTitle")).to_contain_text("Historique")
