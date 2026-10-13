# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""API steps of the end-to-end lab. Runs inside the controller container: python - <command> [args] < drive.py

Commands:
  deploy GROUP COUNT [NODES [TEMPLATE [KIND]]]   grow the group to COUNT engines (KIND lxc or qemu) (data NICs addressed from pools); print the group's
                          engines, with a fresh enrollment token for every engine that has not enrolled
  vs NAME GROUP VIP [PROTOCOL [PORT [POOL]]]   create a virtual service (default http, port 80, pool `controller-api`)
  status                  engine status (reachability, members, VRRP, BGP, drift)
  cert NAME               import the PEM (chain, then key) in the CERT_PEM environment variable as certificate NAME
  vstls NAME GROUP VIP CERT [PORT [POOL]]   create an HTTPS virtual service (TLS on PORT, default 443) using certificate CERT
  retire ENGINE           retire the engine named ENGINE (destroys the guest) and wait for the job
  repair GROUP            push the current desired state to the group again (POST /se-groups/{id}/reapply), canary first
  engines GROUP           the engines of the group (name, node, vmid, kind, enrolled)
  health                  health scores of the SE groups and their engines (GET /status/se-groups)
  reapply                 apply the newest failed change set again
  lastapply               log and result of the newest apply job
Environment: SY_TOKEN (admin API token), TEMPLATE_VMID, LAB_TENANT (default lab). Prints one JSON document.
"""

import asyncio
import json
import os
import ssl
import sys

import httpx

from switchyard.client import ApiClient, ApiClientError, Profile

TENANT = os.environ.get("LAB_TENANT", "lab")


class Lab(ApiClient):
    def __init__(self) -> None:
        super().__init__(Profile(os.environ.get("LAB_URL", "https://127.0.0.1:8443"), os.environ["SY_TOKEN"]))
        ctx = ssl.create_default_context(cafile=os.environ.get("LAB_CA", "/e2e/ca.pem"))
        self._http._transport = httpx.AsyncHTTPTransport(verify=ctx)  # noqa: SLF001

    async def get(self, path: str) -> object:
        return await self.request("GET", f"/tenants/{TENANT}{path}")

    async def items(self, path: str) -> list[dict]:
        page = await self.get(path)
        return page["items"] if isinstance(page, dict) else page

    async def find(self, collection: str, name: str) -> dict:
        return next(i for i in await self.items(f"/{collection}?limit=200") if i["name"] == name)


async def deploy(
    lab: Lab, group_name: str, count: str, nodes: str = "", template: str = "", kind: str = ""
) -> dict:
    group = await lab.find("se-groups", group_name)
    pool = lambda name: lab.find("vip-pools", name)  # noqa: E731
    net = lambda name: lab.find("networks", name)  # noqa: E731
    accepted = await lab.request(
        "POST",
        f"/tenants/{TENANT}/se-groups/{group['id']}/deploy",
        json_body={
            "count": int(count),
            "nodes": (nodes or os.environ.get("LAB_NODES", "pve")).split(","),
            "template_vmid": int(template or os.environ["TEMPLATE_VMID"]),
            "mgmt_pool_id": (await pool("mgmt-addrs"))["id"],
            "nic_pools": {
                (await net("vip"))["id"]: (await pool("se-vip-addrs"))["id"],
                (await net("backend"))["id"]: (await pool("se-be-addrs"))["id"],
            },
            "credentials_ref": os.environ.get("CRED_REF", "kv/se/e2e-dpapi"),
            "guest_kind": kind or os.environ.get("GUEST_KIND", "lxc"),
        },
    )
    await lab.wait_job(accepted["job_id"], timeout_s=900)
    engines = await lab.items(f"/engines?se_group={group['id']}")
    for engine in engines:
        if not engine["agent_enrolled"]:
            engine["token"] = await lab.request(
                "POST", f"/tenants/{TENANT}/engines/{engine['id']}/enrollment-token"
            )
    return {"engines": engines}


async def vs(
    lab: Lab, name: str, group_name: str, vip: str, protocol: str = "http", port: str = "80", pool_name: str = "controller-api"
) -> dict:
    pool = await lab.find("pools", pool_name)
    group = await lab.find("se-groups", group_name)
    vips = await lab.find("vip-pools", "vips")
    body = {
        "name": name,
        "protocol": protocol,
        "se_group_id": group["id"],
        "vip": vip,
        "vip_pool_id": vips["id"],
        "ports": [{"port": int(port)}],
        "default_pool_id": pool["id"],
    }
    accepted = await lab.request(
        "POST", f"/tenants/{TENANT}/virtual-services", json_body=body, params={"apply": "true"}
    )
    await lab.wait_job(accepted["job_id"], timeout_s=300)
    created = await lab.get(f"/change-sets/{accepted['change_set_id']}")
    if created["state"] != "applied":
        return {"state": created["state"], "error": created.get("error")}
    return {"state": "applied", "object": await lab.get(f"/virtual-services/{accepted['object_id']}")}


async def cert(lab: Lab, name: str) -> dict:
    """Import the PEM bundle in ``CERT_PEM`` as a certificate."""
    accepted = await lab.request(
        "POST",
        f"/tenants/{TENANT}/certificates/import",
        json_body={"name": name, "pem": os.environ["CERT_PEM"]},
        params={"apply": "true"},
    )
    await lab.wait_job(accepted["job_id"], timeout_s=300)
    return await lab.get(f"/certificates/{accepted['object_id']}")


async def vstls(
    lab: Lab, name: str, group_name: str, vip: str, cert_name: str, port: str = "443", pool_name: str = "web"
) -> dict:
    """Create a virtual service that terminates TLS on ``port`` with the named certificate."""
    pool = await lab.find("pools", pool_name)
    group = await lab.find("se-groups", group_name)
    vips = await lab.find("vip-pools", "vips")
    certificate = await lab.find("certificates", cert_name)
    body = {
        "name": name,
        "protocol": "http",
        "se_group_id": group["id"],
        "vip": vip,
        "vip_pool_id": vips["id"],
        "ports": [{"port": int(port), "tls": True}],
        "certificate_ids": [certificate["id"]],
        "default_pool_id": pool["id"],
    }
    accepted = await lab.request(
        "POST", f"/tenants/{TENANT}/virtual-services", json_body=body, params={"apply": "true"}
    )
    await lab.wait_job(accepted["job_id"], timeout_s=300)
    created = await lab.get(f"/change-sets/{accepted['change_set_id']}")
    if created["state"] != "applied":
        return {"state": created["state"], "error": created.get("error")}
    return {"state": "applied", "object": await lab.get(f"/virtual-services/{accepted['object_id']}")}


async def retire(lab: Lab, engine_name: str) -> dict:
    """Retire an engine: the controller destroys its guest and removes the record."""
    engine = await lab.find("engines", engine_name)
    accepted = await lab.request("POST", f"/tenants/{TENANT}/engines/{engine['id']}/retire")
    await lab.wait_job(accepted["job_id"], timeout_s=600)
    return {"retired": engine_name}


async def repair(lab: Lab, group_name: str) -> dict:
    group = await lab.find("se-groups", group_name)
    accepted = await lab.request("POST", f"/tenants/{TENANT}/se-groups/{group['id']}/reapply")
    await lab.wait_job(accepted["job_id"], timeout_s=600)
    return {"repaired": group_name}


async def engines(lab: Lab, group_name: str) -> list:
    group = await lab.find("se-groups", group_name)
    rows = await lab.items(f"/engines?se_group={group['id']}")
    return [
        {
            "name": e["name"],
            "pve_node": e["pve_node"],
            "vmid": e["vmid"],
            "guest_kind": e["guest_kind"],
            "agent_enrolled": e["agent_enrolled"],
        }
        for e in rows
    ]


async def health(lab: Lab) -> list:
    return await lab.get("/status/se-groups")


async def recreate(lab: Lab, name: str, group_name: str, vip: str) -> dict:
    """Delete the virtual service and create it again (two rollouts: re-renders everything for the group)."""
    existing = await lab.find("virtual-services", name)
    deleted = await lab.request(
        "DELETE",
        f"/tenants/{TENANT}/virtual-services/{existing['id']}",
        params={"apply": "true"},
        headers={"If-Match": f'"{existing["version"]}"'},
    )
    await lab.wait_job(deleted["job_id"], timeout_s=300)
    return await vs(lab, name, group_name, vip)


async def reapply(lab: Lab) -> dict:
    """Apply the newest failed change set again (after fixing the engine or its peer)."""
    failed = next(c for c in await lab.items("/change-sets?limit=20") if c["state"] == "failed")
    accepted = await lab.request(
        "POST", f"/tenants/{TENANT}/change-sets/{failed['id']}/apply", json_body={}
    )
    await lab.wait_job(accepted["job_id"], timeout_s=300)
    done = await lab.get(f"/change-sets/{failed['id']}")
    return {"state": done["state"], "error": done.get("error")}


async def status(lab: Lab) -> list:
    return await lab.get("/status/engines")


async def lastapply(lab: Lab) -> dict:
    jobs = [j for j in await lab.items("/jobs?limit=50") if j["kind"] == "apply_change_set"]
    job = jobs[0]
    return {"job": job, "log": await lab.get(f"/jobs/{job['id']}/log")}


COMMANDS = {"repair": repair, "engines": engines, "retire": retire, "health": health, "cert": cert, "vstls": vstls, "deploy": deploy, "vs": vs, "status": status, "lastapply": lastapply, "reapply": reapply, "recreate": recreate}


async def main() -> None:
    lab = Lab()
    try:
        out = await COMMANDS[sys.argv[1]](lab, *sys.argv[2:])
    except ApiClientError as exc:
        out = {"error": str(exc), "errors": exc.errors}
    finally:
        await lab.close()
    print(json.dumps(out, default=str))


asyncio.run(main())
