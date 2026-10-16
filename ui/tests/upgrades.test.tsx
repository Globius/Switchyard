// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { buildTarget } from "@/features/admin/BackupTargets";
import { formatKeys, headline, parseKeys, type Check } from "@/features/admin/Upgrades";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

const CHECK: Check = {
  controller_version: "0.1.0",
  latest_version: "0.2.0",
  schema_current: "0046",
  schema_head: "0046",
  schema_state: "current",
  engines: [{ engine_id: "e1", name: "edge-1", tenant_id: "t1", agent_version: "0.1.0" }],
  findings: [
    {
      code: "backup_missing",
      severity: "warning",
      subject: "backup",
      message: "No configuration backup has been taken.",
      action: "Add a backup target and run a backup before upgrading.",
    },
    {
      code: "update_available",
      severity: "warning",
      subject: "controller",
      message: "Version 0.2.0 is available; this controller runs 0.1.0.",
      action: "Back up, read the release notes, then upgrade (docs/upgrades.md).",
    },
  ],
  update: {
    version: "0.2.0",
    date: "2026-10-20",
    notes_url: "https://example.test/notes",
    notes: {
      new: ["Upgrade checks"],
      deprecated: [],
      breaking: ["Removed the old flag"],
      action_required: ["Back up first"],
    },
    path: ["0.1.5", "0.2.0"],
    bundle: { url: "https://example.test/b.sybundle", sha256: "0".repeat(64), size_bytes: 2048 },
  },
};

const STAGED = {
  name: "switchyard-0.2.0-0123456789ab.sybundle",
  version: "0.2.0",
  size_bytes: 2048,
  sha256: "0".repeat(64),
  schema_head: "0047",
  key_id: "release",
  notes: { new: [], deprecated: [], breaking: ["Dropped the legacy flag"], action_required: [] },
  files: [],
};
let bundles: (typeof STAGED)[] = [];
let feedChannel: "stable" | "candidate" | "nightly" = "stable";

const calls: Record<string, unknown> = {};
const server = setupServer(
  http.get("*/api/v1/me", () =>
    HttpResponse.json({
      auth: "session",
      break_glass: false,
      csrf_token: "tok",
      email: null,
      id: "s",
      is_system: true,
      name: "Miguel",
      principal: "Miguel",
      session: null,
      tenants: [{ id: "t1", name: "globius-prod", role: "system-admin", via: [] }],
    }),
  ),
  http.get("*/api/v1/permissions", () =>
    HttpResponse.json({
      roles: ["viewer", "operator", "tenant-admin", "system-admin"],
      permissions: [],
    }),
  ),
  http.get("*/api/v1/auth/providers", () =>
    HttpResponse.json({ break_glass: false, providers: [] }),
  ),
  http.get("*/api/v1/tenants", () => HttpResponse.json([])),
  http.get("*/api/v1/system/upgrade/check", () => HttpResponse.json(CHECK)),
  http.get("*/api/v1/system/backup-targets", () =>
    HttpResponse.json([
      {
        name: "nightly",
        passphrase_ref: "secret/b/p",
        keep: 14,
        target: { kind: "local", path: "" },
      },
    ]),
  ),
  http.post("*/api/v1/system/backup-targets", async ({ request }) => {
    calls.targetAdded = await request.json();
    return HttpResponse.json(calls.targetAdded as Record<string, unknown>, { status: 201 });
  }),
  http.post("*/api/v1/system/backup-targets/:name/test", ({ params }) => {
    calls.targetTested = params.name;
    return HttpResponse.json({ archives: ["a.syb", "b.syb"] });
  }),
  http.delete("*/api/v1/system/backup-targets/:name", ({ params }) => {
    calls.targetDeleted = params.name;
    return new HttpResponse(null, { status: 204 });
  }),
  http.get("*/api/v1/system/backups", () =>
    HttpResponse.json([
      {
        id: "r1",
        target: "nightly",
        archive: "switchyard-0.1.0-20261013T040000Z.syb",
        status: "failed",
        error: "no secret at secret/b/p",
        size_bytes: 0,
        sha256: null,
        controller_version: "0.1.0",
        schema_revision: "0046",
        tenants: 0,
        reason: "pre-upgrade",
        actor: "user:root",
        created_at: "2026-10-13T04:00:00Z",
      },
    ]),
  ),
  http.post("*/api/v1/system/backups", async ({ request }) => {
    calls.backup = await request.json();
    return HttpResponse.json({ ...CHECK, id: "r2" }, { status: 201 });
  }),
  http.get("*/api/v1/system/releases", () =>
    HttpResponse.json({
      url: "https://git.example.test/releases.json",
      channel: feedChannel,
      enabled: true,
      trusted_keys: {},
      checked_at: "2026-10-13T04:00:00Z",
      ok: true,
      error: null,
      latest_version: "0.2.0",
      releases: [],
    }),
  ),
  http.get("*/api/v1/system/bundles", () => HttpResponse.json(bundles)),
  http.get("*/api/v1/system/bundles/:name", () =>
    HttpResponse.json({
      bundle: STAGED,
      running_version: "0.1.0",
      steps: ["Back up the configuration (Back up now) and the database (pgBackRest)."],
      warnings: [
        "This bundle installs over 0.1.5; you run 0.1.0. Go through the intermediate releases first.",
      ],
    }),
  ),
  http.put("*/api/v1/system/bundles", async ({ request }) => {
    calls.upload = (await request.arrayBuffer()).byteLength;
    calls.uploadType = request.headers.get("content-type");
    bundles.push(STAGED);
    return HttpResponse.json(STAGED, { status: 201 });
  }),
  http.post("*/api/v1/system/bundles/fetch", async ({ request }) => {
    calls.fetch = await request.json();
    return HttpResponse.json(STAGED, { status: 201 });
  }),
  http.delete("*/api/v1/system/bundles/:name", ({ params }) => {
    calls.deleted = params.name;
    return new HttpResponse(null, { status: 204 });
  }),
  http.put("*/api/v1/system/releases", async ({ request }) => {
    calls.feedSaved = await request.json();
    return HttpResponse.json({});
  }),
  http.post("*/api/v1/system/releases/check", () => {
    calls.checked = true;
    return HttpResponse.json({});
  }),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  bundles = [];
  feedChannel = "stable";
  for (const k of Object.keys(calls)) delete calls[k];
});
afterAll(() => server.close());

