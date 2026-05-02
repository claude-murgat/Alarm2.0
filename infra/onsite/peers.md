# Wireguard peers — Alarme Murgat onsite mesh

Les clés publiques sont **non secrètes** : elles peuvent vivre dans le repo. Les clés privées
restent sur leurs machines respectives uniquement (`/etc/wireguard/privatekey` mode 600).

## Plan d'adressage

| IP Wireguard | Rôle | Hostname | Endpoint réseau |
|---|---|---|---|
| `10.99.0.1` | NODE3 cloud (hors-site) | _à définir_ | _à définir_ (IP publique cloud) |
| `10.99.0.2` | NODE1 onsite-1 (1ère baie) | _à définir_ | LAN site (à fixer) |
| `10.99.0.3` | NODE2 onsite-2 (cette machine) | onsite-2.alarm.local | `172.16.1.120:51820` |

## Public keys

| IP | Public key | Date génération |
|---|---|---|
| `10.99.0.3` (onsite-2) | `IdbW+fqdOhFPSIO6XRp5oego+U/shypsOllOjYHQhi8=` | 2026-05-01 |

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
