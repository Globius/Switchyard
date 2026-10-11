/**
 * State, validation and ops of the create-virtual-service wizard. Pure, so it is tested without
 * rendering. Everything the wizard creates is sent as one atomic `POST /change-sets/{cs}/ops`.
 */
import type { components } from "@/api/schema";

export type Protocol = "http" | "tcp" | "udp";
export type Algorithm = components["schemas"]["Algorithm"];
export type OpIn = components["schemas"]["OpIn"];

export interface PortDraft {
  port: number;
  tls: boolean;
  http2: boolean;
  proxy_protocol: boolean;
  https_redirect_port: number | null;
}

export interface RouteDraft {
  host: string;
  kind: "prefix" | "exact";
  path: string;
  action: "pool" | "redirect";
  redirect_location: string;
  redirect_code: 301 | 302 | 303 | 307 | 308;
}

export interface MemberDraft {
  address: string;
  port: string;
  weight: number;
}

export interface MonitorDraft {
  name: string;
  type: "tcp" | "http" | "udp";
  path: string;
  expect_status: string;
  payload_hex: string;
  require_reply: boolean;
  interval_s: number;
  timeout_s: number;
  rise: number;
  fall: number;
}

export interface WizardState {
  name: string;
  protocol: Protocol;
  se_group_id: string;
  vipMode: "pool" | "fixed";
  vip_pool_id: string;
  vip: string;
  ports: PortDraft[];
  certificate_ids: string[];
  routes: RouteDraft[];
  /** Existing pool id, or "new" to create one in this wizard. */
  pool_choice: string;
  new_pool_name: string;
  algorithm: Algorithm;
  members: MemberDraft[];
  /** Existing monitor id, "new", or "" for none. */
  monitor_choice: string;
  monitor: MonitorDraft;
}

export const NAME_RE = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;
const IPV4 = /^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}$/;

export function initialState(): WizardState {
  return {
    name: "",
    protocol: "http",
    se_group_id: "",
    vipMode: "pool",
    vip_pool_id: "",
    vip: "",
    ports: [
      { port: 443, tls: true, http2: true, proxy_protocol: false, https_redirect_port: null },
    ],
    certificate_ids: [],
    routes: [],
    pool_choice: "new",
    new_pool_name: "",
    algorithm: "roundrobin",
    members: [{ address: "", port: "8080", weight: 1 }],
    monitor_choice: "new",
    monitor: {
      name: "",
      type: "tcp",
      path: "/healthz",
      expect_status: "200",
      payload_hex: "",
      require_reply: false,
      interval_s: 5,
      timeout_s: 3,
      rise: 2,
      fall: 3,
    },
  };
}

/** Port defaults when the protocol changes (UDP: no TLS, HTTP/2 or PROXY; TCP: no HTTP/2). */
export function withProtocol(s: WizardState, protocol: Protocol): WizardState {
  const ports = s.ports.map((p) => ({
    ...p,
    tls: protocol === "udp" ? false : p.tls,
    http2: protocol === "http" ? p.http2 : false,
    proxy_protocol: protocol === "udp" ? false : p.proxy_protocol,
    https_redirect_port: protocol === "http" ? p.https_redirect_port : null,
  }));
  return {
    ...s,
    protocol,
    ports,
    algorithm: protocol === "udp" && s.algorithm === "random" ? "source" : s.algorithm,
    monitor: {
      ...s.monitor,
      type: protocol === "udp" ? "udp" : s.monitor.type === "udp" ? "tcp" : s.monitor.type,
    },
    routes: protocol === "http" ? s.routes : [],
    certificate_ids: protocol === "udp" ? [] : s.certificate_ids,
  };
}

export const STEPS = [
  "Basics",
  "VIP",
  "Ports and TLS",
  "Routes and pools",
  "Health check",
  "Review",
] as const;

/** Step titles; the fourth is just "Pool" for protocols without routes. */
export function stepTitles(p: Protocol): string[] {
  return STEPS.map((s, i) => (i === 3 && p !== "http" ? "Pool" : s));
}

export type Errors = Record<string, string>;

