# Production deployment (one controller node)

1. Build or pull `switchyard-controller` (`docker build -f deploy/controller/Dockerfile -t switchyard-controller .`).
2. Copy `node.env.example` to `node.env` and set the node's name and IP; put the Ed25519 bundle key at `secrets/bundle.key`.
3. `docker compose --env-file node.env up -d --build` on each of the three VMs. Start the first node with
   `ETCD_CLUSTER_STATE=new` on all three at once; later joins use `existing`.
4. `docker compose exec patroni patronictl -c /etc/patroni.yml list` shows leader, sync standby and replica.

Try failover and restore locally first: `docker compose -f compose.lab.yaml up -d --build --wait`.
Design: ADR 0048. Runbooks: `docs/runbooks/`.
