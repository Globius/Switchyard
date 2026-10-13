import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { bytes, ControllerMembers } from "@/features/admin/ControllerMembers";

const member = (o: object) => ({
  id: "m0",
  name: "ctl-1",
  address: "10.0.0.11",
  platform: "proxmox",
  host: "pve1",
  external: false,
  nofailover: false,
  patroni_url: "http://10.0.0.11:8008",
  created_by: "syctl adopt",
  created_at: "2026-10-12T09:00:00Z",
  version: 1,
  status: {
    database_role: "leader",
    database_state: "running",
    lag_bytes: null,
    timeline: 4,
    nofailover: false,
  },
  in_cluster: true,
  ...o,
});
const esxi = member({
  id: "m3",
  name: "ctl-esxi",
  address: "10.0.0.13",
  platform: "esxi",
  host: "esxi-01",
  external: true,
  nofailover: true,
  status: {
    database_role: "replica",
    database_state: "streaming",
    lag_bytes: 2048,
    timeline: 4,
    nofailover: true,
  },
});

let view: object = {};
const calls: { method: string; url: string; body: unknown; ifMatch: string | null }[] = [];
const server = setupServer(
  http.get("*/api/v1/system/controller/members", () => HttpResponse.json(view)),
  http.post("*/api/v1/system/controller/members", async ({ request }) => {
    calls.push({
      method: "POST",
      url: request.url,
      body: await request.json(),
      ifMatch: null,
    });
    return HttpResponse.json(esxi, { status: 201 });
  }),
  http.patch("*/api/v1/system/controller/members/:id", async ({ request }) => {
    calls.push({
      method: "PATCH",
      url: request.url,
      body: await request.json(),
      ifMatch: request.headers.get("if-match"),
    });
    return HttpResponse.json(esxi);
  }),
  http.delete("*/api/v1/system/controller/members/:id", ({ request }) => {
    calls.push({
      method: "DELETE",
      url: request.url,
      body: null,
      ifMatch: request.headers.get("if-match"),
    });
    return new HttpResponse(null, { status: 204 });
  }),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  calls.length = 0;
});
afterAll(() => server.close());

function renderMembers() {
  render(
    <QueryClientProvider client={createQueryClient()}>
      <ControllerMembers />
    </QueryClientProvider>,
  );
}

const live = () => ({
  members: [member({}), esxi],
  cluster_reachable: true,
  cluster_error: null,
  leader: "ctl-1",
});

describe("bytes", () => {
  it("writes lag in readable units", () => {
    expect(bytes(0)).toBe("0 B");
    expect(bytes(512)).toBe("512 B");
    expect(bytes(2048)).toBe("2.0 KiB");
    expect(bytes(3.5 * 1024 * 1024)).toBe("3.5 MiB");
    expect(bytes(5 * 1024 ** 5)).toBe("5120.0 TiB");
  });
});

