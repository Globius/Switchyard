# Production deployment (one controller node)

1. Build or pull `switchyard-controller` (`docker build -f deploy/controller/Dockerfile -t switchyard-controller .`).
2. Copy `node.env.example` to `node.env` and set the node's name and IP; put the Ed25519 bundle key at `secrets/bundle.key` and the mTLS files (`ca.crt`, `client.crt`, `client.key`) in `secrets/mtls/`.
   The controller will not start with a bundle key but no CA (`SWITCHYARD_DATAPLANE_CA`).
   Also issue a certificate per node (`issue-node-cert.sh`, into `secrets/pki/`), set `NODE_IPS`, `PATRONI_API_PASSWORD` and
   `DOCKER_SUBNET`, and load the host firewall (`sh firewall.sh --persist`): etcd, Patroni and PostgreSQL run over TLS and
   answer only the three nodes (docs/production.md, "Securing the database tier").
3. `docker compose --env-file node.env up -d --build` on each of the three VMs. Start the first node with
   `ETCD_CLUSTER_STATE=new` on all three at once; later joins use `existing`.
4. `docker compose exec patroni patronictl -c /etc/patroni.yml list` shows leader, sync standby and replica.

OpenBao PKI role for agents (`SWITCHYARD_PKI_ROLE`): set `use_csr_sans=false` and `use_csr_common_name=false`, plus
`allow_any_name=true`, `allow_ip_sans=true`, `enforce_hostnames=false`, `key_type=any`. Without `use_csr_sans=false` OpenBao
ignores the SANs the controller sends (the engine's management address never reaches the certificate and the controller
cannot verify the agent) and the agent could pick its own names.

OpenBao: load the controller's policy before first start, matching your secret layout:
`syctl openbao-policy --shared kv/se/ --platform kv/proxmox/token | bao policy write switchyard-controller -`
(drop `--shared` if no shared Data Plane credentials exist), and give the controller's AppRole that policy.
The `SWITCHYARD_INSECURE_AGENT`, `SWITCHYARD_ENGINE_POLICY=open` and `SWITCHYARD_SECRET_POLICY=open` switches exist for
throwaway labs only (ADR 0072); the production Compose never sets them.

Monitoring (Prometheus, Loki, Grafana) runs separately from `deploy/telemetry/`; the scrape token is `METRICS_TOKEN` in `node.env`.

Try failover and restore locally first: `docker compose -f compose.lab.yaml up -d --build --wait`.
Design: ADR 0048. Runbooks: `docs/runbooks/`.

The `proxy` service (nginx, `deploy/proxy/`) serves the built UI and forwards `/api` and `/metrics` to the API. It sets the CSP,
HSTS (when `X-Forwarded-Proto: https`), `nosniff`, `Referrer-Policy` and `frame-ancestors 'none'`, and limits login,
OIDC callback and break-glass requests to 5 a minute per client address. TLS is terminated in front of it, by the management SE
group's HAProxy, whose pool should list each node's port 8080. Behind HAProxy the client address nginx sees is the SE's, so
rate limiting only works per client if HAProxy sends `X-Forwarded-For` and nginx trusts it; see ADR 0053.
