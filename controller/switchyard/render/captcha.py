"""The CAPTCHA gate of a protection set: a Lua service that checks the provider's answer (ADR 0172).

Per protected service the renderer writes two files under ``haproxy/lists/`` (the Data Plane API
keeps general files there): a fixed Lua script
(only validated constants are filled in) and the page that shows the provider's widget. The
provider secret never appears in either; it travels in the agent bundle as
``certs/captcha-<service>.secret`` and the script reads it once at start.

How a request flows:

1. ``sy_cap_check`` looks at the ``sy_cap`` cookie, ``<epoch>.<hmac>``, where the HMAC-SHA256 key is
   derived from the provider secret and the signed text is ``<epoch>|<client address>|<service>``.
   A valid, fresh cookie sets ``txn.sy_pass``.
2. A request without it gets the page (``GET``/``HEAD``) or 403 (other methods).
3. The page posts the widget's token to ``/.sy/captcha``; the Lua service asks the provider to
   verify it (HTTPS, certificate checked) and, if it passes, sets the cookie and redirects back to a
   local path.
"""

import hashlib

from switchyard.domain import models as dm
from switchyard.render import escape as esc

#: Provider endpoints and the form field that carries the widget's token.
PROVIDERS = {
    "turnstile": (
        "https://challenges.cloudflare.com/turnstile/v0/siteverify",
        "cf-turnstile-response",
        '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"'
        " async defer></script>\n"
        '<div class="cf-turnstile" data-sitekey="@SITEKEY@"></div>',
    ),
    "hcaptcha": (
        "https://api.hcaptcha.com/siteverify",
        "h-captcha-response",
        '<script src="https://js.hcaptcha.com/1/api.js" async defer></script>\n'
        '<div class="h-captcha" data-sitekey="@SITEKEY@"></div>',
    ),
}
SUBMIT_PATH = "/.sy/captcha"
COOKIE = "sy_cap"
SECRET_DIR = "/etc/switchyard/certs"  # noqa: S105 - a directory, not a password

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Verify you are human</title></head>
<body style="font-family:sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem">
<h1>Verify you are human</h1>
<form method="post" action="@SUBMIT@">
<input type="hidden" name="next" id="next" value="/">
@WIDGET@
<p><button type="submit">Continue</button></p>
</form>
<script>document.getElementById("next").value = location.pathname + location.search;</script>
</body></html>
"""

_LUA = r"""-- Managed by Switchyard (ADR 0172). Do not edit: changes are reported as drift.
local SERVICE = "@SERVICE@"
local VERIFY_URL = "@VERIFY_URL@"
local FIELD = "@FIELD@"
local VALID = @VALID@
local SECRET_PATH = "@SECRET_PATH@"
local SECRET = nil

core.register_init(function()
  local f = io.open(SECRET_PATH, "r")
  if f then
    SECRET = f:read("*l")
    f:close()
  end
end)

local function xor_pad(key, byte)
  local out = {}
  for i = 1, 64 do
    out[i] = string.char((key:byte(i) or 0) ~ byte)
  end
  return table.concat(out)
end

local function hmac_sha256(txn, key, msg)
  local inner = txn.c:sha2(xor_pad(key, 0x36) .. msg, 256)
  return txn.c:sha2(xor_pad(key, 0x5c) .. inner, 256)
end

local function sign(txn, ts, ip)
  local key = txn.c:sha2("sy-captcha|" .. SECRET, 256)
  return string.lower(txn.c:hex(hmac_sha256(txn, key, ts .. "|" .. ip .. "|" .. SERVICE)))
end

local function urlencode(s)
  return (s:gsub("[^%w%-_%.~]", function(c) return string.format("%%%02X", c:byte()) end))
end

local function urldecode(s)
  s = s:gsub("+", " ")
  return (s:gsub("%%(%x%x)", function(h) return string.char(tonumber(h, 16)) end))
end

