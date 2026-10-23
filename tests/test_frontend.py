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

import os
import re
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


def _admin_headers(primary_url):
    tok = requests.post(
        f"{primary_url}/api/auth/login",
        json={"name": "admin", "password": "admin123"}, timeout=5,
    ).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


def _delete_user(primary_url, name):
    """Cleanup : reset_all ne supprime pas les users, on retire le user cree."""
    hdr = _admin_headers(primary_url)
    for u in requests.get(f"{primary_url}/api/users/", headers=hdr, timeout=5).json():
        if u["name"] == name:
            requests.delete(f"{primary_url}/api/users/{u['id']}", headers=hdr, timeout=5)


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

class TestSessionRestoreAfterReload:
    """INV-135 : apres F5 (session restauree depuis sessionStorage), la page
    charge ses donnees sans erreur JS et ouvre le WebSocket live. Bug du
    2026-10-15 : loadAll() appele avant la declaration de liveWs ->
    ReferenceError, aucun panneau charge."""

    def test_reload_with_session_loads_without_js_error(self, dashboard: Page):
        errors = []
        dashboard.on("pageerror", lambda e: errors.append(str(e)))
        dashboard.reload(wait_until="domcontentloaded")
        expect(dashboard.locator("#loginOverlay")).to_be_hidden(timeout=10000)
        dashboard.wait_for_function("liveWs && liveWs.readyState === WebSocket.OPEN", timeout=10000)
        assert errors == []


class TestHeaderWithoutTestEndpoints:
    """En prod, /api/test/* repond 404 (INV-076). L'en-tete et les cartes du
    tableau de bord ne doivent pas en dependre (bug prod 2026-10-14 : en-tete
    bloque sur "Serveur injoignable")."""

    def test_header_shows_alarm_state_when_test_routes_are_disabled(self, page: Page, primary_url):
        page.route("**/api/test/**", lambda route: route.fulfill(status=404, body="{}"))
        page.goto(primary_url, wait_until="domcontentloaded", timeout=15000)
        page.locator("#loginName").fill("admin")
        page.locator("#loginPassword").fill("admin123")
        page.locator("button:has-text('Se connecter')").click()
        expect(page.locator("#dashboard")).to_be_visible(timeout=10000)

        expect(page.locator("#alarmCount")).to_have_text("Aucune alarme", timeout=8000)
        expect(page.locator("#statsGrid")).to_contain_text("Aucune alarme")


