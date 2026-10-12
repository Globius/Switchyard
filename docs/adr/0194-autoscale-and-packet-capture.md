# ADR 0194: Automatic scale-out of SE groups, and packet capture on engines

Status: Accepted

## Context

Two operational items stayed open in the parity plan (08 C9 and D5) and in the audit. Capacity: adding an engine was a manual
procedure (`runbooks/rolling-upgrade.md`: deploy, enroll, reapply, wait for the health score). Diagnosis: FortiADC, F5 and
NetScaler can capture packets on the appliance, and a Switchyard operator had to find the engine's address and run `tcpdump`
over a shell the product otherwise says nobody needs.

## Decision 1: scale-out by itself, never scale-in

An SE group may carry an `autoscale` block (migration 0034, JSONB on `se_group`, part of desired state and therefore of
change sets and revisions): `max_engines` (2 to 16, default 8), `scale_out_at` (percent of the connection limit, default 70),
`sustain_s` (default 300), `cooldown_s` (default 900), `deploy` (the arguments of a deploy: nodes, template, management pool,
Data Plane login reference, optional NIC pools) and `enroll` (controller URL, certificate name, and an OpenBao reference to the
controller CA).

The `autoscale_se_groups` job (every 30 seconds, only when Proxmox and agent enrollment are configured) looks at each group:

1. **Signal.** The status poller already calls the engine's Data Plane API; it now also keeps `curr_conns` and `max_conn` from
   `runtime/info` in the stored agent status (`load`). Utilization is the mean over reachable engines of connections as a
   percent of that engine's own limit. No Prometheus is needed. The JSON key names (`curr_conns`, `max_conn`, and the
   capitalized forms) were read from documentation and are not yet confirmed against a real Data Plane API (see Consequences).
2. **Decide** (pure function `decide`): busy for `sustain_s`, under `max_engines`, outside the cooldown, group health score of
   80 or more (an unhealthy group is reported `blocked`, not given more engines), and no engine already being added.
3. **Deploy.** It queues the normal `deploy_se_group` job for one more engine, registered **`disabled`** (`staged`), so a
   rollout or a poll never touches an engine that cannot answer yet.
4. **Enroll.** When the deploy job has finished, the controller creates the engine's one-time token and writes the three
   enrollment files into the guest through the Proxmox **guest agent** (`agent/file-write` and `agent/exec`), the same files
   `deploy/quickstart/enroll-engine.sh` writes, then starts `sy-agent-enroll.service`. Nothing connects to the guest: the
   controller still never uses SSH. QEMU engines only (a container has no guest agent API); a retry follows every tick until
   the agent in the guest is up.
5. **Join.** When the engine's agent answers, the engine is enabled and the group is reapplied (canary first, no revision),
   the manual procedure of the runbook. The cooldown starts then.

A stuck step is visible, not retried forever: a failed deploy, an engine that has not enrolled after 20 minutes, or an
unhealthy group shows as `blocked` with the reason in `GET /tenants/{t}/autoscale`, and no second engine is added while one is
pending. Scale-in is not automated: removing capacity stays a deliberate `retire`. Defaults chosen without asking: 70 percent
for 5 minutes, 15 minutes between engines, 8 engines at most.

## Decision 2: packet capture is a bounded job, with a structured filter

`POST /tenants/{t}/engines/{engine}/captures` queues a `packet_capture` job. The agent gets a new server-streaming RPC,
`Capture`, that runs `tcpdump` and streams the pcap back; the controller keeps it in `packet_capture` (migration 0035, RLS,
`bytea`) until housekeeping deletes it after 24 hours.

- **No free-form filter.** The request has an interface (`any` or one the engine has), a protocol (`tcp`, `udp`, `icmp`), one
  IP address and one port. The agent builds the argument list; the controller validates the same fields; tcpdump is started
  without a shell and the expression follows `--`.
- **Bounded.** 1 to 60 seconds, 1 to 20000 packets, 64 to 262144 bytes per packet, 16 MiB of pcap (a capture that hits it is
  marked `truncated`), one capture per engine at a time (agent and controller both refuse a second).
- **Sensitive.** It needs `tenant-admin` (the permission to manage engines), is audited when requested and when downloaded
  (`capture.requested`, `capture.downloaded`), and the tenant's row-level security applies. A capture can contain plaintext
  traffic; TLS payloads stay encrypted, and the SE keeps its own keys.
- The SE image gains the `tcpdump` package.

## Consequences

- Autoscaling needs the SE template to carry `qemu-guest-agent` (it does) and the OpenBao references of the Data Plane login and
  the controller CA. It is **not lab-tested**: the unit and database tests cover the decision, the job sequence, the enrollment
  files and the Proxmox calls against mocks. The first real run should check the Data Plane `runtime/info` key names and the
  guest-agent calls.
- Packet capture on an engine that serves a `dr` or `tunnel` UDP service (ADR 0192) cannot see the replies, which bypass the SE.
- The agent's `Capture` RPC is the only place the agent starts `tcpdump`; the `runner` allowlist is unchanged.