core.register_action("sy_cap_check_@ID@", { "http-req" }, function(txn)
  if not SECRET then return end
  local cookie = txn.sf:req_cook("@COOKIE@")
  if not cookie then return end
  local ts, sig = cookie:match("^(%d+)%.(%x+)$")
  if not ts then return end
  local age = core.now().sec - tonumber(ts)
  if age < 0 or age > VALID then return end
  if sign(txn, ts, txn.sf:src()) == sig:lower() then
    txn:set_var("txn.sy_pass", true)
  end
end)

local function reply(applet, status, text, headers)
  applet:set_status(status)
  for name, value in pairs(headers or {}) do applet:add_header(name, value) end
  applet:add_header("cache-control", "no-store")
  applet:add_header("content-type", "text/plain")
  applet:add_header("content-length", tostring(#text))
  applet:start_response()
  applet:send(text)
end

core.register_service("sy_cap_@ID@", "http", function(applet)
  local length = applet.length or 0
  if length > 8192 then return reply(applet, 413, "request too large") end
  local data = length > 0 and applet:receive(length) or ""
  local form = {}
  for k, v in data:gmatch("([^&=]+)=([^&]*)") do form[urldecode(k)] = urldecode(v) end
  local token = form[FIELD]
  if not SECRET or not token or token == "" or #token > 4096 then
    return reply(applet, 400, "verification failed")
  end
  local ip = applet.sf:src()
  local response = core.httpclient():post({
    url = VERIFY_URL,
    body = "secret=" .. urlencode(SECRET) .. "&response=" .. urlencode(token)
      .. "&remoteip=" .. urlencode(ip),
    headers = { ["content-type"] = { "application/x-www-form-urlencoded" } },
    timeout = 5000,
  })
  if response.status ~= 200 or not response.body:find('"success"%s*:%s*true') then
    core.Warning("captcha " .. SERVICE .. ": provider answered " .. tostring(response.status)
      .. " " .. tostring(response.body):sub(1, 200))
    return reply(applet, 403, "verification failed")
  end
  local ts = tostring(core.now().sec)
  local target = form["next"] or "/"
  if not target:match("^/") or target:match("^//") or target:match("^/\\") or target:find("%c") then
    target = "/"
  end
  local cookie = "@COOKIE@=" .. ts .. "." .. sign(applet, ts, ip)
    .. "; Path=/; Max-Age=" .. VALID .. "; HttpOnly; SameSite=Lax"
  if applet.sf:ssl_fc() then cookie = cookie .. "; Secure" end
  return reply(applet, 302, "", { location = target, ["set-cookie"] = cookie })
end)
"""


def lua_id(svc: dm.VirtualService) -> str:
    """A short identifier for the Lua registrations of one service (names may hold ``-``)."""
    return hashlib.sha256(str(svc.id).encode()).hexdigest()[:8]


def secret_path(svc: dm.VirtualService) -> str:
    """Where the provider secret of a service lives on an engine."""
    return f"{SECRET_DIR}/captcha-{esc.ident(svc.name)}.secret"


def bundle_path(svc: dm.VirtualService) -> str:
    """The same file as a path relative to ``/etc/switchyard/`` (agent bundle)."""
    return f"certs/captcha-{esc.ident(svc.name)}.secret"


def render_lua(svc: dm.VirtualService, gate: dm.CaptchaGate) -> str:
    """The Lua script of one service."""
    url, field, _ = PROVIDERS[gate.provider]
    return (
        _LUA.replace("@SERVICE@", esc.ident(svc.name))
        .replace("@VERIFY_URL@", url)
        .replace("@FIELD@", field)
        .replace("@VALID@", str(int(gate.valid_for_s)))
        .replace("@SECRET_PATH@", secret_path(svc))
        .replace("@COOKIE@", COOKIE)
        .replace("@ID@", lua_id(svc))
    )


def render_page(gate: dm.CaptchaGate) -> str:
    """The page with the provider's widget."""
    _, _, widget = PROVIDERS[gate.provider]
    site_key = esc.site_key(gate.site_key)
    return _PAGE.replace("@SUBMIT@", SUBMIT_PATH).replace(
        "@WIDGET@", widget.replace("@SITEKEY@", site_key)
    )