class TestTestsTabInProd:
    """En prod (/api/test/* -> 404, INV-076), l'onglet Tests doit rester utile :
    "Envoyer une alarme" passe par la vraie route d'envoi, et les boutons de
    simulation (qui n'ont de sens qu'en test) sont masques. Bug terrain
    2026-10-14 : "Erreur : Test endpoints are disabled"."""

    def _prod_dashboard(self, page: Page, primary_url):
        def health(route):
            body = route.fetch().json()
            body["test_endpoints_enabled"] = False
            route.fulfill(json=body)
        page.route("**/health", health)
        page.route("**/api/test/**", lambda route: route.fulfill(status=404, body="{}"))
        page.goto(primary_url, wait_until="domcontentloaded", timeout=15000)
        page.locator("#loginName").fill("admin")
        page.locator("#loginPassword").fill("admin123")
        page.locator("button:has-text('Se connecter')").click()
        expect(page.locator("#dashboard")).to_be_visible(timeout=10000)
        page.locator(".tab:text('Tests')").click()
        return page

    def test_send_alarm_uses_real_route(self, page: Page, primary_url):
        page = self._prod_dashboard(page, primary_url)
        with page.expect_request(lambda r: r.url.endswith("/api/alarms/send") and r.method == "POST"):
            page.locator("button:has-text('Envoyer une alarme')").click()
        expect(page.locator("#tests")).not_to_contain_text("Test endpoints are disabled")

    def test_prod_buttons_use_real_admin_routes(self, page: Page, primary_url):
        """INV-138 : les actions reviennent en prod via de vraies routes admin."""
        page = self._prod_dashboard(page, primary_url)
        # Les 2 actions heartbeat sont interceptees : sinon admin aurait ses
        # heartbeats refuses 5 min pour les tests suivants (effet couvert en tier 2).
        page.route("**/api/devices/me/**", lambda route: route.fulfill(
            json={"status": "ok", "paused": True, "until": "2026-01-01T00:00:00"}))
        for label, path in [("Escalader au suivant", "/api/alarms/escalate-now"),
                            ("Me mettre hors ligne", "/api/devices/me/go-offline"),
                            ("Couper mes heartbeats", "/api/devices/me/heartbeat-pause")]:
            btn = page.locator(f"#tests button:visible:has-text('{label}')")
            expect(btn).to_be_visible()
            with page.expect_request(lambda r, p=path: r.url.endswith(p) and r.method == "POST"):
                btn.click()
        expect(page.locator("#testLog")).not_to_contain_text("Erreur")

    def test_prod_resolve_active_alarms(self, page: Page, primary_url):
        """INV-138 : « Résoudre les alarmes actives » resout sans rien supprimer."""
        token = requests.post(f"{primary_url}/api/auth/login",
                              json={"name": "admin", "password": "admin123"}, timeout=5).json()["access_token"]
        hdr = {"Authorization": f"Bearer {token}"}
        sent = requests.post(f"{primary_url}/api/alarms/send", headers=hdr, timeout=5, json={
            "site": "izeron", "title": "Alarme de test", "message": "INV-138", "severity": "critical", "assigned_user_id": None})
        assert sent.status_code == 200, sent.text
        page = self._prod_dashboard(page, primary_url)
        with page.expect_request(lambda r: r.url.endswith(f"/api/alarms/{sent.json()['id']}/resolve")):
            page.locator("#tests button:visible:has-text('Résoudre les alarmes actives')").click()
        expect(page.locator("#testLog")).to_contain_text("résolue", timeout=8000)
        assert requests.get(f"{primary_url}/api/alarms/active", headers=hdr, timeout=5).json() == []
        # Rien n'est supprime : l'alarme reste dans l'historique.
        assert any(a["id"] == sent.json()["id"] for a in
                   requests.get(f"{primary_url}/api/alarms/", headers=hdr, timeout=5).json())

    def test_dev_simulation_buttons_hidden_in_prod(self, page: Page, primary_url):
        """Les simulations /api/test/* (dev/CI) restent masquees en prod."""
        page = self._prod_dashboard(page, primary_url)
        for label in ["Tous les users hors ligne", "Couper les heartbeats", "Remise a zero"]:
            expect(page.locator(f"button:has-text('{label}')")).to_be_hidden()


class TestActionFeedback:
    """INV-139 : snackbar verte apres une action reussie, rouge avec le message
    du serveur apres un echec."""

    def test_success_snackbar(self, dashboard: Page):
        dashboard.locator(".tab:text('Tests')").click()
        dashboard.locator("#tests button:visible:has-text('Escalader au suivant')").click()
        expect(dashboard.locator("#snackbar.show.success")).to_contain_text("Action effectuée", timeout=5000)

    def test_error_snackbar_shows_server_message(self, dashboard: Page):
        dashboard.route("**/api/alarms/escalate-now", lambda route: route.fulfill(
            status=409, json={"detail": "Refus de test INV-139"}))
        dashboard.locator(".tab:text('Tests')").click()
        dashboard.locator("#tests button:visible:has-text('Escalader au suivant')").click()
        expect(dashboard.locator("#snackbar.show.error")).to_contain_text("Refus de test INV-139", timeout=5000)


class TestWaitingStatus:
    """INV-143 : hors garde + telephone enregistre + pas de heartbeat -> « En attente » ;
    la personne de garde sans heartbeat reste « Hors ligne » (vraie anomalie)."""

    def test_users_tab_shows_waiting_for_off_duty_user_with_phone(self, dashboard: Page, primary_url):
        requests.post(f"{primary_url}/api/test/simulate-watchdog-failure", timeout=5)  # tous hors ligne
        tok = requests.post(f"{primary_url}/api/auth/login",
                            json={"name": "user2", "password": "user123"}, timeout=5).json()["access_token"]
        r = requests.post(f"{primary_url}/api/devices/fcm-token", timeout=5,
                          headers={"Authorization": f"Bearer {tok}"},
                          json={"token": "tok-user2-inv143-web", "device_id": "dev-user2-inv143-web"})
        assert r.status_code == 200, r.text
        dashboard.locator(".tab:text('Utilisateurs')").click()
        row = lambda name: dashboard.locator("#usersTable tr", has_text=name)
        expect(row("user2").locator(".badge.waiting")).to_have_text("En attente", timeout=8000)
        expect(row("user1").locator(".badge.offline")).to_have_text("Hors ligne", timeout=8000)


