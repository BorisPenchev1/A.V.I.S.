"""AVIS Mirror server.

Runs on the Mac and lets an iPhone (or any browser on the same Wi-Fi) act as a
thin mirror: all computation and tools run here on the Mac; the phone just views
and drives the conversation.

    python -m server.mirror

Endpoints (all /api/* require the shared token):
    GET  /                     mobile PWA shell
    GET  /manifest.webmanifest, /sw.js
    POST /api/chat             {session, prompt} -> SSE token stream
    GET  /api/events           SSE live activity feed (automations, notifications)
    POST /api/notify           {title, body, ...} append activity + push to ntfy
    GET  /api/health           {ok, name}

Configuration lives in mirror_config.json at the project root and is shared with
the macOS app (token, port, ntfy topic).
"""

from __future__ import annotations

import json
import queue
import re
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from main.model import ConversationMemory, run_assistant


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "mirror_config.json"
ACTIVITY_PATH = PROJECT_ROOT / "mirror_activity.jsonl"

DEFAULT_CONFIG = {
    "port": 8765,
    "token": "",
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    try:
        config.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    if not config.get("token"):
        config["token"] = secrets.token_urlsafe(12)
    save_config(config)
    return config


def save_config(config: dict[str, Any]) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def lan_ip() -> str:
    """Best-effort local network IP for building the phone URL."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Activity feed (shared between this server and the macOS app via a JSONL file)
# ---------------------------------------------------------------------------

class ActivityHub:
    """Fan-out of activity events to every connected phone, plus ntfy push."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.subscribers: set[queue.Queue] = set()
        self.lock = threading.Lock()
        self._stop = False
        threading.Thread(target=self._tail_file, daemon=True).start()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self.lock:
            self.subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            self.subscribers.discard(q)

    def broadcast(self, event: dict[str, Any]) -> None:
        with self.lock:
            targets = list(self.subscribers)
        for q in targets:
            q.put(event)

    def publish(self, event: dict[str, Any], push: bool = False) -> None:
        event.setdefault("time", time.time())
        try:
            with ACTIVITY_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event) + "\n")
        except OSError:
            pass
        # _tail_file will pick it up and broadcast; push here if requested.
        if push:
            self.push_ntfy(event.get("title", "AVIS"), event.get("body", ""))

    def push_ntfy(self, title: str, body: str) -> None:
        topic = (self.config.get("ntfy_topic") or "").strip()
        if not topic:
            return
        server = (self.config.get("ntfy_server") or "https://ntfy.sh").rstrip("/")
        try:
            request = Request(
                f"{server}/{topic}",
                data=(body or " ").encode("utf-8"),
                headers={"Title": title, "Tags": "robot"},
                method="POST",
            )
            urlopen(request, timeout=8).read()
        except OSError:
            pass

    def _tail_file(self) -> None:
        """Follow the activity JSONL file and broadcast new lines."""
        ACTIVITY_PATH.touch(exist_ok=True)
        with ACTIVITY_PATH.open("r", encoding="utf-8") as handle:
            handle.seek(0, 2)  # skip existing content; only stream new events
            while not self._stop:
                line = handle.readline()
                if not line:
                    time.sleep(0.5)
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    self.broadcast(json.loads(line))
                except json.JSONDecodeError:
                    continue


# ---------------------------------------------------------------------------
# Per-session conversation state
# ---------------------------------------------------------------------------

_BLANKET = {"", "everything", "anything", "do anything", "all", "whatever you want", "do whatever you want"}
_GRANT_PATTERNS = [
    re.compile(r"you have permission(?: to)?(.*)", re.IGNORECASE),
    re.compile(r"i (?:give|grant) you permission(?: to)?(.*)", re.IGNORECASE),
]
_TOOL_KEYWORDS = {
    "open_app": ("open app", "apps", "application", "launch"),
    "close_app": ("close app", "close", "quit"),
    "search_files": ("search file", "find file", "search"),
    "open_file": ("open file", "read file", "file"),
    "list_notifications": ("notification",),
    "connect_bluetooth_device": ("connect", "bluetooth"),
    "disconnect_bluetooth_device": ("disconnect", "bluetooth"),
    "media_play_pause": ("media", "music", "play", "pause"),
    "lock_device": ("lock",),
    "set_volume": ("volume",),
    "make_reminder": ("reminder",),
    "add_event_in_calendar": ("calendar", "event"),
    "run_mac_diagnostics": ("diagnostic", "health check"),
    "remember_long_term": ("remember", "memory", "note"),
}


