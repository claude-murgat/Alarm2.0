# Wireguard peers — Alarme Murgat onsite mesh

Les clés publiques sont **non secrètes** : elles peuvent vivre dans le repo. Les clés privées
restent sur leurs machines respectives uniquement (`/etc/wireguard/privatekey` mode 600).

## Plan d'adressage

| IP Wireguard | Rôle | Hostname | Endpoint réseau |
|---|---|---|---|
| `10.99.0.1` | NODE3 cloud (hors-site) | _hostname OVH_ | `51.210.105.102:51820` |
| `10.99.0.2` | NODE1 onsite-1 (1ère baie) | onsite-1.alarm.local | `192.168.1.50:51820` |
| `10.99.0.3` | NODE2 onsite-2 (2ème baie) | onsite-2.alarm.local | `192.168.1.51:51820` |
| `10.99.0.4` | node4 alarm-1 (site beaufort, gateway) — **non votant etcd** | alarm-1 | derrière NAT `92.103.41.246`, relayé par NODE3 |
| `10.99.0.5` | node5 alarm-2 (site beaufort) — **non votant etcd** | alarm-2 | derrière NAT `92.103.41.246`, relayé par NODE3 |
| `10.99.0.6` | node6 alarm-3 (futur nœud d'Izeron, affiché site izeron) — **non votant etcd** | alarm-3 | à Beaufort jusqu'au déménagement (derrière NAT `92.103.41.246`), relayé par NODE3 |
| `10.99.0.7` | node7 alarm-4 (futur nœud d'Izeron, affiché site izeron) — **non votant etcd** | alarm-4 | à Beaufort jusqu'au déménagement (derrière NAT `92.103.41.246`), relayé par NODE3 |

> **Relais NODE3 (nœuds 4/5, 2026-10-27)** : le site beaufort ne joint pas directement le
> LAN des onsite. Tout le trafic inter-sites passe par NODE3 en étoile : NODE3 route
> `wg0 → wg0` (`net.ipv4.ip_forward=1` + forward iptables autorisé sur `wg0`), et chaque
> spoke déclare le peer NODE3 avec `AllowedIPs = 10.99.0.0/24` (au lieu de `10.99.0.1/32`).
> Seul NODE3 déclare les peers `10.99.0.4/32` à `10.99.0.7/32` (pas d'`Endpoint =`,
> apprentissage par `PersistentKeepalive` depuis le site NAT'd). Un peer plus spécifique
> (`/32`, ex. lien LAN direct onsite-1 ↔ onsite-2) reste prioritaire sur le `/24` du relais.

> **Déménagement de site (2026-06)** : les onsite sont passés du LAN `172.16.0.0/16`
> (endpoints `172.16.1.120/.121`) au LAN `192.168.1.0/24`. IP désormais **statiques et
> persistantes** (`/etc/network/interfaces`) : onsite-1 = `192.168.1.50`, onsite-2 =
> `192.168.1.51`, passerelle `192.168.1.1`. Les `Endpoint =` du peer onsite↔onsite dans
> `wg0.conf` ont été repointés en conséquence (le lien direct LAN ne montait plus sinon —
> le relais cloud NODE3, lui, n'a jamais été affecté grâce au roaming).

> **NAT** : les onsite sortent via l'IP publique du router site (mappings ports
> dynamiques). NODE3 voit donc les onsite sur cet endpoint NAT'd, pas leur IP LAN. Côté
> NODE3, **pas d'`Endpoint =`** déclaré pour les onsite (apprentissage par PersistentKeepalive
> initié depuis les onsite, toutes les 25 s).
>
> L'IP publique du site **change au déménagement** — c'est attendu, pas un signal de
> compromission. Historique vérifié par RDAP : `31.204.85.180` (ancien site) →
> `185.163.139.150` (depuis 2026-06). Les deux sont des lignes xDSL du **même FAI**
> (`srvc.fr`, maintainer `MNT-SRVC`), seul le bloc d'abonné diffère. Avant de conclure à un
> endpoint hostile sur un `wg show`, **résoudre l'IP** (`dig -x`, RDAP) : le roaming
> WireGuard fait apparaître la nouvelle IP sans aucune modification de config.

## Public keys

| IP | Public key | Date génération |
|---|---|---|
| `10.99.0.1` (NODE3) | `GDN64aY60tBSWKN4qA6GBd/JhistmDP2oF1qN0Xj9gw=` | 2026-05-02 |
| `10.99.0.2` (onsite-1) | `iO0HHo7Lbuvqs4rV6C456dSm8d+T3ef96CHA9m32CHE=` | 2026-05-06 |
| `10.99.0.3` (onsite-2) | `IdbW+fqdOhFPSIO6XRp5oego+U/shypsOllOjYHQhi8=` | 2026-05-01 |
| `10.99.0.6` (alarm-3) | `kO/o+xzXVeKNQywK4UDupDRdnM7mZ5cBYauo32OxU2A=` | 2026-11-05 |
| `10.99.0.7` (alarm-4) | `q4Lcr+AqCZBEDwyz052eWqRfY4D9Vg3gfoFi4dioFWY=` | 2026-11-05 |

## Mesh handshakes vérifiés (mesh 3-way complet)

| Date | De | Vers | RTT | Notes |
|---|---|---|---|---|
| 2026-05-08 | onsite-1 (10.99.0.2) | onsite-2 (10.99.0.3) | < 1 ms | LAN direct |
| 2026-05-09 | onsite-1 (10.99.0.2) | NODE3 (10.99.0.1) | 16.6 ms | via Internet, NAT'd, PersistentKeepalive 25s |
| 2026-05-09 | onsite-2 (10.99.0.3) | NODE3 (10.99.0.1) | 16.6 ms | via Internet, NAT'd, PersistentKeepalive 25s |
| 2026-05-09 | NODE3 (10.99.0.1) | onsite-1 (10.99.0.2) | 16.6 ms | retour bidirectionnel OK |
| 2026-05-09 | NODE3 (10.99.0.1) | onsite-2 (10.99.0.3) | 16.6 ms | retour bidirectionnel OK |
| 2026-06-19 | onsite-1 (10.99.0.2) | onsite-2 (10.99.0.3) | 0.75 ms | LAN direct **rétabli** après déménagement — endpoints repointés `172.16.1.x` → `192.168.1.5x` |
| 2026-06-19 | onsite-2 (10.99.0.3) | onsite-1 (10.99.0.2) | 0.99 ms | retour bidirectionnel OK (nouveau LAN) |

## Procédure d'ajout d'un peer

Quand un nouveau nœud est provisionné :

1. Sur le nouveau nœud (NODE_X) : récupérer la public key (`sudo cat /etc/wireguard/publickey`).
2. Ajouter une ligne au tableau ci-dessus (commit dans le repo).
3. Sur **chaque** nœud existant, ajouter le `[Peer]` correspondant dans `/etc/wireguard/wg0.conf` :
   ```
   [Peer]
   PublicKey = <NODE_X public key>
   AllowedIPs = 10.99.0.X/32
   Endpoint = <NODE_X endpoint>:51820
   PersistentKeepalive = 25     # seulement si NODE_X est derrière NAT (cloud, 4G)
   ```
4. Redémarrer wg-quick : `sudo systemctl restart wg-quick@wg0`
5. Vérifier : `sudo wg show` doit afficher un handshake récent.

---

## Tunnel façade OVH (`wg1`) — exposition publique de l'UI

**Distinct du mesh ci-dessus.** Un **2ᵉ tunnel** WireGuard (`wg1`, sous-réseau
`10.200.0.0/24`) relie chaque nœud sur site à un **VPS OVH façade à IP fixe**
(`51.77.222.190`) qui sert `https://alarme.charlesmurgat.com` (INV-199,
configuration : [`infra/facade/`](../facade/README.md)). Interface **séparée** — `wg0` (le
mesh cluster) **reste intact**, sous-réseaux disjoints (`10.99.0.0/24` vs
`10.200.0.0/24`). Roaming complet (pas d'`Endpoint` côté VPS pour les nœuds →
survit au déménagement de site).

Template : [`wg1-facade.conf.example`](wg1-facade.conf.example).
Setup reproductible : `sudo bash scripts/wg-facade-setup.sh <10.200.0.X/24>`
(crée `wg1` **sans jamais toucher `wg0`**).

### Plan d'adressage façade
| IP wg1 | Rôle | Hostname |
|---|---|---|
| `10.200.0.1/24` | VPS façade (interface **`wg0` sur le VPS**) | 51.77.222.190 |
| `10.200.0.2` à `.4` | **autres clients du VPS, pas à nous** (`.4` sert `supervision.charlesmurgat.com`, une autre application : le lien des SMS d'alarme) — ne jamais toucher | — |
| `10.200.0.5/24` | onsite-1 (alarme1) — **à retirer du VPS le jour du déménagement** | onsite-1.alarm.local |
| `10.200.0.6/24` | onsite-2 (alarme2) — idem | onsite-2.alarm.local |
| `10.200.0.7/24` | alarm-1 (node4) | alarm-1 |
| `10.200.0.8/24` | alarm-2 (node5) | alarm-2 |
| `10.200.0.9/24` | alarm-3 (node6) | alarm-3 |
| `10.200.0.10/24` | alarm-4 (node7) | alarm-4 |
| `10.200.0.11`… | machine suivante : prochaine adresse libre (`STD_FACADE_PLAN` de `standard.env` ; `.5` / `.6` jamais réattribuées) | — |

### Peer serveur (OVH façade)
| Champ | Valeur |
|---|---|
| PublicKey | `EO723NGhs9D1ZR7xPTVNUl+ZLAYYKxoQPNmXzuOWdz8=` |
| Endpoint | `51.77.222.190:51820` |
| AllowedIPs (côté nœud) | `10.200.0.0/24` |
| PersistentKeepalive | 25 |
| Côté VPS | interface `wg0`, `10.200.0.1/24`, port 51820 ; un pair `AllowedIPs 10.200.0.X/32` par client, sans `Endpoint` ; **relaie entre tous ses clients** (`PostUp` iptables `FORWARD -i wg0 -j ACCEPT`) |

### Public keys wg1 (à déclarer comme peers **côté OVH**, AllowedIPs `10.200.0.X/32`)
| IP wg1 | Public key | Date |
|---|---|---|
| `10.200.0.5` (alarme1) | `60EmMwaKi6lud6fJD1SPtgak6JQCw8kCIiPmwoQn8V8=` | 2026-06-18 |
| `10.200.0.6` (alarme2) | `5OQWBUYeaaCQkk9wt6sCMwq7n9qdT1YhEnff/zN9yAA=` | 2026-06-18 |
| `10.200.0.7` (alarm-1) | `yYk/1KeQC3w9jAcXjOXT4Pe9d5VZSrPdIzUIpuNKnyU=` | 2026-11-24 |
| `10.200.0.8` (alarm-2) | `Pm6X5tAMdmmIRJQPOfBujI8xTkKdYhgRa3v/7qS8LDQ=` | 2026-11-24 |
| `10.200.0.9` (alarm-3) | `taORhxa26CZCZvlB3QP6dn6GfKP2KVvnAeSxlYtUHm8=` | 2026-11-24 |
| `10.200.0.10` (alarm-4) | `NUuD8poiT0DyJaKIqyGpVW7GIqFaWowD1VbVt4ozihw=` | 2026-11-24 |

### App exposée
UI + API alarme = backend FastAPI sur **port `8000`**, racine `/`, sur chaque
nœud (le write-proxy INV-043/044 permet de servir l'UI depuis n'importe quel
nœud, même replica). Depuis le 2026-11-24 (INV-199) : **`https://alarme.charlesmurgat.com`**
→ Apache `balancer://alarme` sur le VPS, membres `10.200.0.5` à `.10` `:8000` et NODE3
`51.210.105.102:8000` (IP publique, pas de `wg1`), santé `/health` toutes les 10 s,
hors du pool après 2 échecs. Anciens noms `alarme1` / `alarme2.izeron.charlesmurgat.com`
→ `301` vers le nouveau.

### Notes
- **`MTU = 1280`** sur `wg1` : WG-sur-cellulaire drop les gros paquets à 1420
  (constaté 2026-06-18). 1280 passe sur fibre **et** 4G.
- **Aucune règle ufw sur `wg1`** (alarm-1 à alarm-4, retirée le 2026-11-24) : le
  VPS relaie entre tous ses clients, `ufw allow in on wg1` ouvrait SSH et le reste à
  `.2`-`.4`. Le backend reste joignable (Docker publie `8000` sur `0.0.0.0`).
  onsite-1 / onsite-2 ont encore la règle (historique, retirés au déménagement).
- Le handshake reste **vide** tant que la clé publique du nœud n'est pas déclarée
  comme pair sur le VPS.