class TestTimesShownInLocalTime:
    """Le backend envoie de l'UTC sans suffixe. Un navigateur a Paris doit voir
    l'heure de Paris (incident 2026-10-14 : contact sec 15:32 affiche 13:32)."""

    def test_log_tab_shows_local_time(self, browser, primary_url):
        ctx = browser.new_context(timezone_id="Europe/Paris", locale="fr-FR")
        page = ctx.new_page()
        try:
            token = requests.post(f"{primary_url}/api/auth/login",
                                  json={"name": "admin", "password": "admin123"}, timeout=5).json()["access_token"]
            hdr = {"Authorization": f"Bearer {token}"}

            page.goto(primary_url, wait_until="domcontentloaded", timeout=15000)
            page.locator("#loginName").fill("admin")
            page.locator("#loginPassword").fill("admin123")
            page.locator("button:has-text('Se connecter')").click()
            expect(page.locator("#dashboard")).to_be_visible(timeout=10000)
            page.locator(".tab:text('Log')").click()
            expect(page.locator("#auditTableBody tr").first).to_be_visible(timeout=8000)
            # Reference lue APRES la connexion du navigateur (qui cree elle-meme un evenement).
            ts = requests.get(f"{primary_url}/api/audit/?page=1&page_size=1", headers=hdr, timeout=5).json()[0]["timestamp"]

            expected = page.evaluate("ts => new Date(ts + 'Z').toLocaleString()", ts)
            expect(page.locator("#auditTableBody tr").first.locator("td").first).to_have_text(expected, timeout=8000)
        finally:
            ctx.close()


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
            _delete_user(primary_url, unique)

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

    # --- INV-151 : role test ---

    def test_add_test_user_shows_test_column(self, dashboard: Page, primary_url):
        """INV-151 : creer un user avec Test=Oui l'affiche « Oui » dans la colonne Test."""
        unique = f"optest{int(time.time() * 1000) % 1000000}"
        dashboard.locator(".tab:text('Utilisateurs')").click()
        dashboard.wait_for_timeout(1000)
        dashboard.locator("#newUserName").fill(unique)
        dashboard.locator("#newUserPassword").fill("pass1234")
        dashboard.locator("#newUserTest").select_option("true")
        dashboard.locator("button[onclick='addUser()']").click()
        try:
            row = dashboard.locator("#usersTable tr", has_text=unique)
            expect(row.locator("td.user-test")).to_have_text("Oui")
            user1_row = dashboard.locator("#usersTable tr", has_text="user1")
            expect(user1_row.locator("td.user-test")).to_have_text("Non")
        finally:
            _delete_user(primary_url, unique)

    def test_escalation_chain_flags_test_member(self, dashboard: Page, primary_url):
        """INV-151 : un compte test dans la chaine d'escalade affiche un badge
        .test-badge, les autres membres non."""
        unique = f"esctest{int(time.time() * 1000) % 1000000}"
        user = requests.post(
            f"{primary_url}/api/auth/register", headers=_admin_headers(primary_url),
            json={"name": unique, "password": "pass1234", "is_test": True}, timeout=5,
        ).json()
        try:
            r = requests.post(
                f"{primary_url}/api/config/escalation", headers=_admin_headers(primary_url),
                json={"position": 4, "user_id": user["id"]}, timeout=5,
            )
            assert r.status_code == 200, r.text
            # Recharger dans l'ordre : le badge lit la liste des users.
            dashboard.evaluate("async () => { await loadUsers(); await loadEscalation(); }")
            dashboard.locator(".tab:text('Escalade')").click()

            member = dashboard.locator("#escalationChain .esc-item", has_text=unique)
            expect(member.locator(".test-badge")).to_be_visible()
            expect(dashboard.locator("#escalationChain .test-badge")).to_have_count(1)
        finally:
            # Sortir le user de la chaine avant de le supprimer (sinon FK -> echec).
            requests.post(f"{primary_url}/api/test/reset", timeout=5)
            _delete_user(primary_url, unique)


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
# Autorefresh 5s : ne doit jamais ecraser une saisie non enregistree
# (bug terrain 2026-10-12 : email d'alerte impossible a saisir, numero de
# telephone qui "saute" dans l'onglet Utilisateurs)
# ---------------------------------------------------------------------------

