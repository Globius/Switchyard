# How Switchyard objects render to HAProxy

`syctl render --tenant T --se-group G` (or `switchyard.render.haproxy.render_haproxy`) turns the
desired state of one SE group into the files below. The output is deterministic: the same state gives
the same bytes, so a hash identifies a state. Decisions behind the rules are in
[ADR 0004](adr/0004-renderer-determinism-and-safety.md) and
[ADR 0006](adr/0006-http-routing-semantics.md).

Try it locally:

```powershell
docker compose -f deploy/dev/compose.yaml up -d --wait
cd controller
uv run syctl db upgrade
uv run syctl seed --example
uv run syctl render --tenant demo --se-group edge            # print everything
uv run syctl render --tenant demo --se-group edge --out out  # write the files (out/haproxy/..., out/certs/...)
```

## Files

Paths are relative to `/etc/switchyard/` on the SE, the only directory the agent and the Data Plane
API write to. (`--out DIR` reproduces that layout under `DIR`.)

| File | Contents | Written when |
| --- | --- | --- |
| `haproxy/haproxy.cfg` | `global`, `defaults`, one `frontend` per enabled service, one `backend` per pool in use | always |
| `certs/<service>.crtlist` | one line per certificate: PEM path and its SNI filters; the first line is the default certificate | a service has a TLS port |
| `haproxy/maps/<service>-host.map` | `host backend` for exact-host routes | a service has an exact host-only route |
| `haproxy/maps/<service>-wild.map` | `regex backend` for wildcard-host routes | a service has a wildcard host-only route |
| `haproxy/lists/<policy>.cidr` | one network per line, IPv4 first | a service references an allow-list policy set |

`haproxy/haproxy.cfg` has a `# switchyard-render: sha256:<hex>` line, a hash of every file. The Data
Plane API rewrites the config but keeps comments, so the fingerprint is how drift detection knows what
an SE runs. The crt-list sits beside the certificates because the Data Plane API keeps both in one
directory; object names have no dots because the API rewrites dots in file names.

Certificates and keys are **not** rendered. A `crt-list` line points to
`/etc/switchyard/certs/<certificate name>.pem`, which the agent writes from OpenBao.

Disabled services and pools that no enabled service uses are not rendered. UDP services are skipped
with a warning, because they render to IPVS (a later milestone).

## Object mapping

| Object / field | Rendered as |
| --- | --- |
| SE group `name`, `maxconn` | `# SE group:` comment, `global maxconn` |
| Service `name` (http, tcp) | `frontend vs_<name>`, `mode http` or `mode tcp`, `option httplog` / `tcplog` |
| Service `vip` + each port | one `bind <vip>:<port>` per port, sorted by port. IPv6 uses `bind ipv6@<addr>:<port>` |
| Port `tls` | `ssl crt-list /etc/switchyard/certs/<name>.crtlist` on that bind |
| Port `http2` | `alpn h2,http/1.1` (otherwise `alpn http/1.1` on an HTTP TLS port) |
| Port `proxy_protocol` | `accept-proxy` |
| Port `https_redirect_port` | `http-request redirect prefix "https://%[var(txn.host)][:port]" code 301 if { dst_port <port> }` |
| Service `default_pool_id` | `default_backend pool_<name>` |
| Route `host` (exact) | line in `haproxy/maps/<name>-host.map`; routed by `use_backend %[...map(...)]` |
| Route `host` (`*.example.test`) | line in `haproxy/maps/<name>-wild.map` with an anchored one-label regex; routed with `map_reg` |
| Route with `path_prefix` / `path_exact` | `use_backend pool_<n> if { var(txn.host) -m str <host> } { path_beg <path> }` (host condition only when set) |
| Route `redirect` and policy-set redirect rule | `http-request redirect location <target> code <n> if <conditions>` |
| Service `allow_list_id` + `enforce_allow_list` | `tcp-request session reject if !{ src -f <dir>/lists/<name>.cidr }` |
| Service `restore_client_ip_header` | trust flag, `set-src hdr(<H>)` if trusted, `del-header <H>` if not (see below) |
| Pool `name`, `algorithm` | `backend pool_<name>`, `balance <algorithm>` |
| Pool `persistence` (source IP) | `stick-table type ipv6 size <n> expire <t>s` and `stick on src` |
| Pool `tls`, `tls_verify` | server options `ssl verify none` or `ssl verify required ca-file /etc/ssl/certs/ca-certificates.crt`, plus `sni` from the Host header on HTTP pools |
| Member `address`, `port` | `server <name> <ip>[:<port>]`; a member without a port uses the client's port |
| Member `weight`, `admin_state` | `weight <n>`; `drain` is `weight 0`; `disabled` adds the `disabled` keyword |
| Health monitor `tcp` | `check` on each server (`port <n>` when the monitor sets one) |
| Health monitor `http` | `option httpchk`, `http-check send meth <m> uri <path> [ver HTTP/1.1 hdr Host <h>]`, `http-check expect status <codes>` |
| Monitor `interval_s`, `rise`, `fall`, `timeout_s` | `default-server inter <i>s fall <f> rise <r>` and `timeout check <t>s` |
| Pool with a monitor and `tls` | `check-ssl` on the servers |

