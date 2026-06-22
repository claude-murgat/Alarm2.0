"""
Test E2E failback sur cluster 3 noeuds Patroni (la stack de prod).

Refonte chantier #21 step 4 (post-decouverte 2026-05-06) : la version
2-node primary/standby de ce test ciblait `docker-compose.vps2.yml`,
un compose legacy de l'ere "two independent compose instances + streaming
replication" (commits bacd4ed + a25a680) qui n'a JAMAIS ete mis a jour
apres la migration vers Patroni 3-node (commit 2733643). En consequence
le tier4 nightly etait rouge depuis le 2026-05-03 (db-standby-init
cherchait un service `db` que Patroni n'expose plus).

Prod = 3 noeuds Patroni, cf docs/architecture_option_B_3vps_patroni.md.
Topologie : 1 baie serveur (NODE1) + 2 VPS cloud (NODE2, NODE3), chacun
avec etcd + Patroni + backend, formant un consensus a 3 et exposant
3 backends sur 8000/8001/8002. L'app Android (ApiClient.kt) rotate
sur ces 3 URLs (PRIMARY/FALLBACK/FALLBACK_2) en cas d'echec consecutif.

Ce que le test valide (INV-043 revise 2026-06-17 : "heartbeat sur replica ->
proxy au leader (200), continuite des heartbeats a travers un failover") :
- Stop le projet du leader Patroni courant
- Patroni elit un nouveau leader parmi les 2 noeuds restants
- Les backends survivants servent le heartbeat (le nouveau leader en direct,
  OU un replica qui forwarde au nouveau leader via WG -> 200)
- Les heartbeats reprennent sur un noeud survivant (peu importe lequel : la
  garantie est la CONTINUITE, plus l'atterrissage sur l'URL exacte du leader)
- L'app peut encore rotater si elle tape un noeud carrement down (connexion
  refusee) ou si aucun leader n'est joignable (panne cluster -> 503)

Hors scope tier 3 actuellement : `--ignore=tests/test_failback.py` dans
pr.yml. Le test orchestre 3 emulateurs Android via ADB, or les runners CI
self-hosted Docker n'ont pas d'emulateurs. Le tier 4 nightly (workflow
tier4-failback.yml) tourne sur un runner Linux dedie avec emulateurs.

Prerequis (run manuel) :
- Docker Compose 3-node Patroni up (cf CLAUDE.md "Lancer le cluster complet")
  Project names node1/node2/node3, ports 8000/8001/8002.
- Au moins 2 emulateurs Android sur 3 (5552/5554/5556) avec l'app installee
- Variables env (toutes optionnelles, defaults sensibles) :
    ADB_PATH (defaut: 'adb' dans le PATH)
    ALARM_REPO_ROOT (defaut: cwd, racine repo Alarm2.0)
    ALARM_VPS1_URL (defaut: http://localhost:8000) — backend node1
    ALARM_VPS2_URL (defaut: http://localhost:8001) — backend node2
    ALARM_VPS3_URL (defaut: http://localhost:8002) — backend node3

Run manuel :
    pytest tests/test_failback.py -v -s
"""
import os
import subprocess
import tempfile
import time
import uuid

import pytest
import requests


# --- Configuration via env vars (plus de hardcoded Windows path) ---
ADB = os.environ.get("ADB_PATH", "adb")
CWD = os.environ.get("ALARM_REPO_ROOT", os.getcwd())
VPS1 = os.environ.get("ALARM_VPS1_URL", "http://localhost:8000")
VPS2 = os.environ.get("ALARM_VPS2_URL", "http://localhost:8001")
VPS3 = os.environ.get("ALARM_VPS3_URL", "http://localhost:8002")

# Mapping URL -> docker compose project name (pour stop/start le bon noeud).
# Les .env.node{1,2,3} sont les fichiers de config par noeud, et le project
# name docker compose porte le meme nom (cf CLAUDE.md commandes cluster).
NODES = [
    {"url": VPS1, "project": "node1", "label": "NODE1"},
    {"url": VPS2, "project": "node2", "label": "NODE2"},
    {"url": VPS3, "project": "node3", "label": "NODE3"},
]