function renderAt(path: string) {
  const router = createRouter({
    routeTree,
    history: createMemoryHistory({ initialEntries: [path] }),
  });
  render(
    <ThemeProvider>
      <QueryClientProvider client={createQueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </ThemeProvider>,
  );
}

describe("upgrades page", () => {
  it("summarizes in one line, worst first", () => {
    expect(headline({ ...CHECK, update: null, findings: [] }).text).toBe("Up to date");
    expect(headline(CHECK).text).toBe("Version 0.2.0 is available. Upgrading is recommended.");
    const bad = { ...CHECK, findings: [{ ...CHECK.findings[0]!, severity: "error" as const }] };
    expect(headline(bad).tone).toBe("down");
  });

  it("shows versions, the recommendation, the notes and the install order", async () => {
    renderAt("/admin/upgrades");
    expect(await screen.findByRole("heading", { name: "Upgrades" })).toBeInTheDocument();
    expect(
      await screen.findByText("Version 0.2.0 is available. Upgrading is recommended."),
    ).toBeInTheDocument();
    expect(screen.getByText("Install in this order: 0.1.5 → 0.2.0")).toBeInTheDocument();
    expect(screen.getByText("Removed the old flag")).toBeInTheDocument();
    expect(screen.getByText("Back up first")).toBeInTheDocument();
    expect(screen.getByText(/edge-1 0.1.0/)).toBeInTheDocument();
    expect(screen.getByText(/No configuration backup has been taken./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Release notes" })).toHaveAttribute(
      "href",
      "https://example.test/notes",
    );
    expect(await screen.findByText("no secret at secret/b/p")).toBeInTheDocument();
  });

  it("backs up now to the chosen target and checks for releases on request", async () => {
    renderAt("/admin/upgrades");
    await userEvent.click(await screen.findByRole("button", { name: "Back up now" }));
    await waitFor(() => expect(calls.backup).toEqual({ target: "nightly", reason: "pre-upgrade" }));
    await userEvent.click(screen.getByRole("button", { name: "Check now" }));
    await waitFor(() => expect(calls.checked).toBe(true));
  });

  it("has a nav entry", async () => {
    renderAt("/admin/upgrades");
    expect(await screen.findByRole("link", { name: "Upgrades" })).toHaveAttribute(
      "href",
      "/admin/upgrades",
    );
  });
});

describe("backup targets", () => {
  it("builds the body per kind and rejects bad input", () => {
    expect(
      buildTarget("n", "secret/p", "7", "s3", {
        endpoint: "https://s3.example.test",
        bucket: "bk",
        access_key_ref: "secret/a",
        secret_key_ref: "secret/s",
      }),
    ).toEqual({
      body: {
        name: "n",
        passphrase_ref: "secret/p",
        keep: 7,
        target: {
          kind: "s3",
          endpoint: "https://s3.example.test",
          bucket: "bk",
          access_key_ref: "secret/a",
          secret_key_ref: "secret/s",
        },
      },
    });
    expect(buildTarget("n", "p", "0", "local", {})).toHaveProperty("error");
    expect(buildTarget("n", "p", "5", "ftp", { host: "h" })).toHaveProperty("error");
    const ftp = buildTarget("n", "p", "5", "ftp", {
      host: "h",
      username: "u",
      password_ref: "r",
      port: "2121",
    });
    expect(ftp).toHaveProperty("body.target.port", 2121);
    expect(ftp).toHaveProperty("body.target.tls", true);
  });

  it("adds a target, with help text about references", async () => {
    renderAt("/admin/upgrades");
    await userEvent.click(await screen.findByRole("button", { name: "Add target" }));
    expect(screen.getByRole("note")).toHaveTextContent(/never the secret values/);
    await userEvent.type(screen.getByLabelText("Name"), "offsite");
    await userEvent.type(screen.getByLabelText("Passphrase reference"), "secret/backup/pass");
    await userEvent.click(screen.getAllByRole("button", { name: "Add target" }).at(-1)!);
    await waitFor(() =>
      expect(calls.targetAdded).toEqual({
        name: "offsite",
        passphrase_ref: "secret/backup/pass",
        keep: 14,
        target: { kind: "local" },
      }),
    );
  });

  it("tests and deletes a target after confirmation", async () => {
    renderAt("/admin/upgrades");
    await userEvent.click(await screen.findByRole("button", { name: "Test nightly" }));
    expect(await screen.findByText("Works. 2 archives found.")).toBeInTheDocument();
    expect(calls.targetTested).toBe("nightly");
    await userEvent.click(screen.getByRole("button", { name: "Delete nightly" }));
    expect(calls.targetDeleted).toBeUndefined();
    await userEvent.click(screen.getByRole("button", { name: "Confirm delete nightly" }));
    await waitFor(() => expect(calls.targetDeleted).toBe("nightly"));
  });

  it("links the missing-backup finding to the section", async () => {
    renderAt("/admin/upgrades");
    expect(await screen.findByRole("link", { name: "Go to backup targets" })).toHaveAttribute(
      "href",
      "#backup-targets",
    );
  });
});

describe("upgrade bundles", () => {
  it("uploads a file as the raw body and lists it as verified", async () => {
    renderAt("/admin/upgrades");
    expect(await screen.findByText("No bundles staged.")).toBeInTheDocument();
    const file = new File([new Uint8Array(2048)], "b.sybundle");
    await userEvent.upload(await screen.findByLabelText("Bundle file"), file);
    await waitFor(() => expect(calls.upload).toBe(2048));
    expect(calls.uploadType).toBe("application/octet-stream");
    expect(await screen.findByText("Verified")).toBeInTheDocument();
  });

  it("shows the plan, its cautions and the command for each node", async () => {
    bundles = [STAGED];
    renderAt("/admin/upgrades");
    await userEvent.click(await screen.findByRole("button", { name: "Plan" }));
    expect(await screen.findByText(/installs over 0.1.5/)).toBeInTheDocument();
    expect(screen.getByText("Dropped the legacy flag")).toBeInTheDocument();
    expect(screen.getByText(/syctl upgrade apply switchyard-0.2.0.sybundle/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download" })).toHaveAttribute(
      "href",
      "/api/v1/system/bundles/switchyard-0.2.0-0123456789ab.sybundle/file",
    );
  });

  it("fetches the feed's bundle and removes a staged one after confirmation", async () => {
    renderAt("/admin/upgrades");
    await userEvent.click(
      await screen.findByRole("button", { name: "Download 0.2.0 from the feed" }),
    );
    await waitFor(() => expect(calls.fetch).toEqual({ version: "0.2.0" }));
    bundles = [STAGED];
    renderAt("/admin/upgrades");
    const remove = await screen.findAllByRole("button", { name: "Remove" });
    await userEvent.click(remove[0]!);
    expect(calls.deleted).toBeUndefined();
    await userEvent.click((await screen.findAllByRole("button", { name: "Remove" })).at(-1)!);
    await waitFor(() => expect(calls.deleted).toBe("switchyard-0.2.0-0123456789ab.sybundle"));
  });
});

describe("release feed channels and keys", () => {
  it("parses and prints trusted keys, one per line", () => {
    const key = "A".repeat(43) + "=";
    expect(parseKeys(`# note\nnightly=${key}\n\n`)).toEqual({ keys: { nightly: key } });
    expect(parseKeys("nightly=short")).toHaveProperty("error");
    expect(parseKeys("no-equals")).toHaveProperty("error");
    expect(formatKeys({ a: key })).toBe(`a=${key}`);
    expect(formatKeys(undefined)).toBe("");
  });

  it("warns about the nightly channel and saves a trusted key with the channel kept", async () => {
    feedChannel = "nightly";
    renderAt("/admin/upgrades");
    expect(await screen.findByText(/Nightly builds are not releases/)).toBeInTheDocument();
    const box = await screen.findByPlaceholderText("nightly=BASE64PUBLICKEY=");
    const key = "B".repeat(43) + "=";
    await userEvent.type(box, `nightly=${key}`);
    await userEvent.click(screen.getByRole("button", { name: "Save keys" }));
    await waitFor(() =>
      expect(calls.feedSaved).toMatchObject({ channel: "nightly", trusted_keys: { nightly: key } }),
    );
  });

  it("refuses a malformed key line before sending anything", async () => {
    renderAt("/admin/upgrades");
    await userEvent.type(await screen.findByPlaceholderText("nightly=BASE64PUBLICKEY="), "oops");
    await userEvent.click(screen.getByRole("button", { name: "Save keys" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Use one key per line");
    expect(calls.feedSaved).toBeUndefined();
  });
});
