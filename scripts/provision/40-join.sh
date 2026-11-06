#!/usr/bin/env bash
# 40-join.sh — (root, sur la nouvelle machine) entree dans le cluster de prod comme nœud non votant
# (docs/REPLACE_ONSITE_NODE.md, « Ajouter un nœud non votant ») : images :stable et partie hote a
# jour, puis start-prod-node.sh --pull N (Patroni se clone depuis le leader), puis attente de
# l'etat « streaming » vu par NODE3 (Patroni 10.99.0.1:8008). Prerequis : 20, 21 et 30 faits, et le
# commit qui declare ce nœud (.env.prod.nodeN) promu :stable.
# Voteur remplace (meme NODE_NAME) : ETCD_INITIAL_CLUSTER_STATE=existing, transmis tel quel.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
need_root; this_node
cd "$REPO"
systemctl start alarm-cd-pull.service alarm-host-sync.service
[ -f ".env.prod.$NODE_NAME" ] || die ".env.prod.$NODE_NAME absent : le commit qui declare $NODE_NAME n'est pas encore promu :stable (attendre, puis relancer)"
[ "$(stat -c %a .env.prod.secrets 2>/dev/null)" = 600 ] || die ".env.prod.secrets absent ou pas en 600 : lancer 30-secrets.sh depuis le poste"
runuser -u alarm -- ./scripts/start-prod-node.sh --pull "${NODE_NAME#node}"
member='import json, sys
m = [m for m in json.load(sys.stdin)["members"] if m["name"] == sys.argv[1]]
print("%s lag=%s" % (m[0]["state"], m[0].get("lag", "")) if m else "absent")'
for _ in $(seq "${JOIN_TRIES:-40}"); do
  s=$(curl -s --max-time 4 http://10.99.0.1:8008/cluster | python3 -c "$member" "$NODE_NAME" 2>/dev/null || true)
  say "$(date +%T) $NODE_NAME : ${s:-cluster illisible}"
  if [[ "$s" == streaming* ]]; then
    curl -s --max-time 4 "http://$WG_IP:8000/health"; echo
    say "FAIT : $NODE_NAME suit le leader. Suite : 50-gateway.sh, puis sudo alarm-node-check"
    exit 0
  fi
  sleep "${JOIN_POLL_SECONDS:-30}"
done
die "$NODE_NAME pas en streaming : docker logs $NODE_NAME-patroni-1 (un replica bloque est re-clone seul, INV-168)"