ALL_EMUS = ["emulator-5552", "emulator-5554", "emulator-5556"]
USERS_CREDS = {
    "emulator-5552": ("user1", "user123"),
    "emulator-5554": ("user2", "user123"),
    "emulator-5556": ("admin", "admin123"),
}
MIN_WORKING_EMUS = 2

# Marqueurs : `failover` (compatible --skip-failover existant)
# + `chaos` (tier 4 nightly, sera promu tier 3 bloquant lorsque l'infra
# emulateurs sera disponible sur les runners self-hosted).
pytestmark = [pytest.mark.failover, pytest.mark.chaos]


# --- Helpers (logique inchangee depuis script original + helpers de #36) ---

def adb(serial, *args):
    cmd = [ADB, "-s", serial] + list(args)
    r = subprocess.run(cmd, capture_output=True, timeout=15)
    return r.stdout.decode("utf-8", errors="replace")


def login(name, pwd, base=None):
    base = base or VPS1
    r = requests.post(f"{base}/api/auth/login", json={"name": name, "password": pwd}, timeout=5)
    r.raise_for_status()
    d = r.json()
    return d["access_token"], d["user"]["id"], d["user"]["name"]


def inject_prefs(serial, token, name, uid):
    xml = f"""<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <string name="token">{token}</string>
    <string name="user_name">{name}</string>
    <int name="user_id" value="{uid}" />
    <string name="device_token">{uuid.uuid4()}</string>
</map>"""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False)
    tmp.write(xml); tmp.close()
    adb(serial, "push", tmp.name, "/data/local/tmp/alarm_prefs.xml")
    adb(serial, "shell", "run-as com.alarm.critical mkdir -p shared_prefs")
    adb(serial, "shell", "run-as com.alarm.critical cp /data/local/tmp/alarm_prefs.xml shared_prefs/alarm_prefs.xml")
    adb(serial, "shell", "rm /data/local/tmp/alarm_prefs.xml")
    os.unlink(tmp.name)


def get_ui_text(serial):
    adb(serial, "shell", "uiautomator dump /sdcard/window_dump.xml")
    return adb(serial, "shell", "cat /sdcard/window_dump.xml")


def healthy(base):
    try:
        return requests.get(f"{base}/health", timeout=2).status_code == 200
    except Exception:
        return False


def docker(*args):
    """Run docker command depuis le repo root (CWD configurable). Log full stderr
    + exit code a l'erreur (non-tronque) pour simplifier le debug en CI/local."""
    r = subprocess.run(list(args), capture_output=True, timeout=30, cwd=CWD, text=True)
    if r.returncode != 0:
        print(f"  [docker] ERREUR (exit={r.returncode}): {' '.join(args)}")
        print(f"  stderr: {r.stderr}")
    return r.returncode


def wait_healthy(base, timeout=30):
    for _ in range(timeout):
        if healthy(base):
            return True
        time.sleep(1)
    return False


def wait_unhealthy(base, timeout=10):
    """Polling : retourne True quand le backend ne repond plus.
    Substitue les sleep(3) aveugles post-stop par une condition observable."""
    for _ in range(timeout):
        if not healthy(base):
            return True
        time.sleep(1)
    return False


def wait_users_heartbeating(base, expected_count, max_age_seconds=5.0, timeout=20.0):
    """Polling sur /api/test/connected-users-detailed (chantier #21 step 0a).
    Retourne le nombre d'users heartbeating sur `base` depuis < max_age_seconds."""
    deadline = time.time() + timeout
    last_count = 0
    while time.time() < deadline:
        try:
            r = requests.get(f"{base}/api/test/connected-users-detailed", timeout=3)
            users = r.json().get("users", [])
            recent = [
                u for u in users
                if u.get("age_seconds") is not None and u["age_seconds"] < max_age_seconds
            ]
            last_count = len(recent)
            if last_count >= expected_count:
                return last_count
        except Exception:
            pass
        time.sleep(0.5)
    return last_count