class TestAutorefreshKeepsUnsavedInput:

    def _type_then_leave_field(self, page: Page, selector: str, text: str):
        """Saisit puis quitte le champ SANS enregistrer, et laisse passer plus
        d'un cycle d'autorefresh (5s). Le focus est retire expres : l'utilisateur
        peut cliquer ailleurs avant d'appuyer sur Enregistrer."""
        page.locator(selector).fill(text)
        page.locator("h1").first.click()
        page.wait_for_timeout(6000)

    def test_alert_email_survives_autorefresh(self, dashboard: Page):
        dashboard.locator(".tab:text('Escalade')").click()
        dashboard.wait_for_timeout(1000)

        self._type_then_leave_field(dashboard, "#alertEmail", "astreinte@exemple.com")

        assert dashboard.locator("#alertEmail").input_value() == "astreinte@exemple.com", (
            "L'autorefresh a ecrase l'email d'alerte en cours de saisie"
        )

    def test_escalation_delay_survives_autorefresh(self, dashboard: Page):
        dashboard.locator(".tab:text('Escalade')").click()
        dashboard.wait_for_timeout(1000)

        self._type_then_leave_field(dashboard, "#escalationDelay", "7")

        assert dashboard.locator("#escalationDelay").input_value() == "7", (
            "L'autorefresh a ecrase le delai d'escalade en cours de saisie"
        )

    def test_user_phone_survives_autorefresh(self, dashboard: Page):
        dashboard.locator(".tab:text('Utilisateurs')").click()
        dashboard.wait_for_timeout(2000)

        selector = "#usersTable tr:has-text('admin') input.user-phone"
        self._type_then_leave_field(dashboard, selector, "+33611223344")

        assert dashboard.locator(selector).input_value() == "+33611223344", (
            "L'autorefresh a ecrase le numero de telephone en cours de saisie"
        )

    def test_autorefresh_resumes_after_phone_saved(self, dashboard: Page, primary_url):
        """Une fois le numero enregistre, le tableau doit de nouveau suivre le
        serveur (sinon la protection ci-dessus le figerait pour toujours)."""
        dashboard.locator(".tab:text('Utilisateurs')").click()
        dashboard.wait_for_timeout(2000)

        admin_row = dashboard.locator("#usersTable tr", has_text="admin")
        admin_row.locator("input.user-phone").fill("+33611223344")
        admin_row.locator(".save-phone-btn").click()
        dashboard.wait_for_timeout(1000)

        # Changement fait hors de la page (autre admin) sur user2
        token = requests.post(f"{primary_url}/api/auth/login",
                              json={"name": "admin", "password": "admin123"},
                              timeout=5).json()["access_token"]
        hdr = {"Authorization": f"Bearer {token}"}
        user2 = next(u for u in requests.get(f"{primary_url}/api/users/",
                                             headers=hdr, timeout=5).json()
                     if u["name"] == "user2")
        requests.patch(f"{primary_url}/api/users/{user2['id']}", headers=hdr,
                       json={"phone_number": "+33655667788"}, timeout=5)

        dashboard.wait_for_timeout(6000)
        user2_phone = dashboard.locator(
            "#usersTable tr:has-text('user2') input.user-phone").input_value()
        assert user2_phone == "+33655667788", (
            f"Apres enregistrement, l'autorefresh doit reprendre, got {user2_phone!r}"
        )


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


# ---------------------------------------------------------------------------
# INV-129 : indicateur de signal 4G du modem gateway (en-tete)
# INV-158 : un indicateur par passerelle, nomme par son site
# ---------------------------------------------------------------------------

class TestModemSignalIndicator:

    GATEWAY_HEADERS = {"X-Gateway-Key": os.getenv("GATEWAY_KEY", "changeme-in-prod")}

    def _report(self, primary_url, **status):
        r = requests.post(f"{primary_url}/internal/alarms/modem-status",
                          json=status, headers=self.GATEWAY_HEADERS, timeout=5)
        assert r.status_code == 200, r.text

    def test_shows_bars_from_gateway_report(self, dashboard: Page, primary_url):
        self._report(primary_url, gateway_id="e2e-izeron", site="izeron",
                     registered=True, tech="LTE", rsrp=-115.7, csq=15)
        icon = dashboard.locator('#modemSignal .modem-signal[data-gateway="e2e-izeron"]')
        expect(icon).to_have_attribute("data-state", "ok", timeout=8000)
        expect(icon).to_have_attribute("data-bars", "1")
        expect(icon).to_have_attribute("data-site", "izeron")
        expect(icon).to_have_attribute("title", re.compile(r"Izeron.*-115\.7 dBm"))

    def test_no_network_is_flagged(self, dashboard: Page, primary_url):
        self._report(primary_url, gateway_id="e2e-beaufort", site="beaufort",
                     registered=False, tech=None, rsrp=None, csq=0)
        icon = dashboard.locator('#modemSignal .modem-signal[data-gateway="e2e-beaufort"]')
        expect(icon).to_have_attribute("data-state", "no_network", timeout=8000)
        expect(icon).to_have_attribute("data-bars", "0")
        expect(icon).to_contain_text("Beaufort")


