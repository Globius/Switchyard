"""The login gate application (ADR 0195, 0196): an ASGI reverse proxy in front of Dex.

For each ``radius`` directory the broker runs a Dex instance with an ``authproxy`` connector: Dex
trusts the headers of whoever sits in front of its ``/callback``. That is this gate. It serves a
sign-in page, checks the person against RADIUS (one or more steps, ADR 0196), and only then
forwards ``/callback`` to Dex with ``X-Remote-User``, ``X-Remote-User-Email`` and
``X-Remote-Group`` set by the gate. The same headers are removed from every request that comes from
outside, so no client can name itself.

Everything else (discovery, keys, token endpoint) is passed to Dex unchanged. The gate keeps no
state: the sign-in in progress and the finished sign-in are signed cookies, so any number of gate
processes can serve one directory. The only memory is the failure counter that slows guessing.

Example:
    >>> from switchyard.gate.app import Throttle
    >>> t = Throttle(limit=2, window_s=60)
    >>> t.failure("alice", "10.0.0.1", now=0); t.failure("alice", "10.0.0.1", now=1)
    >>> t.locked("alice", "10.0.0.1", now=2)
    True
"""

import html
import json
import logging
import re
import secrets
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from switchyard.adapters.radius import RadiusClient, RadiusServer, UdpRadiusClient
from switchyard.gate import flow as fl

log = logging.getLogger("switchyard.gate")

SESSION_COOKIE = "sy_gate"
FLOW_COOKIE = "sy_flow"
CSRF_COOKIE = "sy_csrf"
REMOTE_HEADERS = ("x-remote-user", "x-remote-user-email", "x-remote-group")
_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-encoding",
    "host",
}
_SAFE_TARGET = re.compile(r"/[A-Za-z0-9._~%!$&'()*+,;=:@/?-]*")
_SAFE_PATH = _SAFE_TARGET  # a step-up may come back to a path with a query
_HOST = re.compile(r"[A-Za-z0-9.-]{1,253}")

ClientFactory = Callable[[fl.GateConfig, str], RadiusClient]


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _ints(value: object) -> list[int]:
    return [v for v in value if isinstance(v, int)] if isinstance(value, list) else []


def _strs(value: object) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


class Throttle:
    """Counts failures per (user, address) and per address; both lock for a while.

    ``limit`` failures inside ``window_s`` lock the pair (and ``limit * 6`` the address) until the
    window passes. A success does not clear the count, so a slow guesser gains nothing by
    interleaving a known good login.
    """

    def __init__(self, limit: int = 5, window_s: int = 300) -> None:
        """Set the number of failures allowed and the length of the window."""
        self.limit = limit
        self.window_s = window_s
        self._seen: dict[tuple[str, str], list[float]] = {}

    def _recent(self, key: tuple[str, str], now: float) -> list[float]:
        hits = [t for t in self._seen.get(key, []) if now - t < self.window_s]
        if hits:
            self._seen[key] = hits
        else:
            self._seen.pop(key, None)
        return hits

    def failure(self, user: str, ip: str, now: float | None = None) -> None:
        """Record one failed attempt."""
        t = time.time() if now is None else now
        for key in ((user.lower(), ip), ("", ip)):
            self._seen[key] = [*self._recent(key, t), t]
        if len(self._seen) > 10_000:  # bound memory under a flood of distinct names
            self._seen.clear()

    def locked(self, user: str, ip: str, now: float | None = None) -> bool:
        """Whether this person from this address must wait."""
        t = time.time() if now is None else now
        return (
            len(self._recent((user.lower(), ip), t)) >= self.limit
            or len(self._recent(("", ip), t)) >= self.limit * 6
        )