def check_emu_network(serial):
    """Verifie que l'emulateur peut joindre 10.0.2.2 (host)."""
    adb(serial, "reverse", "--remove-all")
    # 3 ports backend exposes par les 3 projets node1/node2/node3 (cf NODES).
    adb(serial, "reverse", "tcp:8000", "tcp:8000")
    adb(serial, "reverse", "tcp:8001", "tcp:8001")
    adb(serial, "reverse", "tcp:8002", "tcp:8002")
    out = adb(serial, "shell", "ping -c 1 -W 2 10.0.2.2")
    return "1 received" in out or "1 packets received" in out


def get_role(url):
    """Renvoie le role Patroni du backend (primary, replica, ?). None si injoignable."""
    try:
        return requests.get(f"{url}/health", timeout=2).json().get("role")
    except Exception:
        return None


def find_leader(urls=None, timeout=60.0):
    """Polling : attend qu'UN des `urls` ait role=primary. Renvoie l'URL ou None.

    Conforme a la mini-regle "blind sleeps to observable polling" (chantier #21
    step 2) : on observe l'etat Patroni reel via /health, pas un timer fixe.
    """
    urls = urls if urls is not None else [n["url"] for n in NODES]
    deadline = time.time() + timeout
    while time.time() < deadline:
        for url in urls:
            if get_role(url) == "primary":
                return url
        time.sleep(0.5)
    return None


def node_for(url):
    """Renvoie le dict NODES correspondant a l'URL, ou None."""
    for n in NODES:
        if n["url"] == url:
            return n
    return None


def wait_users_heartbeating_any(urls, expected_count, max_age_seconds=10.0, timeout=60.0):
    """Polling sur N URLs en parallele : retourne (url, count) des qu'UN backend
    voit `expected_count` users heartbeating. Utile post-failover quand on ne sait
    pas encore quel noeud Patroni a elu leader.

    Aucun sleep aveugle : on poll a 0.5Hz les endpoints observables, sortie
    immediate des que la condition est remplie."""
    deadline = time.time() + timeout
    last = (None, 0)
    while time.time() < deadline:
        for url in urls:
            try:
                r = requests.get(f"{url}/api/test/connected-users-detailed", timeout=3)
                users = r.json().get("users", [])
                recent = [
                    u for u in users
                    if u.get("age_seconds") is not None and u["age_seconds"] < max_age_seconds
                ]
                if len(recent) >= expected_count:
                    return (url, len(recent))
                if len(recent) > last[1]:
                    last = (url, len(recent))
            except Exception:
                pass
        time.sleep(0.5)
    return last


def app_connected(serial):
    xml = get_ui_text(serial)
    return "connexion" in xml.lower() and "ok" in xml.lower()


# --- Fixtures ---

@pytest.fixture(scope="module")
def working_emulators():
    """Emulateurs avec reseau OK. Skip pytest si < MIN_WORKING_EMUS."""
    print("\n[fixture] Pre-flight : check reseau emulateurs...")
    emus = []
    for serial in ALL_EMUS:
        ok = check_emu_network(serial)
        status = "OK" if ok else "RESEAU CASSE"
        print(f"  {serial}: {status}")
        if ok:
            emus.append(serial)

    if len(emus) < MIN_WORKING_EMUS:
        pytest.skip(
            f"Seulement {len(emus)} emulateurs fonctionnels "
            f"(minimum {MIN_WORKING_EMUS})"
        )

    print(f"  {len(emus)} emulateurs fonctionnels: {emus}")
    return emus


