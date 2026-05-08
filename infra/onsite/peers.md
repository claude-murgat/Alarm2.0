# Wireguard peers — Alarme Murgat onsite mesh

Les clés publiques sont **non secrètes** : elles peuvent vivre dans le repo. Les clés privées
restent sur leurs machines respectives uniquement (`/etc/wireguard/privatekey` mode 600).

## Plan d'adressage

| IP Wireguard | Rôle | Hostname | Endpoint réseau |
|---|---|---|---|
| `10.99.0.1` | NODE3 cloud (hors-site) | _à définir_ | _à définir_ (IP publique cloud) |
| `10.99.0.2` | NODE1 onsite-1 (1ère baie) | onsite-1.alarm.local | `172.16.1.121:51820` |
| `10.99.0.3` | NODE2 onsite-2 (2ème baie) | onsite-2.alarm.local | `172.16.1.120:51820` |

## Public keys

| IP | Public key | Date génération |
|---|---|---|
| `10.99.0.2` (onsite-1) | `iO0HHo7Lbuvqs4rV6C456dSm8d+T3ef96CHA9m32CHE=` | 2026-05-06 |
| `10.99.0.3` (onsite-2) | `IdbW+fqdOhFPSIO6XRp5oego+U/shypsOllOjYHQhi8=` | 2026-05-01 |

## Mesh handshakes vérifiés

| Date | De | Vers | RTT | Source |
|---|---|---|---|---|
| 2026-05-08 | onsite-1 (10.99.0.2) | onsite-2 (10.99.0.3) | < 1 ms | session audit déploiement |

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
