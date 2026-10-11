"""API steps of the end-to-end lab. Runs inside the controller container: python - <phase> < drive.py

Phases: deploy (grow the group with LXC engines, print engine details and an enrollment token),
vs (create the virtual service and wait for the rollout), status (print engine status).
Environment: SY_TOKEN (admin API token). Prints one JSON object per phase.
"""

import asyncio
import json
import os
import ssl
import sys

import httpx

from switchyard.client import ApiClient, ApiClientError, Profile

TENANT = "lab"


class Lab(ApiClient):
    def __init__(self) -> None:
        super().__init__(Profile("https://127.0.0.1:8443", os.environ["SY_TOKEN"]))
        self._http._transport = httpx.AsyncHTTPTransport(verify=ssl.create_default_context(cafile="/e2e/ca.pem"))  # noqa: SLF001

    async def get(self, path: str) -> object:
        return await self.request("GET", f"/tenants/{TENANT}{path}")

    async def find(self, collection: str, name: str) -> dict:
        page = await self.get(f"/{collection}?limit=200")
        items = page["items"] if isinstance(page, dict) else page
        return next(i for i in items if i["name"] == name)


async def deploy(lab: Lab) -> dict:
    group = await lab.find("se-groups", "edge")
    pool = await lab.find("vip-pools", "mgmt")
    accepted = await lab.request(
        "POST",
        f"/tenants/{TENANT}/se-groups/{group['id']}/deploy",
        json_body={
            "count": 1,
            "nodes": ["pve"],
            "template_vmid": int(os.environ["TEMPLATE_VMID"]),
            "mgmt_pool_id": pool["id"],
            "credentials_ref": "kv/se/e2e-dpapi",
            "guest_kind": "lxc",
        },
    )
    await lab.wait_job(accepted["job_id"], timeout_s=600)
    found = await lab.get(f"/engines?se_group={group['id']}")
    engine = (found["items"] if isinstance(found, dict) else found)[0]
    token = await lab.request("POST", f"/tenants/{TENANT}/engines/{engine['id']}/enrollment-token")
    return {"engine": engine, "token": token}


async def vs(lab: Lab) -> dict:
    pool = await lab.find("pools", "controller-api")
    group = await lab.find("se-groups", "edge")
    vips = await lab.find("vip-pools", "vips")
    body = {
        "name": "api",
        "protocol": "http",
        "se_group_id": group["id"],
        "vip": "172.30.0.240",
        "vip_pool_id": vips["id"],
        "ports": [{"port": 80}],
        "default_pool_id": pool["id"],
    }
    accepted = await lab.request(
        "POST", f"/tenants/{TENANT}/virtual-services", json_body=body, params={"apply": "true"}
    )
    await lab.wait_job(accepted["job_id"], timeout_s=300)
    return await lab.get(f"/virtual-services/{accepted['object_id']}")


async def reapply(lab: Lab) -> dict:
    """Apply the newest failed change set again (after fixing the engine)."""
    page = await lab.get("/change-sets?limit=20")
    failed = next(c for c in (page["items"] if isinstance(page, dict) else page) if c["state"] == "failed")
    accepted = await lab.request("POST", f"/tenants/{TENANT}/change-sets/{failed['id']}/apply", json_body={})
    await lab.wait_job(accepted["job_id"], timeout_s=300)
    done = await lab.get(f"/change-sets/{failed['id']}")
    return {"state": done["state"], "error": done.get("error")}


async def status(lab: Lab) -> dict:
    return await lab.get("/status/engines")


async def main() -> None:
    lab = Lab()
    try:
        out = await {"deploy": deploy, "vs": vs, "status": status, "reapply": reapply}[sys.argv[1]](lab)
    except ApiClientError as exc:
        out = {"error": str(exc), "errors": exc.errors}
    finally:
        await lab.close()
    print(json.dumps(out, default=str))


asyncio.run(main())