@pytest.fixture(scope="module")
def cluster_running():
    """Setup : 3 noeuds Patroni UP et reset. Teardown : remonte tout noeud
    laisse DOWN par un test (succes OU echec).

    Le cluster est CENSE etre deja boote en amont (workflow tier4-failback.yml
    step "Boot cluster" en CI ; cf CLAUDE.md "Lancer le cluster complet" en
    local). La fixture se contente de :
      1. Reveiller (`compose start`) chaque project node1/node2/node3 — sans
         effet si deja running, idempotent.
      2. Verifier que les 3 backends sont healthy (200 sur /health).
      3. Identifier le leader Patroni courant via /health (role=primary).
      4. Reset l'etat applicatif (alarmes, horloge) via le leader.

    Le teardown reveille chaque project si stoppe par un test, pour rendre
    le cluster a son etat baseline avant le prochain test du module.
    """
    print("\n[fixture] Ensure 3 nodes UP...")
    # `compose -p X start` (sans service) reveille TOUS les services du projet
    # (etcd + patroni + backend). Si un test precedent a stoppe le project
    # entier (kill leader scenario), les 3 services doivent etre relances —
    # sinon backend depend de patroni depend de etcd, et un seul service
    # demarre rentre en boucle de retry.
    for n in NODES:
        docker("docker", "compose", "-p", n["project"], "start")

    for n in NODES:
        assert wait_healthy(n["url"], 60), f"{n['label']} ({n['url']}) must be healthy at setup"

    leader = find_leader(timeout=30.0)
    assert leader is not None, "Aucun leader Patroni trouve apres 30s — cluster KO"
    print(f"  Leader Patroni courant : {node_for(leader)['label']} ({leader})")
    for n in NODES:
        role = get_role(n["url"])
        print(f"  {n['label']}: {role}")

    requests.post(f"{leader}/api/test/reset", timeout=5)

    yield {"nodes": NODES, "leader_initial": leader}

    # Teardown : reveille TOUS les services de chaque project stoppe par un
    # test (`start` sans service = etcd + patroni + backend). Voir commentaire
    # symetrique du setup.
    print("\n[fixture cleanup] Restore cluster baseline state...")
    for n in NODES:
        docker("docker", "compose", "-p", n["project"], "start")
    for n in NODES:
        wait_healthy(n["url"], 60)
    # Reset applicatif sur le leader courant (peut avoir change apres un test
    # qui a kill l'ancien leader).
    new_leader = find_leader(timeout=30.0)
    if new_leader is not None:
        try:
            requests.post(f"{new_leader}/api/test/reset", timeout=5)
        except Exception:
            pass


@pytest.fixture
def emulators_connected(working_emulators, cluster_running):
    """Apps Android logged in et confirmees heartbeating sur LE LEADER courant.

    En 3-node Patroni, le leader peut etre node1/node2/node3 selon l'ordre
    d'election au boot (souvent node1 mais pas garanti). L'ApiClient demarre
    sur PRIMARY_BACKEND_URL=8000 ; meme si node1 est replica, le heartbeat
    aboutit (forwarde au leader via WG, cf INV-043 revise). On observe
    l'arrivee des heartbeats sur N'IMPORTE QUEL des 3 backends via
    wait_users_heartbeating_any.

    Le `time.sleep(1)` apres `am force-stop` est un settling delay OS (plus
    de delais SQLite WAL pendant que Android nettoie le process), pas un
    sleep aveugle de test — il doit rester court. Helpers polling utilises
    pour les attentes liees au backend (mini-regle "blind sleeps to
    observable polling", chantier #21 step 2).
    """
    leader_url = cluster_running["leader_initial"]
    print(f"\n[fixture] Setup apps (leader Patroni : {node_for(leader_url)['label']})...")
    for serial in working_emulators:
        name, pwd = USERS_CREDS[serial]
        adb(serial, "shell", "am force-stop com.alarm.critical")
        time.sleep(1)
        # On login sur le leader, peu importe lequel — login va a la base
        # via le primaire de Patroni.
        tok, uid, _uname = login(name, pwd, base=leader_url)
        inject_prefs(serial, tok, name, uid)
        adb(serial, "shell", "am start -n com.alarm.critical/.MainActivity")
        print(f"  {name} sur {serial}")

    print("  Attente heartbeats (polling observable, n'importe quel noeud)...")
    all_urls = [n["url"] for n in NODES]
    url, n_hb = wait_users_heartbeating_any(
        all_urls, expected_count=len(working_emulators),
        max_age_seconds=5.0, timeout=30.0,
    )
    assert n_hb >= len(working_emulators), (
        f"Seulement {n_hb}/{len(working_emulators)} apps heartbeating apres 30s "
        f"(meilleur observe sur {url}). Les apps ne se sont pas connectees au cluster."
    )
    print(f"  → {n_hb}/{len(working_emulators)} apps heartbeatent sur {node_for(url)['label']} ({url})")

    return working_emulators


