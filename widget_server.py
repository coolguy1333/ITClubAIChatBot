"""Widget bridge: WebSocket broadcaster + small HTTP server for the live
display and the /admin control UI.

Everything binds to 127.0.0.1 only by default, so the bot token in
config.json is never exposed to the network unless widget.bindHost is
changed deliberately. /admin lets you edit config.json (channels, modes,
prompts, model) from a browser instead of hand-editing the file; secrets
(bot token, Claude key) are never sent back to the browser, only whether
they're set.
"""

import asyncio
import copy
import json
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: F401

import websockets

from hwstats import get_hw_stats
from state import BASE, CONFIG_PATH, load_config

_SECRET_PATHS = (("discord", "botToken"), ("ai", "claudeApiKey"))


def _restart_service(delay=1.0):
    """Best-effort: ask systemd to restart the 'steve' service a moment
    after this response goes out, so a new bot token actually takes effect
    without anyone having to SSH in and run systemctl by hand. No-ops
    quietly if this isn't running under systemd (e.g. on Windows/dev)."""
    if not shutil.which("systemctl"):
        return False

    def worker():
        time.sleep(delay)
        try:
            subprocess.run(["systemctl", "restart", "steve"], check=False, timeout=15)
        except Exception as e:
            print(f"[admin] auto-restart failed: {e}")
    threading.Thread(target=worker, daemon=True).start()
    return True


def _redact_secrets(cfg):
    safe = copy.deepcopy(cfg)
    secrets_set = {}
    for section, key in _SECRET_PATHS:
        val = safe.get(section, {}).get(key, "")
        secrets_set[f"{section}.{key}"] = bool(val.strip())
        if key in safe.get(section, {}):
            safe[section][key] = ""
    safe["secretsSet"] = secrets_set
    return safe


def _deep_merge(base, incoming):
    for k, v in incoming.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


