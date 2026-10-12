-- Managed by Switchyard (ADR 0172). Do not edit: changes are reported as drift.
local SERVICE = "app"
local VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
local FIELD = "cf-turnstile-response"
local VALID = 1800
local SECRET_PATH = "/etc/switchyard/certs/captcha-app.secret"
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

core.register_action("sy_cap_check_3027bc6b", { "http-req" }, function(txn)
  if not SECRET then return end
  local cookie = txn.sf:req_cook("sy_cap")
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

core.register_service("sy_cap_3027bc6b", "http", function(applet)
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
  local cookie = "sy_cap=" .. ts .. "." .. sign(applet, ts, ip)
    .. "; Path=/; Max-Age=" .. VALID .. "; HttpOnly; SameSite=Lax"
  if applet.sf:ssl_fc() then cookie = cookie .. "; Secure" end
  return reply(applet, 302, "", { location = target, ["set-cookie"] = cookie })
end)