class ConfigSet:
    """The gate configurations in a directory, re-read when the files change."""

    def __init__(self, directory: Path, recheck_s: float = 5.0) -> None:
        """Watch ``directory`` (``*.json`` files written by ``syctl broker render``)."""
        self.directory = directory
        self.recheck_s = recheck_s
        self._sig: tuple[tuple[str, int, int], ...] | None = None
        self._checked = 0.0
        self._configs: list[fl.GateConfig] = []

    def all(self) -> list[fl.GateConfig]:
        """The current configurations (a file that does not parse is skipped and logged)."""
        now = time.monotonic()
        if self._sig is not None and now - self._checked < self.recheck_s:
            return self._configs
        self._checked = now
        files = sorted(self.directory.glob("*.json")) if self.directory.is_dir() else []
        sig = tuple((f.name, f.stat().st_mtime_ns, f.stat().st_size) for f in files)
        if sig != self._sig:
            configs = []
            for f in files:
                try:
                    configs.append(fl.GateConfig.from_mapping(json.loads(f.read_text("utf-8"))))
                except (ValueError, OSError):
                    log.error("gate: cannot use %s", f.name)
            self._configs, self._sig = configs, sig
        return self._configs

    def find(self, path: str) -> tuple[fl.GateConfig, str] | None:
        """The configuration whose path prefix covers ``path`` and the rest of the path."""
        for cfg in self.all():
            if path == cfg.path or path.startswith(cfg.path + "/"):
                return cfg, path[len(cfg.path) :] or "/"
        return None


def default_client(cfg: fl.GateConfig, server: str) -> RadiusClient:
    """A UDP RADIUS client for the named server of ``cfg``."""
    s = cfg.servers[server]
    return UdpRadiusClient(
        RadiusServer(
            s.host,
            s.port,
            s.secret.encode(),
            cfg.nas_identifier,
            s.timeout_s,
            s.retries,
            cfg.group_attribute,
        )
    )


def _page(
    cfg: fl.GateConfig,
    step: fl.Step | None,
    first: bool,
    message: str,
    csrf: str,
    ret: str = "",
) -> str:
    esc = html.escape
    action = f"{cfg.path}/_gate/login" + (f"?return={quote(ret, safe='')}" if first and ret else "")
    if step is None:
        step = cfg.steps[0]
    fields = ""
    if first:
        fields += (
            '<label>Username<input name="username" autocomplete="username" required '
            'maxlength="128" autofocus></label>'
        )
    kind = "text" if step.kind == "code" else "password"
    extra = (
        ' autocomplete="one-time-code" inputmode="numeric"'
        if step.kind == "code"
        else (' autocomplete="current-password"')
    )
    fields += (
        f'<label>{esc(step.prompt)}<input name="secret" type="{kind}" required '
        f'maxlength="128"{extra}{"" if first else " autofocus"}></label>'
    )
    note = f'<p class="msg">{esc(message)}</p>' if message else ""
    return (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        "<title>Sign in</title><style>"
        "body{font:16px system-ui,sans-serif;margin:0;display:grid;place-items:center;"
        "min-height:100vh;background:#f4f5f7;color:#1a1c20}"
        "form{background:#fff;padding:2rem;border-radius:8px;width:min(22rem,90vw);"
        "box-shadow:0 1px 4px #0003}"
        "label{display:block;margin:0 0 1rem;font-weight:600}"
        "input{display:block;width:100%;box-sizing:border-box;margin-top:.3rem;padding:.55rem;"
        "font:inherit}button{padding:.6rem 1.2rem;font:inherit}.msg{color:#a4262c}"
        "@media(prefers-color-scheme:dark){body{background:#16181c;color:#eee}"
        "form{background:#22252b}.msg{color:#ff8a8a}}</style>"
        f'<form method=post action="{esc(action)}"><h1>Sign in</h1>{note}{fields}'
        f'<input type=hidden name=csrf value="{esc(csrf)}"><button>Continue</button></form>'
    )


