# Production deployment (one controller node)

1. Build or pull `switchyard-controller` (`docker build -f deploy/controller/Dockerfile -t switchyard-controller .`).
2. Copy `node.env.example` to `node.env` and set the node's name and IP; put the Ed25519 bundle key at `secrets/bundle.key` and the mTLS files (`ca.crt`, `client.crt`, `client.key`) in `secrets/mtls/`.
   The controller will not start with a bundle key but no CA (`SWITCHYARD_DATAPLANE_CA`).
3. `docker compose --env-file node.env up -d --build` on each of the three VMs. Start the first node with
   `ETCD_CLUSTER_STATE=new` on all three at once; later joins use `existing`.
4. `docker compose exec patroni patronictl -c /etc/patroni.yml list` shows leader, sync standby and replica.

Monitoring (Prometheus, Loki, Grafana) runs separately from `deploy/telemetry/`; the scrape token is `METRICS_TOKEN` in `node.env`.

Try failover and restore locally first: `docker compose -f compose.lab.yaml up -d --build --wait`.
Design: ADR 0048. Runbooks: `docs/runbooks/`.

The `proxy` service (nginx, `deploy/proxy/`) serves the built UI and forwards `/api` and `/metrics` to the API. It sets the CSP,
HSTS (when `X-Forwarded-Proto: https`), `nosniff`, `Referrer-Policy` and `frame-ancestors 'none'`, and limits login,
OIDC callback and break-glass requests to 5 a minute per client address. TLS is terminated in front of it, by the management SE
group's HAProxy, whose pool should list each node's port 8080. Behind HAProxy the client address nginx sees is the SE's, so
rate limiting only works per client if HAProxy sends `X-Forwarded-For` and nginx trusts it; see ADR 0053.
