"""Turn a JSON Web Key Set into the ``keys`` of a ``jwt`` policy set (ADR 0135).

HAProxy verifies tokens with a PEM public key, while identity providers publish JWKS. This module
converts the second into the first. It does no network I/O: the operator downloads the JWKS (or
the controller job of a later block does) and passes the text in. Private key parameters are
refused, so a JWKS that leaked a private key never lands in a policy set.
"""

import base64
import json
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa


class JwksError(ValueError):
    """The JWKS cannot be converted."""


_CURVES = {
    "P-256": (ec.SECP256R1(), "ES256"),
    "P-384": (ec.SECP384R1(), "ES384"),
    "P-521": (ec.SECP521R1(), "ES512"),
}
_PRIVATE_PARAMS = {"d", "p", "q", "dp", "dq", "qi", "k"}


def _int(value: Any, what: str) -> int:
    if not isinstance(value, str):
        raise JwksError(f"{what} is missing")
    padded = value + "=" * (-len(value) % 4)
    try:
        return int.from_bytes(base64.urlsafe_b64decode(padded), "big")
    except ValueError as exc:
        raise JwksError(f"{what} is not base64url") from exc


def jwk_to_key(jwk: dict[str, Any]) -> dict[str, str]:
    """Convert one JWK to ``{kid?, alg, public_key_pem}``.

    Supports RSA (``RS256`` unless the key names another RS or PS algorithm) and EC keys on P-256,
    P-384 and P-521.

    Example:
        >>> jwk_to_key({"kty": "oct", "k": "AAAA"})
        Traceback (most recent call last):
            ...
        switchyard.services.jwks.JwksError: key type 'oct' is not supported (use RSA or EC)
    """
    kty = jwk.get("kty")
    if kty not in ("RSA", "EC"):
        raise JwksError(f"key type {kty!r} is not supported (use RSA or EC)")
    if _PRIVATE_PARAMS & set(jwk):
        raise JwksError("the JWKS contains private key parameters; publish only the public keys")
    alg: str
    public: rsa.RSAPublicKey | ec.EllipticCurvePublicKey
    if kty == "RSA":
        alg = str(jwk.get("alg") or "RS256")
        if alg not in {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512"}:
            raise JwksError(f"algorithm {alg!r} does not fit an RSA key")
        public = rsa.RSAPublicNumbers(_int(jwk.get("e"), "e"), _int(jwk.get("n"), "n")).public_key()
    elif kty == "EC":
        curve, default_alg = _CURVES.get(str(jwk.get("crv")), (None, ""))
        if curve is None:
            raise JwksError(f"curve {jwk.get('crv')!r} is not supported")
        alg = str(jwk.get("alg") or default_alg)
        if alg != default_alg:
            raise JwksError(f"algorithm {alg!r} does not fit curve {jwk.get('crv')}")
        public = ec.EllipticCurvePublicNumbers(
            _int(jwk.get("x"), "x"), _int(jwk.get("y"), "y"), curve
        ).public_key()
    else:  # pragma: no cover - rejected above
        raise JwksError(f"key type {kty!r} is not supported (use RSA or EC)")
    pem = public.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    key = {"alg": alg, "public_key_pem": pem}
    if isinstance(jwk.get("kid"), str):
        key["kid"] = jwk["kid"]
    return key


def jwks_to_keys(text: str) -> list[dict[str, str]]:
    """Convert a JWKS document to policy-set keys, skipping keys meant for encryption.

    Raises:
        JwksError: If the text is not a JWKS, holds no usable key, or holds a private key.

    Example:
        >>> jwks_to_keys("{}")
        Traceback (most recent call last):
            ...
        switchyard.services.jwks.JwksError: the document has no keys
    """
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise JwksError("not JSON") from exc
    keys = doc.get("keys") if isinstance(doc, dict) else None
    if not isinstance(keys, list) or not keys:
        raise JwksError("the document has no keys")
    out = [jwk_to_key(k) for k in keys if isinstance(k, dict) and k.get("use", "sig") == "sig"]
    if not out:
        raise JwksError("the document has no signing keys")
    if len(out) > 8:
        raise JwksError("a rule takes at most 8 keys; remove retired keys first")
    return out
