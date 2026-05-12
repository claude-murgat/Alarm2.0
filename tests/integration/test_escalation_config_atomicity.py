"""
Tier 2 integration test : INV-082 — POST /api/config/escalation/bulk est atomique.

Couvre l'invariant INV-082 (tests/INVARIANTS.md) : DELETE + INSERTs s'executent
dans une transaction SQL unique. Un GET /api/config/escalation concurrent ne doit
JAMAIS observer une chaine vide tant que la chaine precedente etait non-vide.

Pourquoi c'est critique : si la modif de chaine etait fragmentee en 2 commits
(ex: db.commit() apres le DELETE), une fenetre ~ms exposerait 0 ligne. Pendant
cette fenetre, toute alarme entrante (POST /alarms/send) verrait "chaine vide",
declencherait INV-080 (email direction technique + fallback user) — alarme mal
routee + email parasite, juste parce qu'un admin modifiait la chaine au mauvais
moment.

Etat actuel (audit 2026-04-20) : code deja atomique
(`SessionLocal(autocommit=False, autoflush=False)` + un seul `db.commit()` dans
`save_escalation_chain_bulk`). Le but de ce test est de **verrouiller** la
propriete en regression : si quelqu'un casse l'atomicite (split en 2 commits,
nouvelle session entre DELETE et INSERT, etc.), il doit fail immediatement en CI.

Sensibilite verifiee mentalement : injecter `db.commit()` apres
`db.query(EscalationConfig).delete()` (config.py:161) fait qu'entre les 2
commits, la table est physiquement vide sur disque. Un reader concurrent
attrape l'etat vide -> assertion explose.

Note : un test equivalent existe en tier 3 (`tests/test_e2e.py::TestInv082ConcurrentBulk`)
contre cluster live. Cette version tier 2 est plus rapide (~1-3s vs 7 min), tourne
contre SQLite + TestClient, et ne necessite pas Docker — elle fait partie de la
defense en profondeur (l'autre tomberait si un refactor cassait `/test/reset` ou
le cluster).

Budget P4 : 1 test cible.
"""
import threading
import time

import pytest

pytestmark = pytest.mark.integration


# Charge calibree pour exposer la race en quelques secondes max sur SQLite +
# TestClient. Si l'atomicite est cassee, meme une seule des ~600 lectures
# coincidant avec une fenetre intermediate suffit a fail.
WRITER_ITERATIONS = 30
READER_ITERATIONS = 200
READER_THREADS = 3
TEST_BUDGET_SECONDS = 30.0


def _get_chain_user_ids(client):
    """Retourne la liste ordonnee des user_id de la chaine actuelle."""
    r = client.get("/api/config/escalation")
    assert r.status_code == 200, r.text
    return [e["user_id"] for e in r.json()]


def _set_chain(client, admin_headers, user_ids):
    """Force la chaine a un etat connu via POST /bulk."""
    r = client.post(
        "/api/config/escalation/bulk",
        json={"user_ids": user_ids},
        headers=admin_headers,
    )
    assert r.status_code == 200, f"setup /bulk failed: {r.status_code} {r.text}"