# ---------------------------------------------------------------------------
# INV-136 : schema d'escalade temps reel (onglet Escalade)
# ---------------------------------------------------------------------------

class TestEscalationLiveDiagram:

    def _login(self, primary_url, name, password):
        r = requests.post(f"{primary_url}/api/auth/login", json={"name": name, "password": password}, timeout=10)
        r.raise_for_status()
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def _send_alarm(self, primary_url):
        r = requests.post(f"{primary_url}/api/alarms/send", headers=self._login(primary_url, "admin", "admin123"),
                          json={"site": "izeron", "title": "Schema INV-136", "message": "test", "severity": "critical"}, timeout=10)
        r.raise_for_status()
        return r.json()["id"]

    def test_no_alarm_shows_idle_chain(self, dashboard: Page):
        dashboard.locator(".tab[data-tab='escalation']").click()
        live = dashboard.locator("#escalationLive")
        expect(live).to_have_attribute("data-state", "none", timeout=8000)
        expect(live.locator(".esc-lane")).to_have_count(3)

    def test_live_updates_on_alarm_escalation_and_ack(self, dashboard: Page, primary_url):
        dashboard.locator(".tab[data-tab='escalation']").click()
        live = dashboard.locator("#escalationLive")
        expect(live).to_have_attribute("data-state", "none", timeout=8000)

        alarm_id = self._send_alarm(primary_url)
        expect(live).to_have_attribute("data-state", "active", timeout=10000)
        first = live.locator(".esc-lane").nth(0)
        expect(first).to_have_attribute("data-role", "current")
        expect(first.locator("[data-stage='notif']")).to_have_attribute("data-status", "ok")
        expect(first.locator("[data-stage='escalate']")).to_have_attribute("data-status", "doing")

        requests.post(f"{primary_url}/api/test/trigger-escalation", timeout=10).raise_for_status()
        expect(live.locator(".esc-lane").nth(1)).to_have_attribute("data-role", "current", timeout=10000)
        expect(first).to_have_attribute("data-role", "notified")

        requests.post(f"{primary_url}/api/alarms/{alarm_id}/ack", headers=self._login(primary_url, "user2", "user123"),
                      timeout=10).raise_for_status()
        expect(live).to_have_attribute("data-state", "acknowledged", timeout=10000)
        expect(live.locator(".esc-lane.acked")).to_have_attribute("data-role", "current")
        expect(live.locator(".esc-banner.ok")).to_contain_text("user2")


# ---------------------------------------------------------------------------
# INV-150 : web utilisable sur telephone
# ---------------------------------------------------------------------------

class TestMobileLayout:

    def test_no_tab_scrolls_the_page_horizontally_on_a_phone(self, page: Page, primary_url):
        """INV-150 : a 375 px, aucun onglet ne deborde horizontalement (alarme en cours
        pour remplir les tableaux), et le bouton Deconnexion reste dans l'ecran."""
        r = requests.post(f"{primary_url}/api/alarms/send", headers=_admin_headers(primary_url), timeout=5,
                          json={"site": "izeron", "title": "Pompe de relevage en defaut", "message": "INV-150", "severity": "critical"})
        assert r.status_code == 200, r.text

        page.set_viewport_size({"width": 375, "height": 800})
        page.goto(primary_url, wait_until="domcontentloaded", timeout=15000)
        page.locator("#loginName").fill("admin")
        page.locator("#loginPassword").fill("admin123")
        page.locator("button:has-text('Se connecter')").click()
        expect(page.locator("#loginOverlay")).to_be_hidden(timeout=15000)

        logout = page.locator("button:has-text('Déconnexion')").bounding_box()
        assert logout and logout["x"] + logout["width"] <= 375, f"Deconnexion hors ecran : {logout}"

        overflowing = {}
        for tab in page.locator(".tab").all():
            tab.click()
            page.wait_for_timeout(500)
            width = page.evaluate("document.documentElement.scrollWidth")
            if width > 375:
                overflowing[tab.inner_text()] = width
        assert not overflowing, f"Onglets qui debordent a 375 px (largeur de page) : {overflowing}"
