#!/usr/bin/env python3
"""
V4Z AI Route - a small local AI gateway for Termux.

It runs a local OpenAI-compatible HTTP endpoint (POST /v1/chat/completions)
and forwards each request to one of YOUR OWN configured providers. If a
provider fails (timeout, connection error, HTTP error), the next provider
in the chain is tried automatically.

Original code by V4Z RASHD. Channel: https://t.me/rashdteem
Python standard library only - no pip installs.

Honest scope:
  - This is a routing/fallback layer, not a source of free AI. You bring
    your own provider base URLs and API keys (config file, see below).
  - Keys are read only from ~/.v4zroute/config.json on this device and are
    never printed (listings show them masked).
"""

import argparse
import html
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.1"
UPDATE_URL = ("https://raw.githubusercontent.com/v4zrashd/"
              "RASHDAIRouteTermuxx/main/v4zroute.py")
APP_NAME = "V4Z AI Route"
CHANNEL = "https://t.me/rashdteem"
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".v4zroute")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
MAX_BODY = 20 * 1024 * 1024

GREEN = "\033[92m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
RED = "\033[91m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

# The gateway always connects to upstreams directly. Proxy environment
# variables of the host shell are ignored on purpose: a local gateway
# should talk to its providers straight, not through some ambient proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def c(text, color):
    return f"{color}{text}{RESET}"


def banner():
    print(c("=" * 56, GREEN))
    print(c(f"  {APP_NAME}  v{VERSION}", GREEN + BOLD))
    print(c(f"  by V4Z RASHD  |  {CHANNEL}", DIM))
    print(c("=" * 56, GREEN))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TEMPLATE_CONFIG = {
    "host": DEFAULT_HOST,
    "port": DEFAULT_PORT,
    "timeout": 60,
    "gateway_key": "",
    "providers": [
        {
            "name": "example",
            "base_url": "https://api.example.com/v1",
            "api_key": "YOUR_API_KEY_HERE",
            "models": ["model-name-here"],
            "enabled": True,
        }
    ],
    "aliases": {},
    "combos": {},
}


def ensure_config():
    """Create a template config on first run. Returns True if it existed."""
    if os.path.exists(CONFIG_FILE):
        return True
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
        json.dump(TEMPLATE_CONFIG, fh, indent=2)
        fh.write("\n")
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass
    return False


def load_config():
    if not ensure_config():
        print(c(f"Created a template config at {CONFIG_FILE}", YELLOW))
        print("Edit it: put YOUR provider base URLs and API keys there, "
              "then run again.")
        return None
    try:
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(c(f"Config file is broken: {exc}", RED))
        print(f"Fix or delete {CONFIG_FILE} and run again.")
        return None
    return cfg


def is_placeholder(provider):
    url = (provider.get("base_url") or "").lower()
    key = provider.get("api_key") or ""
    return "example.com" in url or key in ("", "YOUR_API_KEY_HERE")


def configured_providers(cfg):
    """Enabled, non-placeholder providers, in config order."""
    out = []
    for p in cfg.get("providers") or []:
        if not p.get("enabled", True):
            continue
        if not p.get("name") or not p.get("base_url"):
            continue
        if is_placeholder(p):
            continue
        out.append(p)
    return out


def mask_key(key):
    if not key:
        return "(none)"
    if len(key) <= 7:
        return "***"
    return f"{key[:3]}***{key[-2:]}"


def provider_default_model(provider):
    if provider.get("model"):
        return provider["model"]
    models = provider.get("models") or []
    return models[0] if models else "auto"


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def configured_combos(cfg, providers):
    """Valid combos from config: {name: ["provider/model", ...]} with at
    least one entry pointing at a configured provider. Secrets-free: the
    entries are only provider/model references."""
    by_name = {p["name"]: p for p in providers}
    out = {}
    raw = cfg.get("combos") or {}
    if not isinstance(raw, dict):
        return out
    for name, entries in raw.items():
        ok_entries = []
        for entry in entries or []:
            if not isinstance(entry, str):
                continue
            entry = entry.strip()
            if not entry:
                continue
            if "/" in entry:
                if entry.split("/", 1)[0] in by_name:
                    ok_entries.append(entry)
            elif entry in by_name:
                ok_entries.append(entry)
            elif any(entry in (p.get("models") or []) for p in providers):
                ok_entries.append(entry)
        if ok_entries:
            out[name] = ok_entries
    return out