/** Field errors for one step (1-based). Empty object means the step can continue. */
export function validateStep(step: number, s: WizardState, existingNames: Set<string>): Errors {
  const e: Errors = {};
  if (step === 1) {
    if (!NAME_RE.test(s.name))
      e.name = "Use lower case letters, digits and hyphens, 1 to 63 characters.";
    else if (existingNames.has(s.name)) e.name = "A virtual service with this name already exists.";
    if (!s.se_group_id) e.se_group_id = "Choose an SE group.";
  }
  if (step === 2) {
    if (s.vipMode === "pool" && !s.vip_pool_id) e.vip_pool_id = "Choose a VIP pool.";
    if (s.vipMode === "fixed" && !IPV4.test(s.vip) && !s.vip.includes(":"))
      e.vip = "Enter an IP address.";
  }
  if (step === 3) {
    if (s.ports.length === 0) e.ports = "Add at least one port.";
    s.ports.forEach((p, i) => {
      if (!Number.isInteger(p.port) || p.port < 1 || p.port > 65535)
        e[`ports.${i}.port`] = "Port must be 1–65535.";
      else if (s.ports.findIndex((q) => q.port === p.port) !== i)
        e[`ports.${i}.port`] = "Port is already listed.";
    });
    if (
      s.ports.some((p) => p.tls) &&
      s.protocol !== "udp" &&
      s.certificate_ids.length === 0 &&
      s.protocol === "http"
    )
      e.certificate_ids = "Choose a certificate for the TLS ports.";
  }
  if (step === 4) {
    if (s.pool_choice === "new") {
      if (!NAME_RE.test(s.new_pool_name))
        e.new_pool_name = "Use lower case letters, digits and hyphens.";
      const valid = s.members.filter((m) => m.address);
      if (valid.length === 0) e.members = "Add at least one member.";
      s.members.forEach((m, i) => {
        if (m.address && !IPV4.test(m.address) && !m.address.includes(":"))
          e[`members.${i}.address`] = "Enter an IP address.";
        if (s.protocol === "udp" && m.address && !m.port)
          e[`members.${i}.port`] = "UDP members need a port.";
        if (m.port && (Number(m.port) < 1 || Number(m.port) > 65535))
          e[`members.${i}.port`] = "Port must be 1–65535.";
      });
    } else if (!s.pool_choice) e.pool_choice = "Choose a pool.";
    if (s.protocol === "http") {
      s.routes.forEach((r, i) => {
        if (!r.host && !r.path) e[`routes.${i}`] = "A route needs a host or a path.";
        if (r.path && !r.path.startsWith("/")) e[`routes.${i}.path`] = "A path starts with /.";
        if (r.action === "redirect" && !r.redirect_location)
          e[`routes.${i}.redirect`] = "Enter where to redirect.";
      });
    }
  }
  if (step === 5 && s.pool_choice === "new" && s.monitor_choice === "new") {
    if (!NAME_RE.test(s.monitor.name))
      e.monitor_name = "Use lower case letters, digits and hyphens.";
    if (s.monitor.timeout_s > s.monitor.interval_s)
      e.monitor_timeout = "Timeout cannot exceed the interval.";
    if (
      s.monitor.type === "udp" &&
      s.monitor.payload_hex &&
      !/^([0-9a-f]{2})*$/.test(s.monitor.payload_hex)
    )
      e.monitor_payload = "Lower-case hex, an even number of digits.";
  }
  return e;
}

/** Time a failing member takes to be marked down, for the monitor hint. */
export function downAfterSeconds(m: Pick<MonitorDraft, "interval_s" | "fall">): number {
  return m.interval_s * m.fall;
}

export interface BuiltOps {
  ops: OpIn[];
  vsId: string;
  poolId: string | null;
  monitorId: string | null;
}

/** The ops, created in dependency order: monitor, pool, then the service. */
export function buildOps(
  s: WizardState,
  newId: () => string = () => crypto.randomUUID(),
): BuiltOps {
  const ops: OpIn[] = [];
  let monitorId: string | null =
    s.monitor_choice && s.monitor_choice !== "new" ? s.monitor_choice : null;
  let poolId: string | null = s.pool_choice !== "new" ? s.pool_choice : null;

  if (s.pool_choice === "new") {
    if (s.monitor_choice === "new") {
      monitorId = newId();
      const m = s.monitor;
      ops.push({
        action: "create",
        object_type: "health_monitor",
        object_id: monitorId,
        body: {
          name: m.name,
          type: m.type,
          interval_s: m.interval_s,
          timeout_s: m.timeout_s,
          fall: m.fall,
          ...(m.type === "udp" ? {} : { rise: m.rise }),
          ...(m.type === "http"
            ? {
                http: {
                  method: "GET",
                  path: m.path,
                  expect_status: m.expect_status
                    .split(",")
                    .map((x) => Number(x.trim()))
                    .filter(Boolean),
                },
              }
            : {}),
          ...(m.type === "udp"
            ? { udp: { payload_hex: m.payload_hex || null, require_reply: m.require_reply } }
            : {}),
        },
      });
    }
    poolId = newId();
    ops.push({
      action: "create",
      object_type: "pool",
      object_id: poolId,
      body: {
        name: s.new_pool_name,
        algorithm: s.algorithm,
        health_monitor_id: monitorId,
        members: s.members
          .filter((m) => m.address)
          .map((m, i) => ({
            id: newId(),
            name: `${s.new_pool_name}-${i + 1}`,
            address: m.address,
            port: m.port ? Number(m.port) : null,
            weight: m.weight,
          })),
      },
    });
  }

  const vsId = newId();
  const udp = s.protocol === "udp";
  ops.push({
    action: "create",
    object_type: "virtual_service",
    object_id: vsId,
    body: {
      name: s.name,
      protocol: s.protocol,
      se_group_id: s.se_group_id,
      vip: s.vip,
      ...(s.vipMode === "pool" ? { vip_pool_id: s.vip_pool_id } : {}),
      ports: s.ports,
      certificate_ids: udp ? [] : s.certificate_ids,
      default_pool_id: poolId,
      routes:
        s.protocol === "http"
          ? s.routes.map((r, i) => ({
              order: i + 1,
              match: {
                host: r.host || null,
                path_prefix: r.kind === "prefix" && r.path ? r.path : null,
                path_exact: r.kind === "exact" && r.path ? r.path : null,
              },
              pool_id: r.action === "pool" ? poolId : null,
              redirect:
                r.action === "redirect"
                  ? { location: r.redirect_location, code: r.redirect_code }
                  : null,
            }))
          : [],
    },
  });
  return { ops, vsId, poolId, monitorId };
}
