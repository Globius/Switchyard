# 0049. Proxmox host state and placement read

Status: Accepted
Date: 2026-10-11

## Context

Engines carry `pve_node` and `vmid`, but the API could not say whether the node was online or how the group's engines are
spread over the cluster. The SE group page had no Placement card and showed "Host state not reported" (06 planned-vs-built).

## Decisions

1. The `Proxmox` adapter gets two read calls: `list_nodes` (`GET /nodes`: state, CPU load, memory, uptime) and
   `list_storage` (`GET /nodes/{node}/storage`). The existing API token needs `Sys.Audit` and `Datastore.Audit` for them.
2. The API process builds the adapter from the same environment variables as the worker (`SWITCHYARD_PVE_URL`, ...,
   `runtime.proxmox_client`). Without them the API keeps working and reports host state as unknown.
3. `GET /tenants/{t}/se-groups/{group}/placement` returns every cluster node with state, load, storage and the group's
   engines on it. `EngineRead.host_online` is the node's state, or `null` when the engine has no node or Proxmox cannot be
   asked. Any tenant role may read both; they show infrastructure capacity, not secrets.
4. Answers are cached in the API process for 15 seconds, so pages that list engines do not call Proxmox on every request.
5. When Proxmox is missing or failing the placement answer is `available: false` with a generic reason (the detail goes
   to the log, not to the tenant), and lists the nodes the engines claim, with `online: null`.
6. No new write calls: choosing nodes for a deploy stays in the deploy request.
