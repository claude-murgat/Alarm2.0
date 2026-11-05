"""Logique pure : site sans modem (INV-185).

Une passerelle qui a un modem poste son etat (modem-status, INV-129/158) a chaque keepalive (60 s).
Un site dont le plus recent etat a 10 min ou plus, ou qui n'en a aucun depuis le debut de l'episode,
ne peut ni envoyer de SMS ni appeler. L'appelant (`quorum_monitor`, leader seul) garde l'etat des
episodes dans SystemConfig et envoie un email par site renvoye dans `alerts`.
"""
from datetime import datetime, timedelta

SITE_MODEM_SILENCE = timedelta(minutes=10)


def decide_site_modem(sites: list, reports: list, state: dict, now: datetime) -> tuple[tuple, dict]:
    """`sites` : sites connus ; `reports` : (site, reported_at) de chaque etat de modem connu ;
    `state` : {site: {"since", "emailed"}} des sites sans modem au tick precedent. Renvoie
    (alertes de ce tick [(site, debut de l'episode, dernier etat ou None)], nouvel etat). Un etat
    plus recent que le debut de l'episode le clot : le silence suivant est un nouvel episode."""
    freshest = {site: at for site, at in sorted(reports, key=lambda r: r[1])}  # le plus recent l'emporte
    alerts, new_state = [], {}
    for site in sites:
        last = freshest.get(site)
        if last is not None and now - last < SITE_MODEM_SILENCE:
            continue
        prev = state.get(site)
        if prev is not None and (last is None or last <= prev["since"]):
            since, emailed = prev["since"], prev["emailed"]
        else:
            since, emailed = last or now, False
        if not emailed and now - since >= SITE_MODEM_SILENCE:
            alerts.append((site, since, last))
            emailed = True
        new_state[site] = {"since": since, "emailed": emailed}
    return tuple(alerts), new_state
