# 0073. Rendered config per engine and running config of an engine

Status: Accepted
Date: 2026-10-11

## Context

The virtual service screens showed one rendered config per object, but not which engines it lands on, what a given engine gets (routing files
differ per engine), or what an engine is running now (07 UI vs artboards).

## Decisions

1. `GET /tenants/{t}/virtual-services/{service}/engines` lists the enabled engines of the service's SE group. `GET .../engines/{engine}/rendered`
   returns every file rendered for that engine: HAProxy (`scope: group`, identical on all engines) and its FRR or Keepalived files (`scope: engine`).
   Certificates and keys are never returned, and BGP passwords are replaced by `REDACTED`. Any tenant role may read both, like other renders.
2. `GET /tenants/{t}/engines/{engine}/running-config` asks the engine live and stores nothing. HAProxy's configuration text comes from its Data
   Plane API (`running_config()` on the engine adapter) with its stamped fingerprint, `fingerprint_matches` and `matches_desired`. The latter is
   also true only when the text equals the desired file, because a hand edit can keep the stamp. Through the agent the endpoint returns the
   hash of each routing file next to the hash of what the controller would send now. The agent has no call that reads file contents, by design
   (it has no file-read endpoint to abuse), so contents of routing files are not available live.
3. Tenants get the fixed engine error sentence for an unreachable engine; system administrators see the real error (ADR 0072).
