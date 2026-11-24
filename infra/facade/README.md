# Façade publique — `https://alarme.charlesmurgat.com` (INV-199)

Un seul nom pour l'UI web et l'API, 24 h/24, servi par **n'importe quel nœud sain** (décision du
propriétaire du 2026-11-24 ; l'app Android l'essaie en premier, INV-ANDROID-402). Le nom pointe sur le
**VPS façade** OVH `51.77.222.190` (Ubuntu 24.04, Apache 2.4.58), qui répartit les requêtes entre les
backends `:8000` du cluster.

> **VPS partagé** : une vingtaine d'autres sites et d'autres clients WireGuard (`10.200.0.2` à `.4` ;
> `.4` sert `supervision.charlesmurgat.com`, une autre application, le lien des SMS d'alarme) n'ont
> **rien à voir** avec l'Alarme Murgat. Ne jamais toucher leurs vhosts ni leurs pairs.

**Hors CI** : tout ce qui est décrit ici est **appliqué à la main** sur le VPS. Les fichiers de ce
dossier sont des copies de référence (état du 2026-11-24, reconstituées après la mise en place) :
rien ne vérifie qu'elles sont identiques au VPS ; faire un `diff` avant de s'en servir. Seul le côté
nœud (`wg1`) est contrôlé chaque jour, par `alarm-node-check` (INV-186).

| Fichier | Sur le VPS |
|---|---|
| [`alarme-murgat-balancer.conf`](alarme-murgat-balancer.conf) | `/etc/apache2/conf-available/` (`a2enconf alarme-murgat-balancer`) : `balancer://alarme`, un `BalancerMember` par nœud, santé `/health` toutes les 10 s, 2 échecs = hors du pool |
| [`alarme.charlesmurgat.com.conf`](alarme.charlesmurgat.com.conf) | `/etc/apache2/sites-available/` : `:80`, redirection `301` vers https (certbot) |
| [`alarme.charlesmurgat.com-le-ssl.conf`](alarme.charlesmurgat.com-le-ssl.conf) | `/etc/apache2/sites-available/` : `:443`, Let's Encrypt, `ProxyPass / balancer://alarme/` |
| [`old-names-redirect.conf`](old-names-redirect.conf) | extrait : `alarme1` / `alarme2.izeron.charlesmurgat.com` (`:443`) en `301` vers le nouveau nom, chemin conservé |

## WireGuard du VPS

Interface **`wg0` sur le VPS** (= `wg1` des nœuds) : `10.200.0.1/24`, port `51820`, clé publique
`EO723NGhs9D1ZR7xPTVNUl+ZLAYYKxoQPNmXzuOWdz8=`. Un pair par nœud, `AllowedIPs 10.200.0.X/32`,
`PersistentKeepalive 25`, **sans `Endpoint`** (le nœud se connecte : un changement d'IP publique du site
ne change rien). Plan d'adresses et clés : [`infra/onsite/peers.md`](../onsite/peers.md) § façade ;
adresse de chaque machine : `STD_FACADE_PLAN` de [`infra/onsite/standard.env`](../onsite/standard.env).

Le VPS fait suivre le trafic **entre tous ses clients** (`PostUp` iptables `FORWARD -i wg0 -j ACCEPT`).
D'où **aucune règle ufw sur `wg1`** côté nœud : `ufw allow in on wg1` ouvrait SSH et le reste aux
clients `.2` à `.4`. Le backend reste joignable : Docker publie `8000` sur `0.0.0.0`.

NODE3 n'a pas de `wg1` : il est membre par son IP publique `51.210.105.102`.

## Avant toute modification sur le VPS : sauvegarde

```bash
B=/root/apache-backup-$(date +%Y%m%d-%H%M); mkdir -p "$B" && cp -a /etc/apache2 /etc/wireguard/wg0.conf "$B"/
```
Convention : `/root/apache-backup-*`. Retour arrière : recopier depuis `$B`, puis
`apache2ctl configtest && systemctl reload apache2` (et `wg syncconf` ci-dessous pour WireGuard).

## Ajouter un nœud

1. **PR** : adresse de la machine dans `STD_FACADE_PLAN` (prochaine adresse libre, `.11`…), ligne dans
   `infra/onsite/peers.md`, `BalancerMember` dans [`alarme-murgat-balancer.conf`](alarme-murgat-balancer.conf).
2. **Nœud** : `sudo bash /opt/alarm/scripts/wg-facade-setup.sh 10.200.0.X/24` ; il affiche sa clé publique.
3. **VPS** (root, après la sauvegarde) : ajouter le pair à `/etc/wireguard/wg0.conf`, puis l'appliquer
   **sans redémarrer** l'interface (les autres clients gardent leur tunnel) :
   ```
   [Peer]
   # alarm-N
   PublicKey = <clé affichée à l'étape 2>
   AllowedIPs = 10.200.0.X/32
   PersistentKeepalive = 25
   ```
   ```bash
   wg syncconf wg0 <(wg-quick strip wg0)
   wg show wg0 latest-handshakes                 # le nœud apparaît (< 2 min)
   curl -s http://10.200.0.X:8000/health          # 200
   ```
4. **VPS** : ajouter le `BalancerMember` dans `/etc/apache2/conf-available/alarme-murgat-balancer.conf`, puis
   ```bash
   apache2ctl configtest && systemctl reload apache2
   ```
5. **Nœud** : `sudo alarm-node-check` → `wg1`, `wg1_facade`, `wg1_ufw` OK ; le backend reçoit les sondes
   `/health` du VPS toutes les 10 s (`docker logs --since 1m nodeN-backend-1 2>&1 | grep -c 'GET /health'`).

## Retirer un nœud (jour du déménagement : onsite-1 `.5`, onsite-2 `.6`)

1. **VPS** : supprimer sa ligne `BalancerMember`, `apache2ctl configtest && systemctl reload apache2`
   (d'abord : plus aucune requête ne lui est envoyée).
2. **VPS** : supprimer son `[Peer]` de `/etc/wireguard/wg0.conf`, `wg syncconf wg0 <(wg-quick strip wg0)`.
3. **PR** : retirer la ligne de ce dossier et de `infra/onsite/peers.md` (adresse jamais réattribuée).

## Certificat

Let's Encrypt par le **certbot du snap** : `/snap/bin/certbot --apache -d alarme.charlesmurgat.com`
(le `/usr/bin/certbot` d'apt n'a pas le plugin apache). Renouvellement automatique par le timer du snap
(`systemctl list-timers 'snap.certbot*'`) ; tester : `/snap/bin/certbot renew --dry-run`.

## Vérifier

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://alarme.charlesmurgat.com/health    # 200
curl -sI https://alarme1.izeron.charlesmurgat.com/api/x | grep -i '^location'      # https://alarme.charlesmurgat.com/api/x
```

## Limite connue

`/health` répond 200 même quand le nœud ne voit plus de leader (`cluster_writable: false`, INV-171) : un
nœud coupé du cluster mais joignable par la façade reste dans le pool (lectures périmées, écritures en
503). Non traité (INV-199).
