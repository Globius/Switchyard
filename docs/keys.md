# Signing keys

The public halves of the keys the project signs with (ADR 0205, 0206). They are public: they only let you check a signature, never make one.
Base64 is the raw 32-byte Ed25519 key, the form `trusted_keys` and `--trust id=` take.

| Key id | Base64 public key | What it signs | Trusted by default |
| --- | --- | --- | --- |
| `release-2026` | `Lr/O9yFbtWHxGa/ZO/U1ASnRHuFHFYu/TJka8C/o6do=` | manual and emergency releases (kept offline) | yes, built into every controller |
| `ci-release-2026` | `Agmhby6SCFD0+xxnencGqcICks5CxnOp3O4UrsNL+D0=` | tagged releases built by CI | yes, built into every controller |
| `nightly-2026` | `dbRNmz7pofWkMM0inV9MtPfcWD2EE+MsiDomHvBtcVc=` | nightly builds | **no**: add it yourself to follow the nightly channel |

## Following the nightlies

Admin > Upgrades > Looking for newer releases: set the feed address to the nightly feed, the channel to **nightly**, and under *Extra trusted
signing keys* add the line

```text
nightly-2026=dbRNmz7pofWkMM0inV9MtPfcWD2EE+MsiDomHvBtcVc=
```

On the command line the same key is `--trust nightly-2026=dbRNmz7pofWkMM0inV9MtPfcWD2EE+MsiDomHvBtcVc=` for `syctl release verify`, `syctl upgrade plan`
and `syctl upgrade apply`. Nightlies are not releases; see [upgrades.md](upgrades.md#channels-stable-candidate-nightly).

## Checking a key you were given

`docker run --rm -i alpine/openssl pkey -pubin -outform DER < key.pem | tail -c 32 | base64` prints the same base64 as the table. If the value you
have is not in this table, do not trust it.

Keys rotate (the id carries the year): see [runbooks/release.md](runbooks/release.md#rotating-or-losing-a-key). This page lists every key that is or
was valid, with its dates, once there is more than one generation.