class Session:
    def __init__(self) -> None:
        self.memory = ConversationMemory()
        self.grants: list[str] = []
        self.grant_all = False

    def note_grant(self, text: str) -> None:
        lowered = text.casefold()
        for pattern in _GRANT_PATTERNS:
            match = pattern.search(lowered)
            if not match:
                continue
            phrase = match.group(1).strip(" .!,:;")
            if phrase in _BLANKET:
                self.grant_all = True
            else:
                self.grants.append(phrase)
            return

    def permission(self):
        phrases = " ; ".join(self.grants)
        blanket = self.grant_all

        def request_permission(name: str, arguments: dict[str, Any]) -> bool:
            if blanket:
                return True
            if not phrases:
                return False
            if name.replace("_", " ") in phrases:
                return True
            if any(keyword in phrases for keyword in _TOOL_KEYWORDS.get(name, ())):
                return True
            for value in arguments.values():
                if isinstance(value, str) and value.strip() and value.strip().casefold() in phrases:
                    return True
            return False

        return request_permission


SESSIONS: dict[str, Session] = {}
SESSIONS_LOCK = threading.Lock()


def get_session(session_id: str) -> Session:
    with SESSIONS_LOCK:
        session = SESSIONS.get(session_id)
        if session is None:
            session = Session()
            SESSIONS[session_id] = session
        return session


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

CONFIG = load_config()
HUB = ActivityHub(CONFIG)


class MirrorHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_: Any) -> None:  # keep the console quiet
        pass

    # -- helpers ---------------------------------------------------------
    def _authorized(self, params: dict[str, list[str]]) -> bool:
        token = CONFIG.get("token", "")
        header = self.headers.get("X-Avis-Token")
        query = params.get("token", [None])[0]
        return bool(token) and (header == token or query == token)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _begin_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _sse(self, payload: dict[str, Any]) -> None:
        self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode("utf-8"))
        self.wfile.flush()

    # -- routing ---------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        route = parsed.path

        if route == "/" or route == "/index.html":
            self._send(200, PWA_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif route == "/manifest.webmanifest":
            self._send(200, MANIFEST.encode("utf-8"), "application/manifest+json")
        elif route == "/sw.js":
            self._send(200, SERVICE_WORKER.encode("utf-8"), "text/javascript")
        elif route == "/api/health":
            self._send_json(200, {"ok": True, "name": "AVIS Mirror"})
        elif route == "/api/events":
            if not self._authorized(params):
                self._send_json(401, {"error": "unauthorized"})
                return
            self._stream_events()
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        route = parsed.path

        if not self._authorized(params):
            self._send_json(401, {"error": "unauthorized"})
            return

        if route == "/api/chat":
            self._stream_chat()
        elif route == "/api/notify":
            payload = self._read_json()
            event = {
                "kind": payload.get("kind", "notify"),
                "title": payload.get("title", "AVIS"),
                "body": payload.get("body", ""),
            }
            HUB.publish(event, push=bool(payload.get("push", True)))
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"error": "not found"})

    # -- SSE endpoints ---------------------------------------------------
    def _stream_chat(self) -> None:
        payload = self._read_json()
        prompt = str(payload.get("prompt", "")).strip()
        session_id = str(payload.get("session", "default"))
        if not prompt:
            self._send_json(400, {"error": "empty prompt"})
            return

        session = get_session(session_id)
        session.note_grant(prompt)

        self._begin_sse()
        try:
            def on_token(token: str) -> None:
                self._sse({"t": "tok", "x": token})

            response = run_assistant(prompt, session.permission(), session.memory, on_token)
            self._sse({"t": "done", "x": response})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:  # surface backend errors to the phone
            try:
                self._sse({"t": "err", "x": str(error)})
            except OSError:
                pass

    def _stream_events(self) -> None:
        q = HUB.subscribe()
        self._begin_sse()
        try:
            self._sse({"kind": "hello", "title": "Connected", "body": "Live activity from your Mac."})
            while True:
                try:
                    event = q.get(timeout=15)
                    self._sse(event)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")  # comment frame
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            HUB.unsubscribe(q)


# ---------------------------------------------------------------------------
# Front-end (PWA) served inline
# ---------------------------------------------------------------------------

MANIFEST = json.dumps({
    "name": "AVIS Mirror",
    "short_name": "AVIS",
    "start_url": ".",
    "display": "standalone",
    "background_color": "#0b1020",
    "theme_color": "#0b1020",
    "icons": [],
})

SERVICE_WORKER = "self.addEventListener('fetch', () => {});"

