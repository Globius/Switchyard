# FIPS 140-3 assessment (research, 2026-10-12, origin/main)

Read-only inventory. Nothing here is a decision; an ADR comes first if we pursue it.

Status: backlog, low priority (feature-completeness first). Chosen direction: FIPS mode on Rocky Linux with free software,
claimed as "FIPS mode", not "validated". Tracked in [work item #1](https://git.example.com/globius-oss/switchyard/-/work_items/1).

"FIPS-enabled" here means every crypto operation runs in a CMVP-validated module in approved mode. In practice that
means building on a vendor's validated OpenSSL/Go module (RHEL/UBI, Ubuntu Pro FIPS) with the OS FIPS flag on. Check the
CMVP certificate for the exact module version before claiming anything.

## Inventory and path

| Area | Today (from repo) | FIPS path | What breaks | Size |
| --- | --- | --- | --- | --- |
| Controller crypto lib | `cryptography>=50` wheels bundle their own OpenSSL | Build `cryptography` from source against system OpenSSL 3 FIPS provider on UBI/Ubuntu Pro | Wheels are not FIPS; uv lock and image policy change | M |
| Operator password hash | `hashlib.scrypt` (`services/sessions.py`) | PBKDF2-HMAC-SHA256 (SP 800-132). scrypt, bcrypt, argon2 are not approved | Versioned hash scheme, rehash on login | S |
| TOTP | HMAC-SHA1 (`sessions.py`) | HMAC-SHA1 is still allowed as a MAC; keep, document | Plain SHA-1 digests would be blocked (none found) | S |
| JWT / OIDC / JWKS | `joserfc`; RS/PS/ES allow-list (`services/oidc.py`) | Algorithms approved; backend is `cryptography` | RSA < 2048 and EdDSA rejected | S |
| SAML | `signxml`, SHA-256 | Same backend; require RSA >= 2048, SHA-256+ | IdPs signing with SHA-1 fail | S |
| ACME / cert keys | ECDSA P-256, `acme` | Approved | None | S |
| Bundle signing | Ed25519 (ADR 0008, agent verifies) | Ed25519 is in FIPS 186-5 but module support varies; safest is ECDSA P-256 | Bundle format version, key rotation, rolling upgrade | M |
| gRPC / Python TLS | stdlib `ssl` fine; `grpcio` wheel bundles BoringSSL | Build grpcio from source against system OpenSSL, or terminate controller-to-agent mTLS in a validated proxy | grpcio is the hardest Python item | M-L |
| Agent (Go 1.26) | `crypto/tls`, ecdsa, ed25519, mTLS | Go native FIPS module: build `GOFIPS140=v1.0.0`, run `GODEBUG=fips140=on` (or `only`); alt BoringCrypto | `only` rejects non-approved algs and curves | S-M |
| HAProxy 3.2 (SE) | Debian OpenSSL; cipher lists in `render/haproxy.py` include CHACHA20 and CBC-SHA legacy set | Link system OpenSSL 3 FIPS provider; add a `fips` TLS profile (TLS 1.2/1.3, AES-GCM, ECDHE P-256/384, no ChaCha/X25519) | Legacy and intermediate profiles unusable; renderer plus golden tests | M |
| Keepalived | No VRRP auth rendered | Nothing; lint to keep `auth_pass` off | None | S |
| FRR BGP | `neighbor ... password` from `md5_ref` (`render/routing.py`, `frr.conf.j2`) = TCP-MD5 | MD5 is not approved. TCP-AO (RFC 5925, Linux 6.7+, FRR support partial) or documented exception with GTSM/ACLs | The one real functional break; BGP peer model and ADR | M-L |
| dnsdist / PowerDNS | `dnsdist-19`, `pdns-auth-50` images | Build against FIPS OpenSSL; DNSSEC RSA/ECDSA only; TSIG HMAC-SHA256 | Ed25519 DNSSEC, HMAC-MD5 TSIG off | M |
| oauth2-proxy | Prebuilt GitHub release, sha256-pinned | Rebuild from source with Go FIPS in our pipeline | None expected | S-M |
| coraza-spoa | Built from source (`golang:1.26`, CGO off) | `GOFIPS140` build; almost no crypto of its own | None expected | S |
| OpenBao | `openbao/openbao:latest` | Source build with Go FIPS; disable transit Ed25519/ChaCha20; pin version | No validated upstream release; `latest` tag also blocks any claim | M |
| PostgreSQL 17 / Patroni / etcd | `postgres:17-bookworm`, scram-sha-256 already | FIPS OS base; TLS 1.2+ approved ciphers; rebuild etcd with Go FIPS | md5 auth must stay off (already is) | M |
| UI build and proxy | Vite/React build-time only; `nginx:1.27-alpine` | No runtime crypto in the SPA; swap nginx to a FIPS base with explicit `ssl_ciphers` | Alpine/musl has no validated module | S |
| Base images | `python:3.12-slim-bookworm`, Debian 13 SEs, alpine | UBI 9/10 or Ubuntu Pro FIPS. Debian has no validated OpenSSL | Largest blocker: SE image (`provision.sh`, `packages.txt`, templates) | L |
| LXC SEs | LXC template shares the Proxmox host kernel | Host is Debian, not FIPS; use KVM SEs only (`fips=1` kernel) | LXC excluded from FIPS profile | M |
| Helm chart | Plain Deployments, tag-pinned images, nginx `TLSv1.2 TLSv1.3` | `fips.enabled` value (env, ciphers), digest-pinned FIPS images, FIPS node pool, FIPS CNPG image | Image swaps | S-M |

Sizes: S = days, M = 1-3 weeks, L = 1-2 months, one engineer, excluding any formal validation.

## Caveats
- Only crypto modules are validated, not products. The realistic claim is "uses FIPS-validated modules in approved mode on FIPS-capable OS images".
- BGP TCP-MD5 and the Debian-based SE image are the two structural gaps. The rest is build and config work.

## Recommended approach
1. Decide the claim and target OS in an ADR. Recommend KVM SEs on Ubuntu Pro FIPS or RHEL, UBI for containers; drop Debian SEs and LXC from the FIPS profile.
2. Phase 0 (S), guardrails: a `fips` TLS profile in the renderer, validators rejecting Ed25519/ChaCha/X25519/legacy, and a CI check for non-approved primitives.
3. Phase 1 (M), controller and agent: UBI image with source-built `cryptography`, PBKDF2 instead of scrypt, ECDSA bundle signing (versioned), agent built with `GOFIPS140`. Decide the grpcio approach.
4. Phase 2 (M-L), data plane: FIPS SE image variant, HAProxy on system OpenSSL, sidecars rebuilt with Go FIPS, BGP auth redesign, dnsdist rebuild.
5. Phase 3 (M), platform: OpenBao, etcd, Patroni, Postgres on FIPS bases, Helm `fips.enabled`, digest pinning, lab e2e on `fips=1` kernels.

Overall: roughly 2-3 engineer-months to run on FIPS-validated modules if we accept dropping LXC and Debian SEs. BGP MD5 needs a product decision.

Open checks: current CMVP status of the chosen OpenSSL/Go module versions; TCP-AO support in FRR and the SE kernel; reproducible source builds of `grpcio` under uv.

SBOM/SPDX is tracked separately and is not covered here.