def resolve_chain(cfg, providers, model):
    """Ordered [(provider, model_to_send), ...] for a request model name.

    Rules (first match wins):
      - alias from config "aliases" ("name/model" or {provider, model})
      - "providername/model" for a configured provider name
      - a plain model name that one provider lists in its "models"
      - "auto" (or unknown): every provider with its own default model
    The first entry is the primary; the rest are fallback entries that use
    their own default model.
    """
    by_name = {p["name"]: p for p in providers}
    chain = []

    def add(provider, model_name):
        if provider and all(e[0]["name"] != provider["name"] for e in chain):
            chain.append((provider, model_name))

    def add_rest(except_name=None):
        for p in providers:
            if p["name"] != except_name:
                add(p, provider_default_model(p))

    model = (model or "auto").strip()
    aliases = cfg.get("aliases") or {}

    if model in aliases:
        target = aliases[model]
        if isinstance(target, str) and "/" in target:
            name, m = target.split("/", 1)
            if name in by_name:
                add(by_name[name], m)
                add_rest(name)
                return chain
        if isinstance(target, dict):
            name, m = target.get("provider"), target.get("model")
            if name in by_name and m:
                add(by_name[name], m)
                add_rest(name)
                return chain

    combos = cfg.get("combos") or {}
    if isinstance(combos, dict) and model in combos:
        for entry in combos.get(model) or []:
            if not isinstance(entry, str):
                continue
            entry = entry.strip()
            if "/" in entry:
                name, m = entry.split("/", 1)
                if name in by_name:
                    add(by_name[name], m)
            elif entry in by_name:
                add(by_name[entry], provider_default_model(by_name[entry]))
            else:
                for p in providers:
                    if entry in (p.get("models") or []):
                        add(p, entry)
                        break
        if chain:
            add_rest()
            return chain

    if "/" in model:
        name, m = model.split("/", 1)
        if name in by_name:
            add(by_name[name], m)
            add_rest(name)
            return chain

    if model != "auto":
        for p in providers:
            if model in (p.get("models") or []):
                add(p, model)
                add_rest(p["name"])
                return chain

    for p in providers:
        add(p, provider_default_model(p))
    return chain


# ---------------------------------------------------------------------------
# Upstream calls
# ---------------------------------------------------------------------------

class UpstreamError(Exception):
    def __init__(self, provider_name, message, status=None):
        super().__init__(message)
        self.provider_name = provider_name
        self.status = status


def open_upstream(provider, model, payload, timeout):
    """POST /chat/completions to one provider. Returns the response object
    (caller reads/relays it). Raises UpstreamError on any failure."""
    name = provider["name"]
    url = provider["base_url"].rstrip("/") + "/chat/completions"
    body = dict(payload)
    body["model"] = model
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", f"v4zroute/{VERSION}")
    key = provider.get("api_key") or ""
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        return _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise UpstreamError(name, f"HTTP {exc.code} {detail}".strip(),
                            status=exc.code) from exc
    except Exception as exc:  # noqa: BLE001 - timeout / DNS / refused etc.
        raise UpstreamError(name, str(exc)[:200]) from exc


# ---------------------------------------------------------------------------
# HTTP gateway
# ---------------------------------------------------------------------------