Server names come from the member name or `<ip>-<port>` with dots and colons turned into dashes
(`10.20.0.11:8080` becomes `10-20-0-11-8080`), so they are stable and safe to use in the Runtime API.

## Request order in an HTTP frontend

1. **Connection filter**: reject sources outside the allow list (only with `enforce_allow_list`).
2. **Header hygiene**: restore the client address (below), delete any client `X-Forwarded-For`, set
   `X-Forwarded-Proto`, store the lower-cased `Host` without port in `txn.host`. `option forwardfor`
   then adds the connection's source address.
3. **Redirects**: HTTP to HTTPS, then policy-set rules in attachment order, then redirect routes by
   `order`. First match wins.
4. **Path routes** by `order`.
5. **Host routes**: exact host, then wildcard host.
6. **Default pool**, or HAProxy's 503 when there is none.

Wildcards match exactly one label: `*.example.test` matches `a.example.test`, not `example.test` and
not `a.b.example.test`. Certificate coverage uses the same rule.

### Restoring the client address behind Cloudflare

With `allow_list_id` (for example the Cloudflare ranges) and `restore_client_ip_header:
CF-Connecting-IP`:

```text
http-request set-var(txn.trusted) bool(true) if { src -f .../lists/cloudflare-ranges.cidr }
http-request set-src hdr(CF-Connecting-IP) if { var(txn.trusted) -m bool } { hdr(CF-Connecting-IP) -m found }
http-request del-header CF-Connecting-IP if !{ var(txn.trusted) -m bool }
```

The trust flag is computed first because `set-src` changes what `src` means afterwards. A source
outside the list cannot spoof the header: it is removed before the backend sees it.

## Example

The demo tenant (`controller/switchyard/examples/demo.yaml`) renders, for its HTTPS service, to:

```text
frontend vs_web
    mode http
    bind 192.0.2.20:80
    bind 192.0.2.20:443 ssl crt-list /etc/switchyard/certs/web.crtlist alpn h2,http/1.1
    option httplog
    option forwardfor
    http-request del-header X-Forwarded-For
    http-request set-header X-Forwarded-Proto https if { ssl_fc }
    http-request set-header X-Forwarded-Proto http if !{ ssl_fc }
    http-request set-var(txn.host) req.hdr(host),field(1,:),lower
    http-request redirect prefix "https://%[var(txn.host)]" code 301 if { dst_port 80 }
    http-request redirect location /remote.php/dav/ code 301 if { path_beg /.well-known/carddav }
    http-request redirect location /remote.php/dav/ code 301 if { path_beg /.well-known/caldav }
    use_backend %[var(txn.host),map(/etc/switchyard/haproxy/maps/web-host.map)] if { var(txn.host),map(/etc/switchyard/haproxy/maps/web-host.map) -m found }

backend pool_files
    mode http
    balance roundrobin
    stick-table type ipv6 size 100000 expire 1800s
    stick on src
    option httpchk
    http-check send meth GET uri /healthz
    http-check expect status 200,204
    timeout check 3s
    default-server inter 5s fall 3 rise 2
    server 10-20-0-21-11000 10.20.0.21:11000 check weight 100
```

with `haproxy/maps/web-host.map`:

```text
app.demo.example.test pool_app
files.demo.example.test pool_files
```

## Requirements on the SE image

- `net.ipv4.ip_nonlocal_bind=1` and `net.ipv6.ip_nonlocal_bind=1`, so HAProxy can bind a VIP it does
  not currently hold.
- A `/run/haproxy/` directory for the admin socket the config declares.
- The CA bundle at `/etc/ssl/certs/ca-certificates.crt` (only used when a pool sets `tls_verify`).

## Validation and tests

| Check | Where |
| --- | --- |
| Domain rules (references, VIP placement, collisions, SAN coverage) | `switchyard.domain.snapshot.validate_snapshot`, run before every render |
| Exact output | `controller/tests/golden/<case>/input.yaml` → `expected/`; 11 cases |
| `haproxy -c` in `haproxy:3.2` | `tests/integration/test_golden_haproxy.py`, every golden case, no warnings allowed |
| Real requests through rendered config | `tests/integration/test_haproxy_behavior.py` |
| Determinism and input-order independence | `tests/unit/render/test_haproxy.py` |
| Hostile values | `tests/unit/render/test_haproxy.py::TestInjection`, `test_escape.py` |

After an intentional renderer change run `UPDATE_GOLDEN=1 uv run pytest tests/unit/render`, then
review the diff of `tests/golden/**/expected`.
