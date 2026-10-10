# 0020. UDP (M6) implementation: monitor, field rules, bundle files, IPVS status, watchdog

Status: Accepted
Date: 2026-10-10

## Context

[ADR 0013](0013-udp-keepalived-ipvs.md) fixed the design of UDP virtual services (IPVS NAT with source NAT, `udp` monitors, scheduler mapping, VRRP and BGP behavior). Building M6 raised details the ADR left open: the exact shape of the `udp` monitor, how a form learns the field rules, which files the agent bundle carries and how the agent applies them, how IPVS state reaches the status API, and how the BGP watchdog treats UDP VIPs. This ADR records those choices. It also retires T6 (ADR 0013, decision 8), because UDP is served now.

## Decisions

1. **The `udp` monitor.** `MonitorType.udp` with an optional `udp` block: `payload_hex` (lowercase hex, at most 256 bytes) and `require_reply` (the server must answer; default off). An `http` block is rejected on `udp` monitors and a `udp` block on the others. It renders Keepalived `UDP_CHECK`: `delay_loop` is the interval, `retry` is `fall`, `connect_timeout` is the timeout, `connect_port` is the monitor port. `rise` has no `UDP_CHECK` equivalent and is ignored for UDP. Storage is a nullable `health_monitor.udp` JSONB column and the `type` check gains `udp` (migration 0010).
2. **Field rules live in one table, reported per field.** `domain/field_rules.py` lists what each protocol forbids on the service, its ports and its pools. The snapshot validator reads it and reports each violation on its own path (`virtual_services.wg.ports[0].tls`, `pools.wg.persistence`) with the codes `udp_field_not_allowed`, `udp_algorithm_unsupported`, `udp_member_port_required` and `monitor_protocol_mismatch`; `GET /api/v1/schema/virtual-service?protocol=` publishes the same table (ticket T7, option 1 of the ticket). The model-level checks that raised a path-less message for UDP services are gone; the model keeps only what it needs to be coherent (`default_pool_id`).
3. **A `udp` monitor only checks UDP pools, and `tcp`/`http` monitors never check a UDP pool.** Unused pools are not constrained. A pool shared by services of different protocols stays `pool_protocol_mismatch`.
4. **Bundle files.** A group with an enabled UDP service gets `nftables/switchyard-udp.nft` (a `sy_udp` table masquerading `daddr . udp dport` of the real servers) and `sysctl/switchyard-udp.conf` (`ip_forward`, IPv6 forwarding, `net.ipv4.vs.conntrack`). In BGP mode `keepalived/keepalived.conf` holds the virtual servers only, and exists only while the group has an enabled UDP service; `frr/vips.json` gains `udp_prefixes` and `udp_only_prefixes` (UDP VIPs no HAProxy service shares).
5. **How the agent applies them.** The managed directories `nftables` and `sysctl` map to the units `nftables` (reload) and `systemd-sysctl` (restart, because it cannot reload). Keepalived runs only when it has a configuration: the agent starts it when its files appear and it is not running, and stops it when its last file is gone, so removing the last UDP service of a BGP group does not fail a reload. The SE image includes the nftables file from `/etc/nftables.conf` and links the sysctl file into `/etc/sysctl.d/`.
6. **Status.** The agent runs `ipvsadm -Ln --exact` and `ipvsadm -Ln --stats --exact` (the second adds packet and byte counters; the two cannot be combined) and compares the table with the real servers in the rendered `keepalived.conf`: Keepalived removes a server whose check fails, so a configured server absent from the table is `down`, and weight 0 is `drain`. The result is `Status.ipvs` in the agent proto. The controller maps it to HAProxy-shaped member rows (`pool_<name>`, server name, `UP`/`DOWN`/`DRAIN`) in the engine status; a member served on several UDP ports reports its worst state. Netlink is not used in M6; `ipvsadm` is the only addition to the agent's command allowlist.
7. **Watchdog.** In BGP mode the agent withdraws the /32 of a UDP VIP when `keepalived` is not running or one of that VIP's virtual servers has no real server that is up, with the same failure threshold as for HAProxy. HAProxy health no longer affects a VIP only UDP services use; a VIP shared between UDP and TCP/HTTP services follows both. Withdrawal is per prefix. A failed `ipvsadm` listing does not withdraw anything.
8. **`not_served` is retired in practice.** The HAProxy renderer skips UDP services silently, the diff has no `udp_not_served` warning and status reports UDP services as `serving`. The `warnings[]` list and the `not_served` value stay in the API contract so clients keep working.
9. **Validation.** `keepalived -t` and `nft -c` run in a test image built from Debian 13 (Keepalived 2.3): Keepalived 2.0 has no `UDP_CHECK`. The golden cases `04-vrrp-udp` and `05-bgp-udp` cover both HA modes. A real IPVS test (kernel module, privileged host) remains a separate optional lane that is not built yet.

## Consequences

- The SE image must run Keepalived 2.1 or later, carry `ipvsadm` and `nftables`, and wire the two include paths from decision 5.
- UDP checks have no `rise`: a recovered server returns to the table after Keepalived's next successful check.
- A UDP VIP that stops serving is withdrawn as a whole prefix; other VIPs of the SE stay announced.
- Existing BGP groups without UDP produce byte-identical bundles.

## Alternatives considered

- **`oneOf` by protocol for the form rules:** a larger schema change for the same information; the small endpoint reads from the table the validator uses.
- **Netlink for IPVS status now:** needs a netlink library and kernel-specific testing; `ipvsadm` is already on the SE image and easy to fake.
- **Always run Keepalived on BGP groups:** an empty configuration is a state to test and monitor for no benefit; start and stop on demand keeps BGP groups without UDP as they were.
- **Whole-SE route withdrawal on any UDP failure:** simpler, but one UDP pool with no healthy server would take HTTP VIPs of the same SE out of rotation.