class Gate:
    """The request handlers; build the ASGI app with :meth:`app`."""

    def __init__(
        self,
        configs: ConfigSet,
        *,
        client_factory: ClientFactory = default_client,
        http: httpx.AsyncClient | None = None,
        throttle: Throttle | None = None,
    ) -> None:
        """Wire the configuration, the RADIUS client factory and the HTTP client for Dex."""
        self.configs = configs
        self.client_factory = client_factory
        self.http = http or httpx.AsyncClient(timeout=15, follow_redirects=False)
        self.throttle = throttle or Throttle()

    def app(self) -> Starlette:
        """The ASGI application."""
        methods = ["GET", "POST", "HEAD", "PUT", "DELETE", "OPTIONS", "PATCH"]
        return Starlette(routes=[Route("/{rest:path}", self.handle, methods=methods)])

    # ------------------------------------------------------------------------------ helpers

    @staticmethod
    def client_ip(request: Request) -> str:
        """The client address from the trusted proxy's ``X-Forwarded-For`` (it overwrites it)."""
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        return forwarded or (request.client.host if request.client else "")

    @staticmethod
    def _secure(response: Response) -> Response:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def _cookie(
        self, response: Response, cfg: fl.GateConfig, name: str, value: str, age: int
    ) -> None:
        response.set_cookie(
            name,
            value,
            max_age=age,
            path=cfg.path + "/",
            secure=True,
            httponly=True,
            samesite="lax",
        )

    def _session(self, cfg: fl.GateConfig, request: Request) -> dict[str, object] | None:
        token = request.cookies.get(SESSION_COOKIE)
        return fl.verify(cfg.signing_secret, "session", token) if token else None

    def _flow(self, cfg: fl.GateConfig, request: Request) -> dict[str, object] | None:
        token = request.cookies.get(FLOW_COOKIE)
        return fl.verify(cfg.signing_secret, "flow", token) if token else None

    def _login_redirect(self, cfg: fl.GateConfig, request: Request) -> Response:
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"{cfg.path}/_gate/login?return={quote(target, safe='')}", 302)

    @staticmethod
    def _return_target(cfg: fl.GateConfig, value: str | None) -> str | None:
        """``value`` if it is a local path under the directory's own prefix, else ``None``."""
        if value and value.startswith(cfg.path + "/") and _SAFE_TARGET.fullmatch(value):
            return None if "//" in value.split("?")[0] else value
        return None

    # ------------------------------------------------------------------------------- routes

    async def handle(self, request: Request) -> Response:
        """Route a request to the sign-in page, the step-up entry, the callback or Dex."""
        found = self.configs.find(request.url.path)
        if found is None:
            return PlainTextResponse("not found", 404)
        cfg, sub = found
        if sub == "/_gate/login":
            return await self._login(cfg, request)
        if sub == "/_gate/elevate":
            return await self._elevate(cfg, request)
        if sub.startswith("/_gate/"):
            return PlainTextResponse("not found", 404)
        extra: dict[str, str] = {}
        if sub == "/callback":
            session = self._session(cfg, request)
            if session is None:
                return self._login_redirect(cfg, request)
            user = str(session["u"])
            groups = [
                *fl.clean_groups(_strs(session.get("g"))),
                *fl.factor_groups(len(_ints(session.get("d")))),
            ]
            extra = {
                "X-Remote-User": user,
                "X-Remote-User-Email": fl.email_of(cfg, user),
                "X-Remote-Group": ",".join(groups),
            }
        return await self._proxy(cfg, request, extra)

    async def _proxy(self, cfg: fl.GateConfig, request: Request, extra: dict[str, str]) -> Response:
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in _HOP and k.lower() not in REMOTE_HEADERS
        }
        headers.update(extra)
        headers["host"] = request.headers.get("host", "")
        url = f"http://127.0.0.1:{cfg.dex_port}{request.url.path}"
        try:
            upstream = await self.http.request(
                request.method,
                url,
                params=request.url.query or None,
                headers=headers,
                content=await request.body(),
            )
        except httpx.HTTPError:
            return PlainTextResponse("the login service is not available", 502)
        out = Response(upstream.content, upstream.status_code)
        for key, value in upstream.headers.multi_items():
            if key.lower() not in _HOP:
                out.headers.append(key, value)
        return out

    def _form(
        self,
        cfg: fl.GateConfig,
        request: Request,
        step_index: int,
        first: bool,
        message: str,
        status: int = 200,
    ) -> Response:
        csrf = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(24)
        ret = request.query_params.get("return", "")
        body = _page(cfg, cfg.steps[step_index], first, message, csrf, ret)
        response = self._secure(HTMLResponse(body, status))
        self._cookie(response, cfg, CSRF_COOKIE, csrf, cfg.login_timeout_s)
        return response

    async def _login(self, cfg: fl.GateConfig, request: Request) -> Response:
        # a request that carries ``return`` starts a new sign-in and drops one left unfinished
        flow = None if "return" in request.query_params else self._flow(cfg, request)
        pending = _int(flow.get("p")) if flow else 0
        if pending >= len(cfg.steps):
            flow, pending = None, 0
        if request.method == "GET":
            return self._form(cfg, request, pending, flow is None, "")
        if request.method != "POST":
            return PlainTextResponse("method not allowed", 405)
        raw = await request.body()
        form = {k: v[0] for k, v in parse_qs(raw[:8192].decode("utf-8", "replace")).items()}
        csrf_cookie = request.cookies.get(CSRF_COOKIE, "")
        csrf_form = str(form.get("csrf", ""))
        if not csrf_cookie or not secrets.compare_digest(csrf_cookie, csrf_form):
            return self._secure(PlainTextResponse("the form expired, reload the page", 400))
        ip = self.client_ip(request)
        secret = str(form.get("secret", ""))
        user = str(flow["u"]) if flow else str(form.get("username", "")).strip()
        if not user or not fl.valid_user(user) or not secret or len(secret) > 128:
            self.throttle.failure(user, ip)
            return self._form(cfg, request, pending, flow is None, "Sign-in failed.", 401)
        if self.throttle.locked(user, ip):
            return self._form(
                cfg, request, pending, flow is None, "Too many attempts, wait a few minutes.", 429
            )
        step = cfg.steps[pending]
        client = self.client_factory(cfg, step.server)
        state = b""
        rounds = 0
        if flow:
            state = bytes.fromhex(str(flow.get("s", "")))
            rounds = _int(flow.get("n"))
        outcome = await fl.verify_step(client, user, secret, state)
        if outcome.status == "error":
            log.warning("gate %s: RADIUS server %s did not answer", cfg.name, step.server)
            return self._form(
                cfg, request, pending, flow is None, "The sign-in service is unavailable.", 503
            )
        if outcome.status == "reject":
            self.throttle.failure(user, ip)
            log.info("gate %s: step %s rejected for %s", cfg.name, step.name, user)
            return self._form(cfg, request, pending, flow is None, "Sign-in failed.", 401)
        base: dict[str, object] = {
            "u": user,
            "g": _strs(flow.get("g")) if flow else [],
            "d": _ints(flow.get("d")) if flow else [],
            "w": _int(flow.get("w")) if flow else 0,
            "r": str(flow.get("r", ""))
            if flow
            else (self._return_target(cfg, request.query_params.get("return")) or ""),
        }
        if outcome.status == "challenge":
            if rounds + 1 >= fl.MAX_CHALLENGE_ROUNDS:
                self.throttle.failure(user, ip)
                return self._form(cfg, request, pending, flow is None, "Sign-in failed.", 401)
            token = fl.sign(
                cfg.signing_secret,
                "flow",
                {**base, "p": pending, "s": outcome.state.hex(), "n": rounds + 1},
                cfg.login_timeout_s,
            )
            response = self._secure(
                HTMLResponse(
                    _page(
                        cfg,
                        step,
                        False,
                        outcome.message or step.prompt,
                        request.cookies.get(CSRF_COOKIE, ""),
                    )
                )
            )
            self._cookie(response, cfg, FLOW_COOKIE, token, cfg.login_timeout_s)
            return response
        # accepted
        groups = sorted({*_strs(base["g"]), *outcome.groups})
        done = [*_ints(base["d"]), pending]
        nxt = fl.next_step(cfg, done, groups, ip, _int(base["w"]))
        if nxt is not None:
            token = fl.sign(
                cfg.signing_secret,
                "flow",
                {**base, "g": groups, "d": done, "p": nxt, "s": "", "n": 0},
                cfg.login_timeout_s,
            )
            response = self._form(cfg, request, nxt, False, "")
            self._cookie(response, cfg, FLOW_COOKIE, token, cfg.login_timeout_s)
            return response
        session = fl.sign(
            cfg.signing_secret, "session", {"u": user, "g": groups, "d": done}, fl.GATE_SESSION_S
        )
        # ``r`` is a local path checked when the flow began, or the service address the gate
        # itself built from a validated host (step-up); never a value the client chose freely
        target = str(base["r"]) or f"{cfg.path}/"
        response = RedirectResponse(target, 303)
        self._cookie(response, cfg, SESSION_COOKIE, session, fl.GATE_SESSION_S)
        response.delete_cookie(FLOW_COOKIE, path=cfg.path + "/")
        return self._secure(response)

    async def _elevate(self, cfg: fl.GateConfig, request: Request) -> Response:
        """Step-up entry: ask for the missing factors, then send the person back to the service.

        ``level`` is the number of factors wanted; ``host`` and ``path`` say where to return:
        ``https://<host><auth prefix>/start?rd=<path>``. The host must be one the directory's
        access sets serve, so the gate cannot be used as an open redirect.
        """
        params = request.query_params
        host = params.get("host", "").lower()
        path = params.get("path", "/")
        try:
            level = int(params.get("level", ""))
        except ValueError:
            return PlainTextResponse("bad request", 400)
        prefixes = cfg.hosts.get(host)
        if (
            not 2 <= level <= len(cfg.steps)
            or not prefixes
            or not _HOST.fullmatch(host)
            or not _SAFE_PATH.fullmatch(path)
            or path.startswith("//")
        ):
            return PlainTextResponse("bad request", 400)
        back = f"https://{host}{prefixes[0]}/start?rd={quote(path, safe='')}"
        session = self._session(cfg, request)
        if session is None:
            return self._login_redirect(cfg, request)
        done = _ints(session.get("d"))
        if len(done) >= level:
            return self._secure(RedirectResponse(back, 303))
        groups = _strs(session.get("g"))
        nxt = fl.next_step(cfg, done, groups, self.client_ip(request), level)
        if nxt is None:
            return PlainTextResponse("bad request", 400)
        token = fl.sign(
            cfg.signing_secret,
            "flow",
            {
                "u": session["u"],
                "g": groups,
                "d": done,
                "p": nxt,
                "s": "",
                "n": 0,
                "w": level,
                "r": back,
            },
            cfg.login_timeout_s,
        )
        response = self._secure(RedirectResponse(f"{cfg.path}/_gate/login", 303))
        self._cookie(response, cfg, FLOW_COOKIE, token, cfg.login_timeout_s)
        return response


def build_app(directory: Path, *, client_factory: ClientFactory = default_client) -> Starlette:
    """The ASGI app serving every ``*.json`` gate configuration in ``directory``."""
    return Gate(ConfigSet(directory), client_factory=client_factory).app()


def hosts_of(mapping: Mapping[str, tuple[str, ...]]) -> list[str]:
    """Host names in ``mapping`` that look like URLs (used by the renderer's tests)."""
    return [h for h in mapping if urlsplit(f"https://{h}").hostname == h]
