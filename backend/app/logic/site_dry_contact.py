"""Logique pure : site sans contact sec lisible (INV-195).

Contact sec lisible = au moins une passerelle vivante du site rapporte `open` ou `closed` (INV-122/157 ;
`no_sensor` et passerelle muette ne comptent pas, INV-194). Seuls les sites qui en ont deja eu un sont
suivis. L'episode compte depuis le premier controle qui voit le site sans contact lisible (un `no_sensor`
efface l'heure de la derniere lecture) ; plus de 10 min -> un email, retour -> « retabli ». L'appelant
(`quorum_monitor`, leader seul) garde `state` dans SystemConfig.
"""
from datetime import datetime, timedelta

SITE_DRY_CONTACT_LOST_AFTER = timedelta(minutes=10)


def decide_site_dry_contact(sites: list, gateways: list, state: dict, now: datetime,
                            liveness: timedelta) -> tuple[tuple, tuple, dict]:
    """`sites` : sites connus ; `gateways` : (site, etat, last_seen) de chaque passerelle ; `state` :
    {site: {"since", "emailed"}} des sites suivis (`since` None : lisible) ; `liveness` : fenetre de
    vivacite. Renvoie (perdus [(site, since)], retablis [(site, since)], nouvel etat)."""
    lost, recovered, new_state = [], [], {}
    for site in sites:
        read_at = [at for s, st, at in gateways if s == site and st in ("open", "closed")]
        prev = state.get(site)
        if any(now - at < liveness for at in read_at):
            if prev and prev["emailed"]:
                recovered.append((site, prev["since"]))
            new_state[site] = {"since": None, "emailed": False}
            continue
        if prev is None and not read_at:
            continue  # jamais eu de contact sec lisible : pas suivi
        since, emailed = (prev["since"], prev["emailed"]) if prev and prev["since"] else (now, False)
        if not emailed and now - since > SITE_DRY_CONTACT_LOST_AFTER:
            lost.append((site, since))
            emailed = True
        new_state[site] = {"since": since, "emailed": emailed}
    return tuple(lost), tuple(recovered), new_state