PWA_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0b1020">
<link rel="manifest" href="/manifest.webmanifest">
<title>AVIS Mirror</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { margin: 0; font: 16px/1.45 -apple-system, system-ui, sans-serif; background: #0b1020; color: #e5e7eb;
         display: flex; flex-direction: column; height: 100dvh; }
  header { padding: env(safe-area-inset-top) 16px 10px; padding-top: max(env(safe-area-inset-top), 12px);
           display: flex; align-items: center; gap: 10px; border-bottom: 1px solid #1f2937; background: #0d1428; }
  header .dot { width: 9px; height: 9px; border-radius: 50%; background: #ef4444; }
  header .dot.on { background: #22c55e; }
  header b { font-size: 18px; letter-spacing: .04em; }
  header .sp { flex: 1; }
  .tabs { display: flex; gap: 6px; padding: 8px 12px; background: #0d1428; border-bottom: 1px solid #1f2937; }
  .tabs button { flex: 1; padding: 8px; border: 0; border-radius: 10px; background: #131c33; color: #cbd5e1; font-weight: 600; }
  .tabs button.active { background: #2563eb; color: #fff; }
  main { flex: 1; overflow-y: auto; padding: 14px; -webkit-overflow-scrolling: touch; }
  .msg { margin: 8px 0; display: flex; }
  .msg .bubble { max-width: 82%; padding: 10px 13px; border-radius: 16px; white-space: pre-wrap; word-wrap: break-word; }
  .msg.user { justify-content: flex-end; }
  .msg.user .bubble { background: #2563eb; color: #fff; border-bottom-right-radius: 5px; }
  .msg.assistant .bubble { background: #131c33; border: 1px solid #1f2937; border-bottom-left-radius: 5px; }
  .event { margin: 8px 0; padding: 10px 13px; background: #10182b; border: 1px solid #1f2937; border-radius: 12px; }
  .event .t { font-weight: 700; font-size: 14px; }
  .event .b { color: #9fb0c9; font-size: 14px; white-space: pre-wrap; }
  .event .ts { color: #64748b; font-size: 12px; margin-top: 4px; }
  footer { padding: 10px 12px calc(env(safe-area-inset-bottom) + 10px); border-top: 1px solid #1f2937; background: #0d1428;
           display: flex; gap: 8px; }
  textarea { flex: 1; resize: none; background: #131c33; border: 1px solid #26324d; color: #e5e7eb; border-radius: 14px;
             padding: 10px 13px; font: inherit; max-height: 120px; }
  footer button { border: 0; border-radius: 14px; background: #2563eb; color: #fff; font-weight: 700; padding: 0 16px; font-size: 18px; }
  footer button:disabled { opacity: .5; }
  .setup { padding: 24px; }
  .setup input { width: 100%; padding: 12px; border-radius: 12px; border: 1px solid #26324d; background: #131c33; color: #fff; font: inherit; margin: 10px 0; }
  .setup button { width: 100%; padding: 13px; border: 0; border-radius: 12px; background: #2563eb; color: #fff; font-weight: 700; }
  .hide { display: none !important; }
  .hint { color: #64748b; font-size: 13px; }
</style>
</head>
<body>
<div id="setup" class="setup hide">
  <h2>Connect to your Mac</h2>
  <p class="hint">Enter the access token shown in the AVIS app's Mirror page. It is saved on this device.</p>
  <input id="tokenInput" placeholder="Access token" autocapitalize="off" autocorrect="off">
  <button onclick="saveToken()">Connect</button>
</div>

<div id="app" class="hide" style="display:flex;flex-direction:column;height:100%">
  <header>
    <span class="dot" id="status"></span><b>AVIS</b>
    <span class="sp"></span>
    <span class="hint" id="statusText">connecting…</span>
  </header>
  <div class="tabs">
    <button id="tabChat" class="active" onclick="showTab('chat')">Chat</button>
    <button id="tabActivity" onclick="showTab('activity')">Activity</button>
  </div>
  <main id="chatView"></main>
  <main id="activityView" class="hide"></main>
  <footer id="composer">
    <textarea id="input" rows="1" placeholder="Ask AVIS…" oninput="autosize(this)"></textarea>
    <button id="send" onclick="send()">↑</button>
  </footer>
</div>

<script>
const TOKEN_KEY = "avis.token";
const SESSION_KEY = "avis.session";
let token = localStorage.getItem(TOKEN_KEY) || new URLSearchParams(location.search).get("token") || "";
let session = localStorage.getItem(SESSION_KEY);
if (!session) { session = Math.random().toString(36).slice(2) + Date.now().toString(36); localStorage.setItem(SESSION_KEY, session); }

function boot() {
  if (!token) { document.getElementById("setup").classList.remove("hide"); return; }
  localStorage.setItem(TOKEN_KEY, token);
  document.getElementById("app").classList.remove("hide");
  connectEvents();
}
function saveToken() {
  token = document.getElementById("tokenInput").value.trim();
  if (!token) return;
  localStorage.setItem(TOKEN_KEY, token);
  document.getElementById("setup").classList.add("hide");
  boot();
}
function showTab(which) {
  document.getElementById("chatView").classList.toggle("hide", which !== "chat");
  document.getElementById("activityView").classList.toggle("hide", which !== "activity");
  document.getElementById("composer").classList.toggle("hide", which !== "chat");
  document.getElementById("tabChat").classList.toggle("active", which === "chat");
  document.getElementById("tabActivity").classList.toggle("active", which === "activity");
}
function autosize(el) { el.style.height = "auto"; el.style.height = Math.min(el.scrollHeight, 120) + "px"; }

function addMsg(role, text) {
  const view = document.getElementById("chatView");
  const wrap = document.createElement("div"); wrap.className = "msg " + role;
  const bubble = document.createElement("div"); bubble.className = "bubble"; bubble.textContent = text;
  wrap.appendChild(bubble); view.appendChild(wrap); view.scrollTop = view.scrollHeight;
  return bubble;
}
function setStatus(on, text) {
  document.getElementById("status").classList.toggle("on", on);
  document.getElementById("statusText").textContent = text;
}

async function send() {
  const input = document.getElementById("input");
  const text = input.value.trim();
  if (!text) return;
  input.value = ""; autosize(input);
  addMsg("user", text);
  const bubble = addMsg("assistant", "…");
  document.getElementById("send").disabled = true;
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Avis-Token": token },
      body: JSON.stringify({ prompt: text, session })
    });
    if (res.status === 401) { bubble.textContent = "Unauthorized — check the token."; return; }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "", acc = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, idx); buffer = buffer.slice(idx + 2);
        const line = frame.split("\n").find(l => l.startsWith("data: "));
        if (!line) continue;
        const evt = JSON.parse(line.slice(6));
        if (evt.t === "tok") { if (acc === "") bubble.textContent = ""; acc += evt.x; bubble.textContent = acc; }
        else if (evt.t === "final" || (evt.t === "done" && acc === "")) { bubble.textContent = evt.x || acc; }
        else if (evt.t === "err") { bubble.textContent = "Error: " + evt.x; }
        document.getElementById("chatView").scrollTop = 1e9;
      }
    }
    if (acc === "" && bubble.textContent === "…") bubble.textContent = "(no response)";
  } catch (e) {
    bubble.textContent = "Connection error: " + e.message;
  } finally {
    document.getElementById("send").disabled = false;
  }
}

function addEvent(evt) {
  const view = document.getElementById("activityView");
  const el = document.createElement("div"); el.className = "event";
  const t = document.createElement("div"); t.className = "t"; t.textContent = evt.title || evt.kind || "Event";
  const b = document.createElement("div"); b.className = "b"; b.textContent = evt.body || "";
  const ts = document.createElement("div"); ts.className = "ts";
  ts.textContent = new Date((evt.time || Date.now()/1000) * 1000).toLocaleTimeString();
  el.append(t, b, ts); view.prepend(el);
}

let es;
function connectEvents() {
  setStatus(false, "connecting…");
  es = new EventSource("/api/events?token=" + encodeURIComponent(token));
  es.onopen = () => setStatus(true, "live");
  es.onerror = () => { setStatus(false, "reconnecting…"); };
  es.onmessage = (m) => {
    try { const evt = JSON.parse(m.data); if (evt.kind !== "hello") addEvent(evt); setStatus(true, "live"); } catch {}
  };
}

document.getElementById("input") && document.getElementById("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
boot();
</script>
</body>
</html>
"""


def main() -> None:
    global CONFIG
    CONFIG = load_config()
    HUB.config = CONFIG
    port = int(CONFIG.get("port", 8765))
    server = ThreadingHTTPServer(("0.0.0.0", port), MirrorHandler)
    ip = lan_ip()
    print(f"AVIS Mirror running:  http://{ip}:{port}/?token={CONFIG['token']}")
    print(f"Token: {CONFIG['token']}")
    if CONFIG.get("ntfy_topic"):
        print(f"ntfy notifications -> {CONFIG.get('ntfy_server')}/{CONFIG['ntfy_topic']}")
    else:
        print("ntfy topic not set (no phone push). Set it in the app's Mirror page.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
