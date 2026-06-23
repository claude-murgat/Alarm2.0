# Wireguard peers — Alarme Murgat onsite mesh

Les clés publiques sont **non secrètes** : elles peuvent vivre dans le repo. Les clés privées
restent sur leurs machines respectives uniquement (`/etc/wireguard/privatekey` mode 600).

## Plan d'adressage

| IP Wireguard | Rôle | Hostname | Endpoint réseau |
|---|---|---|---|
| `10.99.0.1` | NODE3 cloud (hors-site) | _hostname OVH_ | `51.210.105.102:51820` |
| `10.99.0.2` | NODE1 onsite-1 (1ère baie) | onsite-1.alarm.local | `172.16.1.121:51820` |
| `10.99.0.3` | NODE2 onsite-2 (2ème baie) | onsite-2.alarm.local | `172.16.1.120:51820` |

> **NAT** : les onsite sortent via l'IP publique `31.204.85.180` du router site (mappings ports
> dynamiques). NODE3 voit donc les onsite sur cet endpoint NAT'd, pas leur IP LAN. Côté
> NODE3, **pas d'`Endpoint =`** déclaré pour les onsite (apprentissage par PersistentKeepalive
> initié depuis les onsite, toutes les 25 s).

## Public keys

| IP | Public key | Date génération |
|---|---|---|
| `10.99.0.1` (NODE3) | `GDN64aY60tBSWKN4qA6GBd/JhistmDP2oF1qN0Xj9gw=` | 2026-05-02 |
| `10.99.0.2` (onsite-1) | `iO0HHo7Lbuvqs4rV6C456dSm8d+T3ef96CHA9m32CHE=` | 2026-05-06 |
| `10.99.0.3` (onsite-2) | `IdbW+fqdOhFPSIO6XRp5oego+U/shypsOllOjYHQhi8=` | 2026-05-01 |

## Mesh handshakes vérifiés (mesh 3-way complet)

| Date | De | Vers | RTT | Notes |
|---|---|---|---|---|
| 2026-05-08 | onsite-1 (10.99.0.2) | onsite-2 (10.99.0.3) | < 1 ms | LAN direct |
| 2026-05-09 | onsite-1 (10.99.0.2) | NODE3 (10.99.0.1) | 16.6 ms | via Internet, NAT'd, PersistentKeepalive 25s |
| 2026-05-09 | onsite-2 (10.99.0.3) | NODE3 (10.99.0.1) | 16.6 ms | via Internet, NAT'd, PersistentKeepalive 25s |
| 2026-05-09 | NODE3 (10.99.0.1) | onsite-1 (10.99.0.2) | 16.6 ms | retour bidirectionnel OK |
| 2026-05-09 | NODE3 (10.99.0.1) | onsite-2 (10.99.0.3) | 16.6 ms | retour bidirectionnel OK |

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
`10.200.0.0/24`) relie chaque onsite à un **VPS OVH façade à IP fixe** qui
reverse-proxy l'UI alarme vers l'extérieur. Interface **séparée** — `wg0` (le
mesh cluster) **reste intact**, sous-réseaux disjoints (`10.99.0.0/24` vs
`10.200.0.0/24`). Roaming complet (pas d'`Endpoint` côté OVH pour les onsite →
survit au déménagement de site).

Template : [`wg1-facade.conf.example`](wg1-facade.conf.example).
Setup reproductible : `sudo bash scripts/wg-facade-setup.sh <10.200.0.X/24>`
(crée `wg1` **sans jamais toucher `wg0`**).

### Plan d'adressage façade
| IP wg1 | Rôle | Hostname |
|---|---|---|
| `10.200.0.5/24` | alarme1 (onsite-1) | onsite-1.alarm.local |
| `10.200.0.6/24` | alarme2 (onsite-2) | onsite-2.alarm.local |

### Peer serveur (OVH façade)
| Champ | Valeur |
|---|---|
| PublicKey | `EO723NGhs9D1ZR7xPTVNUl+ZLAYYKxoQPNmXzuOWdz8=` |
| Endpoint | `51.77.222.190:51820` |
| AllowedIPs (côté onsite) | `10.200.0.0/24` |
| PersistentKeepalive | 25 |

### Public keys wg1 (à déclarer comme peers **côté OVH**, AllowedIPs `10.200.0.X/32`)
| IP wg1 | Public key | Date |
|---|---|---|
| `10.200.0.5` (alarme1) | `60EmMwaKi6lud6fJD1SPtgak6JQCw8kCIiPmwoQn8V8=` | 2026-06-18 |
| `10.200.0.6` (alarme2) | `5OQWBUYeaaCQkk9wt6sCMwq7n9qdT1YhEnff/zN9yAA=` | 2026-06-18 |

### App exposée
UI + API alarme = backend FastAPI sur **port `8000`**, racine `/`, sur chaque
nœud (le write-proxy INV-043/044 permet de servir l'UI depuis n'importe quel
nœud, même replica). Reverse-proxy OVH → `http://10.200.0.5:8000/` (alarme1) /
`http://10.200.0.6:8000/` (alarme2).

### Notes
- **`MTU = 1280`** sur `wg1` : WG-sur-cellulaire drop les gros paquets à 1420
  (constaté 2026-06-18). 1280 passe sur fibre **et** 4G.
- **`ufw allow in on wg1`** sur chaque onsite (le reverse-proxy joint `:8000`).
- Le handshake reste **vide** tant que les 2 public keys ci-dessus ne sont pas
  ajoutées comme peers côté OVH.