# --- Tests ---

def test_failback_kill_leader(working_emulators, cluster_running, emulators_connected):
    """Stop le projet du leader Patroni courant -> les apps doivent continuer
    a heartbeater (sur le nouveau leader, en direct ou via proxy depuis un
    replica survivant).

    Materialise INV-043 revise ("heartbeat sur replica -> proxy au leader,
    continuite a travers un failover") en condition Patroni 3-node reelle :
    1. Identifier le leader courant via /health (role=primary).
    2. `docker compose -p <leader_project> stop` (project entier — sinon Patroni
       reste leader et l'ancien leader continue de repondre).
    3. Patroni sur les 2 noeuds restants forme quorum a 2 (a partir de 3 etcd
       initiaux, perdre 1 garde majorite) et elit un nouveau leader.
    4. Le backend du nouveau leader passe role=primary ; les replicas survivants
       forwardent le heartbeat vers lui (via WG) -> 200.
    5. L'app peut devoir rotater une fois (l'ancien leader tape est down =
       connexion refusee, consecutiveFailures>=3) mais des qu'elle atteint un
       noeud survivant, le heartbeat repasse 200 (direct ou proxifie).
    """
    expected = len(working_emulators)
    initial_leader = cluster_running["leader_initial"]
    leader_node = node_for(initial_leader)
    print(f"\n[1] Leader Patroni initial : {leader_node['label']} ({initial_leader})")

    # --- Stop le project du leader courant (project entier pour aussi virer
    #     etcd et patroni, sinon patroni reste actif comme leader avec son etcd
    #     et l'ancien leader continue de servir jusqu'a son retrait).
    other_nodes = [n for n in NODES if n["url"] != initial_leader]
    print(f"\n[2] Stop projet {leader_node['project']} entier (force election Patroni)...")
    docker("docker", "compose", "-p", leader_node["project"], "stop")

    # Backend du leader doit etre injoignable rapidement
    assert wait_unhealthy(initial_leader, timeout=20), \
        f"{leader_node['label']} backend doit etre down apres compose stop"

    # --- Attendre l'election d'un nouveau leader parmi les 2 restants
    print(f"\n[3] Attente election nouveau leader parmi {[n['label'] for n in other_nodes]}...")
    new_leader = find_leader(urls=[n["url"] for n in other_nodes], timeout=60.0)
    assert new_leader is not None, (
        "Aucun nouveau leader Patroni elu apres 60s. "
        "Verifier : etcd quorum (2/3 minimum), patroni health check."
    )
    new_leader_node = node_for(new_leader)
    print(f"  → Nouveau leader : {new_leader_node['label']} ({new_leader})")

    # --- Verifier que les apps rotatent vers le nouveau leader
    # Note : selon que le nouveau leader est URL[1] (VPS2) ou URL[2] (VPS3),
    # l'ApiClient passera par 1 ou 2 rotations. Les deux cas valident
    # l'invariant : le test reste en succes des que les heartbeats sont
    # observes sur le nouveau leader.
    print(f"\n[4] Polling heartbeats sur {new_leader_node['label']} (apps doivent rotater)...")
    all_urls = [n["url"] for n in NODES]
    landed_url, n_hb = wait_users_heartbeating_any(
        all_urls, expected_count=expected,
        max_age_seconds=10.0, timeout=120.0,  # 120s : marge pour rotations multiples
    )

    if n_hb < expected:
        # Diagnostic detaille avant fail.
        print("\n  ECHEC: failover pas complet apres 120s")
        print(f"  Meilleur observe : {n_hb}/{expected} sur {landed_url}")
        for n in NODES:
            print(f"  {n['label']} role: {get_role(n['url'])}")
        print("  LOGCAT (extrait ApiClient + AlarmPollingService) :")
        for serial in working_emulators:
            print(f"\n  === {serial} ===")
            print(adb(serial, "logcat", "-d", "-t", "50",
                      "-s", "ApiClient:*", "AlarmPollingService:*"))
        pytest.fail(
            f"Failover incomplet : {n_hb}/{expected} apps seulement sur "
            f"{landed_url} apres 120s. Nouveau leader Patroni : {new_leader}."
        )

    print(f"\n  FAILOVER OK : {n_hb}/{expected} apps sur {landed_url} "
          f"(leader Patroni : {new_leader})")
    # INV-043 (revise 2026-06-17) : un replica FORWARDE le heartbeat au leader
    # (via WG) et renvoie 200 — il ne renvoie plus 503. Donc apres le failover,
    # les apps peuvent heartbeater avec succes sur N'IMPORTE quel noeud survivant
    # (le nouveau leader en direct, OU un replica qui proxy vers lui). La
    # garantie de failback est la CONTINUITE des heartbeats (deja verifiee par
    # n_hb >= expected ci-dessus), pas le fait d'atterrir sur l'URL exacte du
    # leader. On n'exige donc plus landed_url == new_leader (ce serait l'ancien
    # modele 503-rotation). On verifie juste que le landing est un noeud encore
    # debout (pas l'ancien leader qu'on a stoppe).
    assert landed_url != initial_leader, (
        f"Apps heartbeatent sur {landed_url} = ancien leader stoppe : impossible, "
        "le failover n'a pas eu lieu."
    )