def test_bulk_atomic_no_empty_chain_observed_under_concurrent_reads(client, admin_headers):
    """INV-082 : sous charge concurrente (1 writer x N readers), aucun GET ne
    voit la chaine vide. Verrouille l'atomicite DELETE+INSERTs.

    Cause attendue d'echec en cas de regression : `save_escalation_chain_bulk`
    fragmente sa transaction (split commit, nouvelle session, etc.). Pointer vers
    `backend/app/api/config.py:save_escalation_chain_bulk`.
    """
    # 1) Etat de depart connu : il faut une chaine d'au moins 2 entrees pour
    #    pouvoir alterner deux orderings differents.
    initial_chain = _get_chain_user_ids(client)
    if len(initial_chain) < 2:
        # Reset si un test precedent a vide / partiellement modifie la chaine.
        r = client.post("/api/test/reset")
        assert r.status_code == 200, f"/test/reset failed: {r.status_code} {r.text}"
        initial_chain = _get_chain_user_ids(client)
    assert len(initial_chain) >= 2, (
        f"Setup invalide : besoin >=2 entrees, chaine actuelle: {initial_chain}"
    )

    # 2) Deux orderings non vides — peu importe le contenu, ce qui compte c'est
    #    qu'a chaque iteration le writer fasse un cycle complet DELETE+INSERTs.
    ordering_a = initial_chain[:]
    ordering_b = list(reversed(initial_chain))
    assert ordering_a != ordering_b, "orderings doivent differer pour exercer DELETE+INSERT"

    empty_observations = []   # GETs ayant vu une liste vide -> violation INV-082
    short_observations = []   # GETs ayant vu une chaine non-vide mais < len(initial)
                              # — symptome d'un INSERTs partiellement visible
                              # (fragmentation potentielle)
    reader_errors = []
    writer_errors = []
    stop = threading.Event()

    expected_len = len(initial_chain)

    def reader(thread_idx: int):
        try:
            for i in range(READER_ITERATIONS):
                if stop.is_set():
                    return
                rr = client.get("/api/config/escalation")
                if rr.status_code != 200:
                    reader_errors.append(
                        f"reader{thread_idx} iter {i}: HTTP {rr.status_code} {rr.text[:200]}"
                    )
                    continue
                data = rr.json()
                if not isinstance(data, list):
                    reader_errors.append(
                        f"reader{thread_idx} iter {i}: reponse non-liste: {data!r}"
                    )
                    continue
                if len(data) == 0:
                    empty_observations.append({"thread": thread_idx, "iter": i})
                elif len(data) < expected_len:
                    short_observations.append(
                        {"thread": thread_idx, "iter": i, "len": len(data)}
                    )
        except Exception as exc:
            reader_errors.append(f"reader{thread_idx} crashed: {exc!r}")
            stop.set()

    def writer():
        try:
            for i in range(WRITER_ITERATIONS):
                if stop.is_set():
                    return
                user_ids = ordering_a if i % 2 == 0 else ordering_b
                rr = client.post(
                    "/api/config/escalation/bulk",
                    json={"user_ids": user_ids},
                    headers=admin_headers,
                )
                if rr.status_code != 200:
                    writer_errors.append(
                        f"writer iter {i}: HTTP {rr.status_code} {rr.text[:200]}"
                    )
                    return
        except Exception as exc:
            writer_errors.append(f"writer crashed: {exc!r}")
            stop.set()

    threads = [threading.Thread(target=reader, args=(idx,), name=f"inv082-reader-{idx}")
               for idx in range(READER_THREADS)]
    threads.append(threading.Thread(target=writer, name="inv082-writer"))

    t_start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        remaining = max(0.1, TEST_BUDGET_SECONDS - (time.monotonic() - t_start))
        t.join(timeout=remaining)
    for t in threads:
        assert not t.is_alive(), f"Thread {t.name} n'a pas termine dans le budget"

    # 3) Restauration de l'etat seed pour ne pas polluer les autres tests
    #    (pytest-randomly + fixture client session-scope).
    try:
        _set_chain(client, admin_headers, initial_chain)
    except AssertionError:
        # Si la restauration via /bulk echoue (ex: doublons), on tente reset.
        client.post("/api/test/reset")

    # 4) Pas d'erreur transport/HTTP — si l'API repond mal sous concurrence,
    #    c'est aussi un bug a remonter avant meme de parler d'INV-082.
    assert not reader_errors, f"readers reported errors: {reader_errors[:5]}"
    assert not writer_errors, f"writer reported errors: {writer_errors[:5]}"

    # 5) L'assertion centrale : INV-082.
    assert not empty_observations, (
        f"INV-082 viole : {len(empty_observations)} GET(s) ont observe une chaine "
        f"VIDE pendant qu'un autre POST /bulk tournait, alors que la chaine de "
        f"depart contenait {expected_len} entrees. "
        f"Premieres observations : {empty_observations[:3]}. "
        f"Cause probable : DELETE et INSERTs ne sont plus dans la meme transaction. "
        f"Verifier `save_escalation_chain_bulk` dans backend/app/api/config.py — un "
        f"db.commit() supplementaire apres la ligne `db.query(EscalationConfig).delete()` "
        f"introduit cette fenetre."
    )

    # 6) Defense en profondeur : si les INSERTs sont visibles avant que tous
    #    aient ete commites (fragmentation par-ligne), on verrait une chaine
    #    plus courte que prevu. Notable mais moins grave que vide.
    assert not short_observations, (
        f"INV-082 (fragmentation partielle) : {len(short_observations)} GET(s) ont "
        f"observe une chaine plus courte que prevu ({expected_len} entrees). "
        f"Premieres observations : {short_observations[:3]}. "
        f"Cause probable : flush/commit par-INSERT au lieu d'un seul commit final."
    )