class Broadcaster:
    def __init__(self):
        self.clients = set()

    async def handler(self, ws):
        self.clients.add(ws)
        try:
            async for _ in ws:      # we don't expect messages; keep alive
                pass
        except Exception:
            pass
        finally:
            self.clients.discard(ws)

    async def send(self, obj):
        if not self.clients:
            return
        msg = json.dumps(obj, ensure_ascii=False)
        dead = []
        for ws in self.clients:
            try:
                await ws.send(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def serve(self, port):
        host = load_config().get("widget", {}).get("bindHost", "127.0.0.1")
        async with websockets.serve(self.handler, host, port):
            print(f"[widget] WebSocket on ws://{host}:{port}")
            await asyncio.Future()


class ControlContext:
    """Refs the HTTP thread needs to control the running bot (set by run.py)."""

    def __init__(self, state, brain, broadcaster, loop):
        self.state = state
        self.brain = brain
        self.broadcaster = broadcaster
        self.loop = loop
        self.voice_status = lambda: None    # run.py swaps in a real callable


class _WidgetHandler(BaseHTTPRequestHandler):
    ctx: ControlContext = None

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path in ("/", "/widget", "/widget.html"):
            try:
                body = (BASE / "widget.html").read_bytes()
            except OSError:
                self.send_error(404)
                return
            self._ok(body, "text/html; charset=utf-8")
        elif self.path in ("/admin", "/admin/"):
            try:
                body = (BASE / "admin.html").read_bytes()
            except OSError:
                self.send_error(404)
                return
            self._ok(body, "text/html; charset=utf-8")
        elif self.path == "/widget-config":
            cfg = load_config()
            safe = {
                "widget": cfg.get("widget", {}),
            }
            self._ok(json.dumps(safe).encode(), "application/json")
        elif self.path == "/admin/config":
            self._ok(json.dumps(_redact_secrets(load_config())).encode(), "application/json")
        elif self.path == "/admin/hardware":
            self._ok(json.dumps(get_hw_stats()).encode(), "application/json")
        elif self.path.startswith("/admin/log"):
            from urllib.parse import urlparse, parse_qs
            n = int(parse_qs(urlparse(self.path).query).get("n", ["50"])[0])
            lines = []
            path = BASE / "chat_history.jsonl"
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    lines = f.readlines()[-max(1, min(n, 500)):]
            entries = []
            for line in lines:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
            self._ok(json.dumps(entries).encode(), "application/json")
        elif self.path == "/status":
            ctx = self.ctx
            cfg = load_config()
            ai_cfg = cfg.get("ai", {})
            provider = ai_cfg.get("provider", "ollama")
            self._ok(json.dumps({
                "mode": ctx.state.mode,
                "profile": ctx.state.profile_name,
                "provider": provider,
                "model": ai_cfg.get("claudeModel") if provider == "claude" else ai_cfg.get("model"),
                "voice": ctx.voice_status(),
            }).encode(), "application/json")
        else:
            self.send_error(404)

    def do_POST(self):
        ctx = self.ctx
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self.send_error(400)
            return

        if self.path == "/control":
            mode = data.get("mode", "")
            if mode not in ("casual", "meeting"):
                self.send_error(400)
                return
            ctx.state.mode = mode
            self._ok(json.dumps({"ok": True, "profile": ctx.state.profile_name}).encode(),
                     "application/json")
        elif self.path == "/say":
            text = (data.get("text") or "").strip()
            if not text:
                self.send_error(400)
                return
            asyncio.run_coroutine_threadsafe(
                ctx.broadcaster.send({"type": "steve", "source": "say",
                                      "user": "", "question": "", "text": text}),
                ctx.loop)
            self._ok(b'{"ok": true}', "application/json")
        elif self.path == "/chat":
            text = (data.get("text") or "").strip()
            if not text:
                self.send_error(400)
                return
            admin_name = load_config().get("discord", {}).get("adminName", "an officer")
            fut = asyncio.run_coroutine_threadsafe(
                ctx.brain.ask("gui", f"[{admin_name} says via the control panel]: {text}",
                              "gui_chat", admin_name),
                ctx.loop)
            try:
                reply = fut.result(timeout=180)
            except Exception as e:
                reply = None
                print(f"[gui] chat error: {e}")
            self._ok(json.dumps({"reply": reply}).encode(), "application/json")
        elif self.path == "/admin/config":
            # incoming body must never contain secret fields (client strips
            # them) - merge over the live config.json, preserving whatever
            # secrets are already there
            for section, key in _SECRET_PATHS:
                data.get(section, {}).pop(key, None)
            cfg = load_config()
            _deep_merge(cfg, data)
            _save_config(cfg)
            if "profiles" in data and "mode" in data.get("profiles", {}):
                ctx.state.mode = data["profiles"]["mode"]
            self._ok(b'{"ok": true}', "application/json")
        elif self.path == "/admin/secrets":
            cfg = load_config()
            token = (data.get("discordBotToken") or "").strip()
            key = (data.get("claudeApiKey") or "").strip()
            if token:
                cfg.setdefault("discord", {})["botToken"] = token
            if key:
                cfg.setdefault("ai", {})["claudeApiKey"] = key
            _save_config(cfg)
            note = "saved"
            if token:
                if _restart_service():
                    note = "saved - restarting Steve now to pick up the new token"
                else:
                    note = "saved - restart Steve manually (not running under systemd) for the new token to take effect"
            self._ok(json.dumps({"ok": True, "note": note}).encode(), "application/json")
        else:
            self.send_error(404)

    def _ok(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def start_http_server(port, ctx=None):
    _WidgetHandler.ctx = ctx
    host = load_config().get("widget", {}).get("bindHost", "127.0.0.1")
    server = ThreadingHTTPServer((host, port), _WidgetHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[widget] overlay at http://{host}:{port}/widget (OBS Browser Source)")
    return server