@pytest.mark.failover
@pytest.mark.chaos
@pytest.mark.parametrize("service", ["patroni", "etcd"])
def test_container_auto_recovery_after_crash(cluster_running, service):
    """Verrouille en regression la restart policy `unless-stopped` sur les
    conteneurs etcd + patroni ajoutee par PR #184 (issue #186 / INV-094 etendu).

    Bug observe le 2026-06-18 : apres reboot d'un noeud onsite, etcd et patroni
    restaient `Exited` (defaut `restart: no`) ; il fallait `docker start <c>` a
    la main pour rejoindre le cluster. PR #184 a ajoute `restart: unless-stopped`
    aux deux services. Sans ce test tier 4, aucune assertion CI ne previendrait
    une regression future (ex. deploiement CD qui recree un conteneur sans la
    politique). Sa presence ferme la boucle sur PR #184.

    Comment on declenche le restart automatique :

    On simule un crash inattendu via `docker exec --user root <c> kill -KILL 1`
    (SIGKILL sur le process PID 1 = entrypoint = etcd ou patroni). Docker traite
    ca comme un exit non-volontaire et applique la restart policy → le conteneur
    redemarre seul.

    Pourquoi pas `docker stop` ou `docker kill` depuis l'exterieur (comme suggere
    dans le body de l'issue) ? Verification empirique : ces deux gestes sont
    traites comme STOP MANUEL par `unless-stopped` ; le conteneur reste down.
    C'est d'ailleurs ce qu'on veut pour les tests failover tier3/tier4 qui font
    `docker compose stop` et comptent dessus pour laisser le noeud stoppe.
    Seul un exit interne (kill PID 1 depuis l'interieur, OOM, segfault, host
    reboot) declenche la restart policy.

    Pourquoi parametrer sur patroni + etcd : les deux ont recu le meme fix dans
    PR #184 (meme mecanisme), le bug initial portait sur patroni mais etcd avait
    le meme defaut. Couverture symetrique pour le prix d'un.

    Tier 4 uniquement (chaos marker) : manipule la stack Docker locale, exclu du
    tier 3 par `--ignore=tests/test_failback.py` dans pr.yml.
    """
    # Restaurer baseline : si un test precedent (test_failback_kill_leader) a
    # laisse un projet compose stoppe, on le remonte avant de cibler.
    for n in NODES:
        docker("docker", "compose", "-p", n["project"], "start")
    for n in NODES:
        assert wait_healthy(n["url"], 60), \
            f"{n['label']} ({n['url']}) doit etre healthy avant le test"

    # Cibler un noeud non-leader (perturbation minimale). Si pas de leader
    # joignable (race rare), fallback sur NODES[0] : le test reste valide,
    # on verifie une propriete locale au conteneur, pas une propriete cluster.
    leader_url = find_leader(timeout=10.0)
    target_node = next((n for n in NODES if n["url"] != leader_url), NODES[0])
    container = f"{target_node['project']}-{service}-1"
    print(f"\n[1] Cible : {container} (noeud non-leader)")

    r = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Status}}|{{.RestartCount}}", container],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 0, f"docker inspect echoue : {r.stderr.strip()}"
    status, count_str = r.stdout.strip().split("|")
    initial_count = int(count_str)
    assert status == "running", \
        f"{container} doit etre Running avant le test (actuel : {status!r})"
    print(f"  Etat initial : Running, RestartCount={initial_count}")

    # Crash simule : kill PID 1 depuis l'interieur via root. `docker exec` peut
    # renvoyer non-zero (le conteneur meurt pendant la commande) — on n'asserte
    # pas dessus, on verifie l'effet (auto-restart) plus bas.
    print(f"\n[2] Simule crash : docker exec --user root {container} kill -KILL 1")
    subprocess.run(
        ["docker", "exec", "--user", "root", container, "kill", "-KILL", "1"],
        capture_output=True, text=True, timeout=15,
    )

    # Attendre Running + RestartCount > initial (max 60s, couvre le backoff
    # exponentiel Docker meme si plusieurs restarts s'enchainent).
    print(f"\n[3] Attente auto-restart (status=running ET RestartCount>{initial_count}, max 60s)...")
    deadline = time.time() + 60.0
    final_status, final_count = None, initial_count
    while time.time() < deadline:
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}|{{.RestartCount}}", container],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            s, c = r.stdout.strip().split("|")
            final_status, final_count = s, int(c)
            if s == "running" and final_count > initial_count:
                break
        time.sleep(1)

    assert final_status == "running", (
        f"{container} pas Running apres 60s (status={final_status!r}). "
        f"Le conteneur ne s'est PAS auto-redemarre apres crash. "
        f"Verifier `restart: unless-stopped` sur service {service} dans docker-compose.yml."
    )
    assert final_count > initial_count, (
        f"{container} status=running mais RestartCount={final_count} (initial={initial_count}). "
        "Le kill -KILL 1 n'a pas declenche un restart par Docker — soit le kill n'a pas "
        "atteint PID 1 (verifier l'entrypoint), soit la restart policy est inactive."
    )
    print(f"  → Auto-recovery OK : Running, RestartCount={final_count} (etait {initial_count})")

    # Pour patroni : verifier le retour fonctionnel dans le cluster (role
    # primary ou replica via /health du backend). Detecte un faux positif ou
    # le conteneur tourne mais patroni n'arrive pas a se resync.
    if service == "patroni":
        print(f"\n[4] Verification role Patroni sur {target_node['label']} (max 60s)...")
        deadline = time.time() + 60.0
        role = None
        while time.time() < deadline:
            role = get_role(target_node["url"])
            if role in ("primary", "replica"):
                break
            time.sleep(1)
        assert role in ("primary", "replica"), (
            f"Patroni sur {target_node['label']} n'a pas retrouve de role apres 60s "
            f"(role={role!r}). Conteneur up mais Patroni n'a pas rejoint le cluster."
        )
        print(f"  → Patroni role={role}, cluster OK.")
