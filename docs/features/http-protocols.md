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

HTTP/3 adds a QUIC listener on the same port number over UDP and an `Alt-Svc` header so clients upgrade. WebSocket works
through the HTTP/1.1 upgrade with nothing to switch on.

**Limits.** `http` services and pools only. The VIP must accept UDP on the HTTP/3 port, so check firewalls in front of the
engines. There is no separate gRPC switch: use `h2` or `h2c`.

**Decision:** [0116](../adr/0116-http3-grpc-websocket.md).
