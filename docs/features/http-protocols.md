# HTTP/2, HTTP/3, gRPC and WebSocket

**What.** HTTP/2 and HTTP/3 (QUIC) towards clients, HTTP/2 to the members (which carries gRPC), and a timeout for
long-lived upgraded connections.

**How to use.**

```yaml
# on the service; http3 needs tls and http2
ports: [{port: 443, tls: true, http2: true, http3: true}]
---
# on the pool
backend_protocol: h2       # http1 (default), h2 (TLS with ALPN h2) or h2c (cleartext HTTP/2)
tunnel_timeout_s: 3600     # idle time of WebSocket connections and long gRPC streams
```

HTTP/3 adds a QUIC listener on the same port number over UDP and an `Alt-Svc` header so clients upgrade. WebSocket needs
nothing switched on: it rides the HTTP/1.1 upgrade, and `tunnel_timeout_s` is the only knob — the pool's value becomes
`timeout tunnel` in its backend.

**Tested.** `controller/tests/integration/test_websocket_upgrade.py` starts HAProxy from a rendered config, completes the
RFC 6455 handshake through a plain-HTTP frontend, exchanges a frame in each direction, and holds the connection silent
past the frontend's 10s `timeout http-request` before exchanging another one. A second service on the same backend sets
`tunnel_timeout_s: 2` and the test asserts that its tunnel is *closed* after four seconds of silence — which is what
fails if the rendered `timeout tunnel` line disappears, because the `defaults` section's `timeout tunnel 1h` would keep
the connection open instead. The renderer half is pinned without a container by
`tests/unit/render/test_haproxy.py::TestBackends::test_tunnel_timeout_is_the_pools_value_and_only_in_its_backend`.

**Limits.** `http` services and pools only. The VIP must accept UDP on the HTTP/3 port, so check firewalls in front of the
engines. There is no separate gRPC switch: use `h2` or `h2c`. The WebSocket test covers cleartext `ws` through a
plain-HTTP port; it does not compare `wss` termination with other HTTPS services, and it waits seconds rather than
exercising long-lived idle behaviour (no keepalives, no reconnect, no hours-long tunnel).

**Decision:** [0116](../adr/0116-http3-grpc-websocket.md).