class GatewayHandler(BaseHTTPRequestHandler):
    cfg = None           # set by run_server()
    providers = []       # set by run_server()
    timeout = 60
    server_version = f"v4zroute/{VERSION}"
    protocol_version = "HTTP/1.1"

    # -- helpers -----------------------------------------------------------
    def log_message(self, fmt, *args):  # keep the console readable
        sys.stderr.write("[gw] %s\n" % (fmt % args))

    def _send_json(self, status, obj):
        data = json.dumps(obj).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _authorized(self):
        want = (self.cfg or {}).get("gateway_key") or ""
        if not want:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {want}"

    def _send_html(self, status, text):
        data = text.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _dashboard(self):
        esc = html.escape
        prov_cards = []
        for p in self.providers:
            models = ", ".join(p.get("models") or []) or "-"
            prov_cards.append(
                "<div class='card'>"
                f"<div class='pname'>{esc(str(p.get('name', '?')))}</div>"
                f"<div class='row'><span>base_url</span>"
                f"<code>{esc(str(p.get('base_url', '')))}</code></div>"
                f"<div class='row'><span>api_key</span>"
                f"<code>{esc(mask_key(p.get('api_key') or ''))}</code></div>"
                f"<div class='row'><span>models</span>"
                f"<code>{esc(models)}</code></div></div>")
        combos = configured_combos(self.cfg, self.providers)
        combo_rows = "".join(
            f"<div class='row'><span>{esc(name)}</span>"
            f"<code>{esc(' -> '.join(entries))}</code></div>"
            for name, entries in combos.items()) or \
            "<div class='dim'>none configured</div>"
        aliases = self.cfg.get("aliases") or {}
        alias_rows = "".join(
            f"<div class='row'><span>{esc(str(k))}</span>"
            f"<code>{esc(str(v))}</code></div>"
            for k, v in aliases.items()) or \
            "<div class='dim'>none configured</div>"
        model_ids = ["auto"] + list(combos) + list(aliases)
        for p in self.providers:
            for m in p.get("models") or []:
                model_ids.append(m)
                model_ids.append(f"{p['name']}/{m}")
        chips = "".join(f"<code class='chip'>{esc(mid)}</code>"
                        for mid in model_ids) or \
            "<span class='dim'>none</span>"
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(APP_NAME)} - dashboard</title>
<style>
body{{background:#0b0f14;color:#d6e2ee;font-family:monospace;margin:0;
padding:16px}}
h1{{color:#00ff9d;font-size:20px;margin:0 0 4px}}
h2{{color:#00ff9d;font-size:14px;margin:20px 0 8px}}
.dim{{color:#5b6b7c}}
.card{{background:#10161f;border:1px solid #1d2a3a;border-radius:8px;
padding:10px 12px;margin:0 0 10px}}
.pname{{font-weight:bold;font-size:15px;margin-bottom:6px}}
.row{{display:flex;gap:10px;padding:2px 0}}
.row span{{min-width:90px;color:#5b6b7c}}
code{{color:#a8ffd8;word-break:break-all}}
.chip{{display:inline-block;background:#10161f;border:1px solid #1d2a3a;
border-radius:6px;padding:2px 8px;margin:0 4px 6px 0}}
#dot{{color:#00ff9d}}
a{{color:#00ff9d}}
</style>
</head>
<body>
<h1>{esc(APP_NAME)} <span class="dim">v{esc(VERSION)}</span></h1>
<div><span id="dot">&#9679;</span> <span id="status">status: ok</span>
<span class="dim">| last checked <span id="seen">-</span></span></div>
<div class="dim">Endpoint: POST /v1/chat/completions on this host.
Providers are tried in the order shown; failures fall back to the next.</div>
<h2>Providers ({len(self.providers)})</h2>
{''.join(prov_cards) or "<div class='dim'>no providers configured</div>"}
<h2>Combos</h2>
<div class="card">{combo_rows}</div>
<h2>Aliases</h2>
<div class="card">{alias_rows}</div>
<h2>Model names you can request</h2>
<div>{chips}</div>
<p class="dim">Keys are masked on this page. Full keys live only in
~/.v4zroute/config.json on this device. By V4Z RASHD -
<a href="{esc(CHANNEL)}">channel</a></p>
<script>
async function tick(){{
  try{{
    const r = await fetch('/health');
    const j = await r.json();
    document.getElementById('status').textContent =
      'status: ' + j.status + ' (' + j.providers.length + ' providers)';
    document.getElementById('dot').style.color = '#00ff9d';
  }}catch(e){{
    document.getElementById('status').textContent = 'status: unreachable';
    document.getElementById('dot').style.color = '#ff5555';
  }}
  document.getElementById('seen').textContent =
    new Date().toLocaleTimeString();
}}
tick(); setInterval(tick, 5000);
</script>
</body>
</html>"""

    # -- GET ----------------------------------------------------------------
    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {
                "status": "ok",
                "app": APP_NAME,
                "version": VERSION,
                "providers": [p["name"] for p in self.providers],
            })
            return
        if self.path == "/":
            if not self._authorized():
                self._send_json(401, {"error": {"message": "bad gateway key",
                                                "type": "auth_error"}})
                return
            self._send_html(200, self._dashboard())
            return
        if self.path in ("/v1/models", "/models"):
            if not self._authorized():
                self._send_json(401, {"error": {"message": "bad gateway key",
                                                "type": "auth_error"}})
                return
            data = [{"id": "auto", "object": "model", "owned_by": "v4zroute"}]
            for alias in (self.cfg.get("aliases") or {}):
                data.append({"id": alias, "object": "model",
                             "owned_by": "v4zroute-alias"})
            for combo in configured_combos(self.cfg, self.providers):
                data.append({"id": combo, "object": "model",
                             "owned_by": "v4zroute-combo"})
            for p in self.providers:
                for m in p.get("models") or []:
                    data.append({"id": m, "object": "model",
                                 "owned_by": p["name"]})
                    data.append({"id": f"{p['name']}/{m}", "object": "model",
                                 "owned_by": p["name"]})
            self._send_json(200, {"object": "list", "data": data})
            return
        self._send_json(404, {"error": {"message": "not found",
                                        "type": "not_found"}})

    # -- POST ---------------------------------------------------------------
    def do_POST(self):
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            self._send_json(404, {"error": {"message": "not found",
                                            "type": "not_found"}})
            return
        if not self._authorized():
            self._send_json(401, {"error": {"message": "bad gateway key",
                                            "type": "auth_error"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._send_json(400, {"error": {"message": "bad request body size",
                                            "type": "bad_request"}})
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": {"message": "body must be JSON",
                                            "type": "bad_request"}})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": {"message": "body must be a JSON "
                                            "object", "type": "bad_request"}})
            return

        model = payload.get("model") or "auto"
        chain = resolve_chain(self.cfg, self.providers, model)
        if not chain:
            self._send_json(503, {"error": {
                "message": "no providers configured - edit " + CONFIG_FILE,
                "type": "no_providers"}})
            return

        stream = bool(payload.get("stream"))
        errors = []
        for provider, send_model in chain:
            started = time.time()
            try:
                resp = open_upstream(provider, send_model, payload,
                                      self.timeout)
            except UpstreamError as exc:
                ms = int((time.time() - started) * 1000)
                errors.append(f"{exc.provider_name}: {exc}")
                print(c(f"[route] {exc.provider_name} failed ({ms} ms): "
                        f"{exc} -> trying next", YELLOW), file=sys.stderr)
                continue
            ms = int((time.time() - started) * 1000)
            print(c(f"[route] {provider['name']} model={send_model} "
                    f"status={resp.status} ({ms} ms)", GREEN),
                  file=sys.stderr)
            if stream:
                self._relay_stream(resp)
            else:
                self._relay_body(resp)
            return

        self._send_json(502, {"error": {
            "message": "all providers failed: " + " | ".join(errors),
            "type": "all_providers_failed"}})

    def _relay_body(self, resp):
        try:
            data = resp.read()
        except Exception:  # noqa: BLE001
            data = b""
        try:
            self.send_response(resp.status)
            self.send_header("Content-Type",
                             resp.headers.get("Content-Type",
                                              "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _relay_stream(self, resp):
        try:
            self.send_response(resp.status)
            self.send_header("Content-Type",
                             resp.headers.get("Content-Type",
                                              "text/event-stream"))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return
        try:
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def run_server(cfg, providers, host, port, timeout):
    GatewayHandler.cfg = cfg
    GatewayHandler.providers = providers
    GatewayHandler.timeout = timeout
    server = ThreadingHTTPServer((host, port), GatewayHandler)
    server.daemon_threads = True
    print(c(f"Listening on http://{host}:{port}", CYAN + BOLD))
    print(f"Endpoint: POST http://{host}:{port}/v1/chat/completions")
    print(f"Providers (in order): "
          f"{', '.join(p['name'] for p in providers) or '(none)'}")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(c("\nStopped.", YELLOW))
    finally:
        server.server_close()
    return 0


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_serve(args):
    cfg = load_config()
    if cfg is None:
        return 1
    providers = configured_providers(cfg)
    banner()
    if not providers:
        print(c("No usable providers in the config yet.", RED))
        print(f"Edit {CONFIG_FILE}: replace the example entry with your "
              "own provider base URL, API key and model names.")
        print("Then run:  v4zroute test   (to check them)")
        return 1
    host = args.host or cfg.get("host") or DEFAULT_HOST
    port = int(args.port or cfg.get("port") or DEFAULT_PORT)
    timeout = int(cfg.get("timeout") or 60)
    skipped = len(cfg.get("providers") or []) - len(providers)
    if skipped:
        print(c(f"({skipped} provider entr(y/ies) skipped: disabled or "
                "still placeholder)", DIM))
    return run_server(cfg, providers, host, port, timeout)


def cmd_providers(args):
    cfg = load_config()
    if cfg is None:
        return 1
    banner()
    all_entries = cfg.get("providers") or []
    usable = configured_providers(cfg)
    print(f"Config: {CONFIG_FILE}")
    print(f"Gateway: http://{cfg.get('host') or DEFAULT_HOST}:"
          f"{cfg.get('port') or DEFAULT_PORT}  |  "
          f"gateway key: {'set' if cfg.get('gateway_key') else 'not set'}")
    print(c("\nProviders:", CYAN + BOLD))
    if not all_entries:
        print("  (none - edit the config)")
    for p in all_entries:
        state = "ready" if p in usable else "skipped (disabled/placeholder)"
        print(f"  - {p.get('name')}  [{state}]")
        print(f"      base_url : {p.get('base_url')}")
        print(f"      api_key  : {mask_key(p.get('api_key') or '')}")
        print(f"      models   : {', '.join(p.get('models') or []) or '-'}")
    aliases = cfg.get("aliases") or {}
    if aliases:
        print(c("\nAliases:", CYAN + BOLD))
        for name, target in aliases.items():
            print(f"  {name} -> {target}")
    combos = configured_combos(cfg, usable)
    if combos:
        print(c("\nCombos (ordered fallback chains):", CYAN + BOLD))
        for name, entries in combos.items():
            print(f"  {name} -> {' -> '.join(entries)}")
    print(c("\nModel names you can request:", CYAN + BOLD))
    print("  auto                 first working provider (with fallback)")
    print("  <combo name>         ordered chain from config \"combos\"")
    print("  <provider>/<model>   a specific provider")
    print("  <model>              whichever provider lists that model")
    return 0


def cmd_test(args):
    cfg = load_config()
    if cfg is None:
        return 1
    banner()
    providers = configured_providers(cfg)
    if not providers:
        print(c("No usable providers yet - edit the config first:", RED))
        print(f"  {CONFIG_FILE}")
        return 1
    timeout = int(cfg.get("timeout") or 60)
    ping_timeout = min(timeout, 15)
    print(f"Checking {len(providers)} provider(s) via GET /models ...")
    ok_count = 0
    for p in providers:
        url = p["base_url"].rstrip("/") + "/models"
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", f"v4zroute/{VERSION}")
        if p.get("api_key"):
            req.add_header("Authorization", f"Bearer {p['api_key']}")
        started = time.time()
        try:
            resp = _OPENER.open(req, timeout=ping_timeout)
            ms = int((time.time() - started) * 1000)
            print(c(f"  OK   {p['name']:<16} HTTP {resp.status} "
                    f"({ms} ms)  {p['base_url']}", GREEN))
            ok_count += 1
        except urllib.error.HTTPError as exc:
            # Any HTTP answer means the endpoint exists; auth-type answers
            # still prove reachability, so report them plainly.
            print(c(f"  FAIL {p['name']:<16} HTTP {exc.code}  "
                    f"{p['base_url']}", RED))
        except Exception as exc:  # noqa: BLE001
            print(c(f"  FAIL {p['name']:<16} {str(exc)[:80]}  "
                    f"{p['base_url']}", RED))
    print(f"\nReachable: {ok_count}/{len(providers)}")
    if ok_count:
        print("Start the gateway with:  v4zroute serve")
    return 0 if ok_count else 1


def _find_update_target():
    prefix = os.environ.get("PREFIX") or ""
    if prefix:
        cand = os.path.join(prefix, "bin", "v4zroute")
        if os.path.exists(cand):
            return cand
    found = shutil.which("v4zroute")
    if found:
        return found
    if prefix:
        return os.path.join(prefix, "bin", "v4zroute")
    return os.path.join(CONFIG_DIR, "v4zroute.py")


def cmd_update(args):
    banner()
    target = _find_update_target()
    print(f"Update source : {UPDATE_URL}")
    print(f"Target        : {target}")
    if args.dry_run:
        print(c("Dry run: would download the file above and replace the "
                "target with it. Nothing was downloaded or written.",
                YELLOW))
        return 0
    print("Downloading...")
    try:
        req = urllib.request.Request(
            UPDATE_URL, headers={"User-Agent": f"v4zroute/{VERSION}"})
        with _OPENER.open(req, timeout=30) as resp:
            data = resp.read()
    except Exception as exc:  # noqa: BLE001 - report any network failure
        print(c(f"Download failed: {exc}", RED))
        print("Check your internet connection, then try again.")
        return 1
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        print(c("Downloaded file is not valid UTF-8 - aborting, "
                "nothing was replaced.", RED))
        return 1
    if APP_NAME not in text or "def main" not in text:
        print(c("Downloaded file does not look like v4zroute.py - "
                "aborting, nothing was replaced.", RED))
        return 1
    new_version = "?"
    for line in text.splitlines():
        if line.startswith('VERSION = "'):
            new_version = line.split('"')[1]
            break
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp_path = target + ".tmp"
        with open(tmp_path, "wb") as fh:
            fh.write(data)
        os.chmod(tmp_path, 0o755)
        os.replace(tmp_path, target)
    except OSError as exc:
        print(c(f"Could not write {target}: {exc}", RED))
        return 1
    print(c(f"Updated {target} -> v{new_version} ({len(data)} bytes).",
            GREEN))
    if os.path.basename(os.path.dirname(target)) != "bin":
        print(c("Note: the file was written outside a bin folder, so the "
                "`v4zroute` command may not pick it up. Run install.sh "
                "from the package to install it properly.", YELLOW))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="v4zroute",
        description=f"{APP_NAME} - local AI gateway with provider "
                    "fallback (OpenAI-compatible endpoint).")
    parser.add_argument("--version", action="version",
                        version=f"v4zroute {VERSION}")
    sub = parser.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="run the gateway server")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)

    sub.add_parser("providers", help="show configured providers (keys masked)")
    sub.add_parser("test", help="check config and ping each provider")
    p_update = sub.add_parser("update",
                              help="download the latest v4zroute from GitHub")
    p_update.add_argument("--dry-run", action="store_true",
                          help="only report what would happen")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "serve":
        return cmd_serve(args)
    if args.cmd == "providers":
        return cmd_providers(args)
    if args.cmd == "test":
        return cmd_test(args)
    if args.cmd == "update":
        return cmd_update(args)
    # No subcommand: show the shortest useful help.
    banner()
    print("Usage:")
    print("  v4zroute serve       run the gateway (POST /v1/chat/completions)")
    print("  v4zroute providers   list configured providers (keys masked)")
    print("  v4zroute test        check config + ping providers")
    print("  v4zroute update      download the latest version from GitHub")
    print(f"\nConfig file: {CONFIG_FILE}")
    print("Point any OpenAI-compatible app at "
          f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/v1 with model \"auto\".")
    return 0


if __name__ == "__main__":
    sys.exit(main())
