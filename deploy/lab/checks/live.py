"""Live checks of two features that were only ever tested with mocked responses (docs/design/06). Runs inside the
controller container against a real stack: python - <command> < live.py
  p12        import a PKCS#12 with a password, a wrong password, a file without a password, then replace it
  conflict   provoke a 412 with a stale If-Match and read the conflict document the UI's resolver needs
Environment: SY_TOKEN, LAB_URL, LAB_CA, LAB_TENANT (a tenant with at least one health monitor). Exit 1 on a failed check.
"""

import asyncio
import base64
import datetime
import os
import ssl
import sys

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from switchyard.client import ApiClient, ApiClientError, Profile

TENANT = os.environ.get("LAB_TENANT", "lab")


class Live(ApiClient):
    def __init__(self) -> None:
        super().__init__(Profile(os.environ.get("LAB_URL", "https://127.0.0.1:8443"), os.environ["SY_TOKEN"]))
        ctx = ssl.create_default_context(cafile=os.environ.get("LAB_CA", "/etc/ssl/certs/ca-certificates.crt"))
        self._http._transport = httpx.AsyncHTTPTransport(verify=ctx)  # noqa: SLF001


def check(ok: bool, what: str) -> None:
    print(("ok    " if ok else "FAIL  ") + what)
    if not ok:
        sys.exit(1)


def make_p12(name: str, password: bytes | None) -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=90))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    enc = (
        serialization.BestAvailableEncryption(password)
        if password
        else serialization.NoEncryption()
    )
    return pkcs12.serialize_key_and_certificates(name.encode(), key, cert, None, enc)


async def p12(live: Live) -> None:
    stamp = str(int(datetime.datetime.now().timestamp()))
    base = f"/tenants/{TENANT}/certificates"
    name = f"live-p12-{stamp}"
    data = base64.b64encode(make_p12(f"{name}.example.test", b"s3cret-pw")).decode()
    done = await live.request(
        "POST", f"{base}/import", json_body={"name": name, "pkcs12_b64": data, "password": "s3cret-pw"}, params={"apply": "true"}
    )
    await live.wait_job(done["job_id"], timeout_s=120)
    cert = await live.request("GET", f"{base}/{done['object_id']}")
    check(cert["sans"] == [f"{name}.example.test"] and cert["source"] == "upload", "PKCS#12 with a password imports; SANs and source read back")
    try:
        await live.request("POST", f"{base}/import", json_body={"name": name + "-bad", "pkcs12_b64": data, "password": "wrong"})
        check(False, "a wrong password is refused")
    except ApiClientError as exc:
        check(exc.status == 422 and any(e.get("path") == "password" for e in exc.errors), "a wrong password is a 422 on `password`")
    plain = base64.b64encode(make_p12(f"{name}-np.example.test", None)).decode()
    done2 = await live.request("POST", f"{base}/import", json_body={"name": name + "-np", "pkcs12_b64": plain}, params={"apply": "true"})
    await live.wait_job(done2["job_id"], timeout_s=120)
    check(True, "PKCS#12 without a password imports")
    renewed = base64.b64encode(make_p12(f"{name}.example.test", b"other-pw")).decode()
    rep = await live.request(
        "POST", f"{base}/{done['object_id']}/replace", json_body={"pkcs12_b64": renewed, "password": "other-pw"}, params={"apply": "true"}
    )
    await live.wait_job(rep["job_id"], timeout_s=120)
    after = await live.request("GET", f"{base}/{done['object_id']}")
    check(after["id"] == cert["id"] and after["secret_ref"] != cert["secret_ref"], "replace keeps the object and changes the secret")


async def conflict(live: Live) -> None:
    page = await live.request("GET", f"/tenants/{TENANT}/health-monitors?limit=1")
    item = (page["items"] if isinstance(page, dict) else page)[0]
    url = f"/tenants/{TENANT}/health-monitors/{item['id']}"
    stale = f'"{item["version"] + 7}"'
    r = await live._http.request(  # noqa: SLF001  (the client hides the problem's `conflict` member)
        "PATCH", url, json={"name": item["name"]}, headers={"If-Match": stale}, params={"apply": "true"}
    )
    check(r.status_code == 412, "a stale If-Match is a 412")
    doc = r.json().get("conflict") or {}
    check(
        doc.get("object_id") == item["id"] and doc.get("current_version") == item["version"],
        "the 412 carries the conflict document (object, current version) the resolver reads",
    )
    r = await live._http.request("PATCH", url, json={"name": item["name"]}, params={"apply": "true"})
    check(r.status_code == 428, "a missing If-Match is a 428")


asyncio.run({"p12": p12, "conflict": conflict}[sys.argv[1]](Live()))