describe("controller cluster members", () => {
  it("shows the ESXi VM as external, never primary, beside the Proxmox one", async () => {
    view = live();
    renderMembers();
    const row = (await screen.findByText("ctl-esxi")).closest("tr")!;
    expect(within(row).getByText("External")).toBeInTheDocument();
    expect(within(row).getByText(/ESXi/)).toBeInTheDocument();
    expect(within(row).getByText("Replica")).toBeInTheDocument();
    expect(within(row).getByText("2.0 KiB")).toBeInTheDocument();
    expect(within(row).getByText("Never primary")).toBeInTheDocument();
    const first = screen.getByText("ctl-1").closest("tr")!;
    expect(within(first).queryByText("External")).not.toBeInTheDocument();
    expect(within(first).getByText("Primary")).toBeInTheDocument();
    expect(within(first).getByText("Can take over")).toBeInTheDocument();
    expect(screen.getByText("Primary right now: ctl-1.")).toBeInTheDocument();
  });

  it("flags a member Patroni does not list", async () => {
    const lost = member({
      name: "ctl-2",
      id: "m2",
      in_cluster: false,
      status: {
        database_role: null,
        database_state: null,
        lag_bytes: null,
        timeline: null,
        nofailover: null,
      },
    });
    view = { ...live(), members: [member({}), lost] };
    renderMembers();
    const row = (await screen.findByText("ctl-2")).closest("tr")!;
    expect(within(row).getByText("Not in the cluster")).toBeInTheDocument();
  });

  it("says so when no Patroni answers, and when none is configured", async () => {
    view = {
      members: [member({ in_cluster: null })],
      cluster_reachable: false,
      cluster_error: "http://10.0.0.11:8008: connection refused",
      leader: null,
    };
    renderMembers();
    expect(await screen.findByText(/Patroni did not answer/)).toBeInTheDocument();
    expect(screen.getByText(/connection refused/)).toBeInTheDocument();
  });

  it("explains a list without Patroni URLs", async () => {
    view = {
      members: [member({ in_cluster: null, patroni_url: null })],
      cluster_reachable: null,
      cluster_error: null,
      leader: null,
    };
    renderMembers();
    expect(await screen.findByText(/No member has a Patroni URL/)).toBeInTheDocument();
  });

  it("shows an empty state", async () => {
    view = { members: [], cluster_reachable: null, cluster_error: null, leader: null };
    renderMembers();
    expect(await screen.findByText("No controller VMs listed")).toBeInTheDocument();
  });

  it("adds the external VM", async () => {
    view = { members: [], cluster_reachable: null, cluster_error: null, leader: null };
    renderMembers();
    await userEvent.click(await screen.findByRole("button", { name: "Add a VM" }));
    await userEvent.type(screen.getByLabelText("Name"), "ctl-esxi");
    await userEvent.type(screen.getByLabelText("Address"), "10.0.0.13");
    await userEvent.selectOptions(screen.getByLabelText("Runs on"), "esxi");
    expect(screen.getByText(/shown as an external member/)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Host"), "esxi-01");
    await userEvent.type(screen.getByLabelText("Patroni URL"), "http://10.0.0.13:8008");
    await userEvent.click(screen.getByRole("switch", { name: "Never becomes the primary" }));
    await userEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]?.body).toEqual({
      name: "ctl-esxi",
      address: "10.0.0.13",
      platform: "esxi",
      host: "esxi-01",
      nofailover: true,
      patroni_url: "http://10.0.0.13:8008",
    });
  });

  it("shows the API's reason next to the field", async () => {
    view = { members: [], cluster_reachable: null, cluster_error: null, leader: null };
    server.use(
      http.post("*/api/v1/system/controller/members", () =>
        HttpResponse.json(
          {
            title: "Invalid",
            status: 422,
            errors: [
              {
                path: "address",
                code: "invalid",
                message: "127.0.0.1 is not an address of another machine",
              },
            ],
          },
          { status: 422 },
        ),
      ),
    );
    renderMembers();
    await userEvent.click(await screen.findByRole("button", { name: "Add a VM" }));
    await userEvent.type(screen.getByLabelText("Name"), "x");
    await userEvent.type(screen.getByLabelText("Address"), "127.0.0.1");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(await screen.findByText(/not an address of another machine/)).toBeInTheDocument();
  });

  it("edits with the version it read, and keeps the name fixed", async () => {
    view = live();
    renderMembers();
    await userEvent.click(await screen.findByRole("button", { name: "Edit ctl-esxi" }));
    expect(screen.getByLabelText("Name")).toBeDisabled();
    await userEvent.clear(screen.getByLabelText("Host"));
    await userEvent.type(screen.getByLabelText("Host"), "esxi-02");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]?.ifMatch).toBe("1");
    expect(calls[0]?.body).toMatchObject({ host: "esxi-02", platform: "esxi", nofailover: true });
  });

  it("asks before removing and sends the version", async () => {
    view = live();
    renderMembers();
    await userEvent.click(await screen.findByRole("button", { name: "Remove ctl-esxi" }));
    expect(screen.getByText(/only forgets the entry/)).toBeInTheDocument();
    await userEvent.click(screen.getAllByRole("button", { name: "Remove" }).at(-1)!);
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]).toMatchObject({ method: "DELETE", ifMatch: "1" });
  });
});
