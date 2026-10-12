# Packet capture on an engine

Capture the traffic of one service engine for a short time and download it as a `pcap` file for Wireshark. The controller asks
the engine's agent to run `tcpdump`; nobody needs a shell on the engine.

A capture can contain plaintext traffic, so it needs the **tenant-admin** role, is audited when requested and when downloaded
(`capture.requested`, `capture.downloaded`), stays inside the tenant, and is deleted after 24 hours. TLS payloads remain
encrypted. See [ADR 0194](adr/0194-autoscale-and-packet-capture.md).

## Take a capture

```sh
syctl api POST /tenants/acme/engines/<engine id>/captures \
  '{"interface": "any", "protocol": "tcp", "host": "192.0.2.20", "port": 443, "duration_s": 10, "max_packets": 2000}'
```

| Field | Meaning | Limit |
| --- | --- | --- |
| `interface` | `any`, or an interface of the engine (`eth0`) | the engine must have it |
| `protocol` | `tcp`, `udp` or `icmp` (includes ICMPv6); empty for all | |
| `host` | Packets to or from this IP address | an address, not a name or a filter |
| `port` | Packets to or from this port | not with `icmp` |
| `duration_s` | Stop after this long | 1 to 60 |
| `max_packets` | Stop after this many packets | 1 to 20000 |
| `snaplen` | Bytes kept per packet; 0 keeps whole packets | 64 to 262144 |

There is no free-text filter. The agent builds the filter from these fields, so a value cannot add options or expressions.
One capture runs on an engine at a time.

Then follow it and download the file:

```sh
syctl api GET /tenants/acme/captures                       # all captures, newest first, without data
syctl api GET /tenants/acme/captures/<id>                  # state: queued, running, done or failed
curl -H "Authorization: Bearer $TOKEN" -o edge-1.pcap \
  https://switchyard.example.test/api/v1/tenants/acme/captures/<id>/pcap
```

A capture stops at its time, its packet count or 16 MiB of file, whichever comes first (`truncated: true` when the size ended
it). `failed` carries the reason, for example that the engine has no such interface. The agent must be reachable; the engine
must be enabled.

## What you will not see

An engine that serves a UDP service in `dr` or `tunnel` mode ([ADR 0192](adr/0192-ipvs-direct-return-and-tunnel.md)) never
carries the replies, so a capture there shows the requests only. Capture on the real server for the other direction.
