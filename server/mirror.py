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
from security.identity import (
    DEFAULT_SIGNUP_ROLE,
    OWNER,
    ROLES,
    Principal,
    active_sessions,
    create_user,
    issue_session,
    resolve_session,
    revoke_session,
    user_count,
    user_exists,
    verify_user,
)
from store import db


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "mirror_config.json"
ACTIVITY_PATH = PROJECT_ROOT / "mirror_activity.jsonl"

DEFAULT_CONFIG = {
    "port": 8765,
    "token": "",
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
    "allow_tools": False,   # when true, the phone may run actions without per-chat grants
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

class Session:
    """Per-conversation state, bound to an authenticated principal.

    Permission for ASK-tier tools is decided from the principal's identity, not
    from the chat text. A trusted owner/adult on the LAN implicitly confirms;
    everyone else (lower roles, remote origin, untrusted devices) is denied and
    the attempt is left for the audit/guard logs. This removes the old
    natural-language grant parser, which could be steered by injected text.
    """

    def __init__(self, principal: Principal | None = None) -> None:
        self.memory = ConversationMemory()
        self.principal = principal or OWNER

    def permission(self):
        principal = self.principal

        def request_permission(name: str, arguments: dict[str, Any]) -> bool:
            # Only reached for ASK-tier tools. Confirm automatically for a
            # trusted adult-or-higher on the local network; deny otherwise.
            return (
                principal.source == "lan"
                and principal.trust == "trusted"
                and principal.at_least("adult")
            )

        return request_permission


SESSIONS: dict[str, Session] = {}
SESSIONS_LOCK = threading.Lock()


def get_session(session_id: str, principal: Principal) -> Session:
    with SESSIONS_LOCK:
        session = SESSIONS.get(session_id)
        if session is None:
            session = Session(principal)
            SESSIONS[session_id] = session
        else:
            # Keep the conversation memory, but always use the freshly
            # authenticated principal (role/device/source) for this request.
            session.principal = principal
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
    def _client_source(self) -> str:
        """Classify the caller as on the local network ("lan") or "remote"."""
        ip = (self.client_address or ["", 0])[0]
        if (
            ip.startswith(("10.", "192.168.", "127."))
            or ip in ("::1", "localhost")
            or ip.startswith("172.")  # 172.16/12 private range (approx)
            or ip.startswith("fe80")
        ):
            return "lan"
        return "remote"

    def _authorized(self, params: dict[str, list[str]]) -> bool:
        """Resolve the caller to a Principal, stored on ``self.principal``.

        Priority: a per-user session token (from login), then the legacy shared
        config token (mapped to the owner) for backward compatibility until
        family accounts are created.
        """
        source = self._client_source()

        session_token = self.headers.get("X-Avis-Session") or params.get("s", [None])[0]
        principal = resolve_session(session_token, source=source)
        if principal is not None:
            self.principal = principal
            return True

        token = CONFIG.get("token", "")
        header = self.headers.get("X-Avis-Token")
        query = params.get("token", [None])[0]
        if bool(token) and (header == token or query == token):
            # Legacy device: the shared token grants owner rights, but respects
            # the real network origin (remote shared-token use is still gated by
            # the policy's stricter remote handling).
            self.principal = Principal(user="owner", role="owner", device_id="legacy-token", source=source)
            return True

        self.principal = None
        return False

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

    def _begin_sse(self, close: bool = False) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # A one-shot chat stream must close so the client sees end-of-stream and
        # the socket is freed; the events feed stays open (persistent EventSource).
        if close:
            self.close_connection = True
            self.send_header("Connection", "close")
        else:
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
        elif route == "/api/me":
            if not self._authorized(params):
                self._send_json(401, {"error": "unauthorized"})
                return
            self._me()
        elif route == "/api/admin/summary":
            if not self._authorized(params):
                self._send_json(401, {"error": "unauthorized"})
                return
            self._admin_summary()
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        route = parsed.path

        # Login and register are authenticated by credentials, not a session,
        # so they must be reachable without one.
        if route == "/api/login":
            self._login()
            return
        if route == "/api/register":
            self._register()
            return

        if not self._authorized(params):
            self._send_json(401, {"error": "unauthorized"})
            return

        if route == "/api/chat":
            self._stream_chat()
        elif route == "/api/logout":
            self._logout()
        elif route == "/api/admin/user":
            self._admin_user()
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

    # -- auth ------------------------------------------------------------
    def _login(self) -> None:
        payload = self._read_json()
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        device_id = str(payload.get("device", "")).strip() or "unknown-device"
        source = self._client_source()
        record = verify_user(username, password)
        if record is None:
            # A failed login is exactly the kind of event the bodyguard watches.
            db.log_event("login_failed", username=username.casefold() or None,
                         device=device_id, source=source)
            self._send_json(401, {"error": "invalid credentials"})
            return
        principal = Principal(
            user=record["user"],
            role=record["role"],
            device_id=device_id,
            source=source,
            trust="trusted",
        )
        token = issue_session(principal)
        db.touch_user(principal.user)
        db.log_event("login", username=principal.user, role=principal.role,
                     device=device_id, source=source)
        self._send_json(200, {"token": token, "user": principal.user, "role": principal.role})

    def _register(self) -> None:
        payload = self._read_json()
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        device_id = str(payload.get("device", "")).strip() or "unknown-device"
        if len(username) < 3 or len(password) < 6:
            self._send_json(400, {"error": "username needs 3+ and password 6+ characters"})
            return
        if user_exists(username):
            self._send_json(409, {"error": "that username is taken"})
            return
        create_user(username, password, DEFAULT_SIGNUP_ROLE)
        db.log_event("register", username=username.casefold(), role=DEFAULT_SIGNUP_ROLE,
                     device=device_id, source=self._client_source())
        # Log the new account straight in.
        record = verify_user(username, password)
        principal = Principal(record["user"], record["role"], device_id,
                              self._client_source(), "trusted")
        token = issue_session(principal)
        db.touch_user(principal.user)
        self._send_json(200, {"token": token, "user": principal.user, "role": principal.role})

    def _me(self) -> None:
        p = self.principal or OWNER
        self._send_json(200, {"user": p.user, "role": p.role, "source": p.source})

    def _logout(self) -> None:
        token = self.headers.get("X-Avis-Session")
        revoke_session(token)
        self._send_json(200, {"ok": True})

    # -- admin -----------------------------------------------------------
    def _require_admin(self) -> bool:
        if (self.principal or OWNER).role != "admin":
            self._send_json(403, {"error": "admin only"})
            return False
        return True

    def _admin_summary(self) -> None:
        if not self._require_admin():
            return
        day_ago = time.time() - 86400
        stats = {
            "users": user_count(),
            "active_sessions": active_sessions(),
            "messages_total": db.count_events("message"),
            "messages_today": db.count_events("message", day_ago),
            "logins_today": db.count_events("login", day_ago),
            "failed_logins_today": db.count_events("login_failed", day_ago),
            "denied_today": db.count_events("denied", day_ago),
        }
        self._send_json(200, {
            "stats": stats,
            "users": db.list_users(),
            "roles": list(ROLES),
            "activity": db.recent_events(40, kinds=("message", "login", "register")),
            "security": db.recent_events(40, kinds=("login_failed", "denied")),
        })

    def _admin_user(self) -> None:
        if not self._require_admin():
            return
        payload = self._read_json()
        action = str(payload.get("action", ""))
        username = str(payload.get("username", "")).strip().casefold()
        if not username:
            self._send_json(400, {"error": "username required"})
            return
        if username == (self.principal or OWNER).user:
            self._send_json(400, {"error": "you cannot change your own admin account here"})
            return
        if action == "set_role":
            role = str(payload.get("role", ""))
            if role not in ROLES:
                self._send_json(400, {"error": f"role must be one of {list(ROLES)}"})
                return
            ok = db.set_role(username, role)
            db.log_event("role_change", username=username, role=role,
                         detail={"by": (self.principal or OWNER).user})
            self._send_json(200 if ok else 404, {"ok": ok})
        elif action == "delete":
            ok = db.delete_user(username)
            db.log_event("user_deleted", username=username,
                         detail={"by": (self.principal or OWNER).user})
            self._send_json(200 if ok else 404, {"ok": ok})
        else:
            self._send_json(400, {"error": "unknown action"})

    # -- SSE endpoints ---------------------------------------------------
    def _stream_chat(self) -> None:
        payload = self._read_json()
        prompt = str(payload.get("prompt", "")).strip()
        session_id = str(payload.get("session", "default"))
        if not prompt:
            self._send_json(400, {"error": "empty prompt"})
            return

        session = get_session(session_id, self.principal or OWNER)
        p = session.principal
        db.log_event("message", username=p.user, role=p.role,
                     device=p.device_id, source=p.source,
                     detail={"chars": len(prompt)})

        self._begin_sse(close=True)
        try:
            def on_token(token: str) -> None:
                self._sse({"t": "tok", "x": token})

            response = run_assistant(
                prompt, session.permission(), session.memory, on_token,
                principal=session.principal,
            )
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
<title>AVIS</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0b1020; --bg2: #0d1428; --panel: #131c33; --panel2: #10182b;
    --line: #1f2937; --line2: #26324d; --text: #e5e7eb; --muted: #9fb0c9;
    --dim: #64748b; --accent: #2563eb; --accent2: #3b82f6;
    --green: #22c55e; --red: #ef4444; --amber: #f59e0b;
    --radius: 14px;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { margin: 0; font: 16px/1.45 -apple-system, system-ui, "Segoe UI", sans-serif;
         background: radial-gradient(1200px 600px at 50% -10%, #101a36 0%, var(--bg) 60%);
         color: var(--text); min-height: 100dvh; }
  .hide { display: none !important; }
  button { cursor: pointer; font: inherit; }
  input, select { font: inherit; }
  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-thumb { background: #1e293b; border-radius: 8px; }

  /* ---- Auth screen ---- */
  .auth-wrap { min-height: 100dvh; display: flex; align-items: center; justify-content: center; padding: 24px; }
  .card { width: 100%; max-width: 400px; background: var(--bg2); border: 1px solid var(--line);
          border-radius: 20px; padding: 28px 24px; box-shadow: 0 24px 60px rgba(0,0,0,.45); }
  .brand { display: flex; align-items: center; gap: 10px; justify-content: center; margin-bottom: 4px; }
  .brand .glyph { width: 34px; height: 34px; border-radius: 10px; background: linear-gradient(135deg, var(--accent), #7c3aed);
                  display: grid; place-items: center; font-weight: 800; }
  .brand b { font-size: 22px; letter-spacing: .14em; }
  .sub { text-align: center; color: var(--muted); font-size: 14px; margin: 6px 0 20px; }
  .seg { display: flex; background: var(--panel2); border: 1px solid var(--line); border-radius: 12px; padding: 4px; margin-bottom: 18px; }
  .seg button { flex: 1; border: 0; background: transparent; color: var(--muted); font-weight: 600; padding: 9px; border-radius: 9px; }
  .seg button.active { background: var(--accent); color: #fff; }
  .field { margin: 12px 0; }
  .field label { display: block; font-size: 13px; color: var(--muted); margin-bottom: 6px; }
  .field input { width: 100%; padding: 12px 13px; border-radius: 12px; border: 1px solid var(--line2);
                 background: var(--panel); color: var(--text); }
  .field input:focus { outline: none; border-color: var(--accent2); }
  .primary { width: 100%; padding: 13px; border: 0; border-radius: 12px; background: var(--accent); color: #fff;
             font-weight: 700; margin-top: 8px; }
  .primary:disabled { opacity: .55; }
  .err { color: #fca5a5; font-size: 13px; min-height: 18px; margin-top: 10px; text-align: center; }
  .hint { color: var(--dim); font-size: 13px; }

  /* ---- App shell ---- */
  .shell { display: flex; flex-direction: column; height: 100dvh; }
  header { padding: max(env(safe-area-inset-top), 12px) 16px 10px; display: flex; align-items: center; gap: 10px;
           border-bottom: 1px solid var(--line); background: var(--bg2); }
  header .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--red); flex: none; }
  header .dot.on { background: var(--green); }
  header b { font-size: 18px; letter-spacing: .06em; }
  header .sp { flex: 1; }
  .chip { display: flex; align-items: center; gap: 8px; background: var(--panel); border: 1px solid var(--line);
          border-radius: 999px; padding: 5px 6px 5px 12px; font-size: 13px; color: var(--muted); }
  .badge { font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 999px; text-transform: uppercase; letter-spacing: .04em; }
  .badge.admin { background: rgba(124,58,237,.2); color: #c4b5fd; }
  .badge.owner { background: rgba(37,99,235,.2); color: #93c5fd; }
  .badge.adult { background: rgba(34,197,94,.16); color: #86efac; }
  .badge.teen { background: rgba(245,158,11,.16); color: #fcd34d; }
  .badge.child { background: rgba(148,163,184,.18); color: #cbd5e1; }
  .badge.guest { background: rgba(148,163,184,.14); color: #94a3b8; }
  .icon-btn { border: 1px solid var(--line2); background: var(--panel); color: var(--muted); border-radius: 10px; padding: 6px 10px; font-size: 13px; }
  .tabs { display: flex; gap: 6px; padding: 8px 12px; background: var(--bg2); border-bottom: 1px solid var(--line); }
  .tabs button { flex: 1; padding: 9px; border: 0; border-radius: 10px; background: var(--panel); color: var(--muted); font-weight: 600; }
  .tabs button.active { background: var(--accent); color: #fff; }
  main { flex: 1; overflow-y: auto; padding: 14px; -webkit-overflow-scrolling: touch; }

  /* ---- Chat ---- */
  .msg { margin: 8px 0; display: flex; }
  .msg .bubble { max-width: 82%; padding: 10px 13px; border-radius: 16px; white-space: pre-wrap; word-wrap: break-word; }
  .msg.user { justify-content: flex-end; }
  .msg.user .bubble { background: var(--accent); color: #fff; border-bottom-right-radius: 5px; }
  .msg.assistant .bubble { background: var(--panel); border: 1px solid var(--line); border-bottom-left-radius: 5px; }
  footer { padding: 10px 12px calc(env(safe-area-inset-bottom) + 10px); border-top: 1px solid var(--line);
           background: var(--bg2); display: flex; gap: 8px; }
  textarea { flex: 1; resize: none; background: var(--panel); border: 1px solid var(--line2); color: var(--text);
             border-radius: var(--radius); padding: 10px 13px; font: inherit; max-height: 120px; }
  footer .send { border: 0; border-radius: var(--radius); background: var(--accent); color: #fff; font-weight: 700; padding: 0 18px; font-size: 18px; }
  footer .send:disabled { opacity: .5; }

  /* ---- Activity / events ---- */
  .event { margin: 8px 0; padding: 10px 13px; background: var(--panel2); border: 1px solid var(--line); border-radius: 12px; }
  .event .t { font-weight: 700; font-size: 14px; }
  .event .b { color: var(--muted); font-size: 14px; white-space: pre-wrap; }
  .event .ts { color: var(--dim); font-size: 12px; margin-top: 4px; }

  /* ---- Dashboard ---- */
  .section-title { font-size: 13px; text-transform: uppercase; letter-spacing: .08em; color: var(--dim); margin: 18px 4px 8px; }
  .grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }
  @media (min-width: 620px) { .grid { grid-template-columns: repeat(4, 1fr); } }
  .stat { background: var(--panel2); border: 1px solid var(--line); border-radius: 14px; padding: 14px; }
  .stat .n { font-size: 26px; font-weight: 800; }
  .stat .l { font-size: 12px; color: var(--muted); margin-top: 2px; }
  .stat.warn .n { color: var(--amber); }
  .stat.bad .n { color: var(--red); }
  .panel { background: var(--panel2); border: 1px solid var(--line); border-radius: 14px; overflow: hidden; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--line); }
  th { color: var(--dim); font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }
  tr:last-child td { border-bottom: 0; }
  td select { background: var(--panel); border: 1px solid var(--line2); color: var(--text); border-radius: 8px; padding: 5px 7px; }
  .link-danger { background: transparent; border: 1px solid rgba(239,68,68,.4); color: #fca5a5; border-radius: 8px; padding: 5px 9px; font-size: 13px; }
  .row-list .item { display: flex; align-items: baseline; gap: 8px; padding: 9px 12px; border-bottom: 1px solid var(--line); font-size: 14px; }
  .row-list .item:last-child { border-bottom: 0; }
  .row-list .who { font-weight: 700; }
  .row-list .k { color: var(--muted); }
  .row-list .when { margin-left: auto; color: var(--dim); font-size: 12px; white-space: nowrap; }
  .row-list .item.alert .k { color: #fca5a5; }
  .empty { padding: 16px; color: var(--dim); text-align: center; font-size: 14px; }
</style>
</head>
<body>

<!-- ===================== AUTH ===================== -->
<div id="auth" class="auth-wrap hide">
  <div class="card">
    <div class="brand"><span class="glyph">A</span><b>AVIS</b></div>
    <div class="sub">Your home assistant</div>
    <div class="seg">
      <button id="segLogin" class="active" onclick="setMode('login')">Sign in</button>
      <button id="segReg" onclick="setMode('register')">Create account</button>
    </div>
    <div class="field"><label>Username</label>
      <input id="uName" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="e.g. mom"></div>
    <div class="field"><label>Password</label>
      <input id="uPass" type="password" placeholder="Your password"></div>
    <div class="field hide" id="confirmField"><label>Confirm password</label>
      <input id="uPass2" type="password" placeholder="Repeat password"></div>
    <button id="authBtn" class="primary" onclick="submitAuth()">Sign in</button>
    <div class="err" id="authErr"></div>
  </div>
</div>

<!-- ===================== USER SHELL (chat) ===================== -->
<div id="userShell" class="shell hide">
  <header>
    <span class="dot" id="status"></span><b>AVIS</b>
    <span class="sp"></span>
    <span class="chip"><span id="whoName">—</span><span class="badge" id="whoRole">·</span></span>
    <button class="icon-btn" onclick="logout()">Sign out</button>
  </header>
  <div class="tabs">
    <button id="tabChat" class="active" onclick="showTab('chat')">Chat</button>
    <button id="tabActivity" onclick="showTab('activity')">Activity</button>
  </div>
  <main id="chatView"></main>
  <main id="activityView" class="hide"></main>
  <footer id="composer">
    <textarea id="input" rows="1" placeholder="Ask AVIS…" oninput="autosize(this)"></textarea>
    <button class="send" id="send" onclick="send()">↑</button>
  </footer>
</div>

<!-- ===================== ADMIN SHELL (dashboard) ===================== -->
<div id="adminShell" class="shell hide">
  <header>
    <span class="dot on"></span><b>AVIS</b><span class="hint">&nbsp;· Admin</span>
    <span class="sp"></span>
    <button class="icon-btn" onclick="loadDashboard()">Refresh</button>
    <span class="chip"><span id="adWho">admin</span><span class="badge admin">admin</span></span>
    <button class="icon-btn" onclick="logout()">Sign out</button>
  </header>
  <div class="tabs">
    <button id="dtabOverview" class="active" onclick="showDash('overview')">Overview</button>
    <button id="dtabUsers" onclick="showDash('users')">Users</button>
    <button id="dtabSecurity" onclick="showDash('security')">Security</button>
  </div>
  <main>
    <section id="dashOverview">
      <div class="section-title">Usage</div>
      <div class="grid" id="statGrid"></div>
      <div class="section-title">Recent activity</div>
      <div class="panel row-list" id="activityList"></div>
    </section>
    <section id="dashUsers" class="hide">
      <div class="section-title">Accounts</div>
      <div class="panel"><table id="usersTable"></table></div>
      <p class="hint" style="margin:12px 4px">New sign-ups start as their default role. Promote them here.</p>
    </section>
    <section id="dashSecurity" class="hide">
      <div class="section-title">Security events — failed logins &amp; denied actions</div>
      <div class="panel row-list" id="securityList"></div>
    </section>
  </main>
</div>

<script>
const SESSION_TOKEN_KEY = "avis.sessionToken";
const DEVICE_KEY = "avis.device";
const CHAT_SESSION_KEY = "avis.chat";
let sessionToken = localStorage.getItem(SESSION_TOKEN_KEY) || "";
let me = null;
let mode = "login";

let device = localStorage.getItem(DEVICE_KEY);
if (!device) { device = deviceLabel(); localStorage.setItem(DEVICE_KEY, device); }
let chatSession = localStorage.getItem(CHAT_SESSION_KEY);
if (!chatSession) { chatSession = rand(); localStorage.setItem(CHAT_SESSION_KEY, chatSession); }

function rand() { return Math.random().toString(36).slice(2) + Date.now().toString(36); }
function deviceLabel() {
  const ua = navigator.userAgent;
  let os = "device";
  if (/iPhone/.test(ua)) os = "iPhone"; else if (/iPad/.test(ua)) os = "iPad";
  else if (/Android/.test(ua)) os = "Android"; else if (/Mac/.test(ua)) os = "Mac";
  else if (/Windows/.test(ua)) os = "Windows"; else if (/Linux/.test(ua)) os = "Linux";
  return os + "-" + rand().slice(0, 4);
}
function authHeaders(extra) {
  return Object.assign({ "Content-Type": "application/json", "X-Avis-Session": sessionToken }, extra || {});
}
function show(id) { document.getElementById(id).classList.remove("hide"); }
function hideEl(id) { document.getElementById(id).classList.add("hide"); }
function esc(s) { const d = document.createElement("div"); d.textContent = s == null ? "" : String(s); return d.innerHTML; }
function timeAgo(ts) {
  const s = Math.max(0, Math.floor(Date.now()/1000 - ts));
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s/60) + "m ago";
  if (s < 86400) return Math.floor(s/3600) + "h ago";
  return Math.floor(s/86400) + "d ago";
}

/* ---------- boot / routing ---------- */
async function boot() {
  if (!sessionToken) { return showAuth(); }
  try {
    const res = await fetch("/api/me", { headers: authHeaders() });
    if (!res.ok) { sessionToken = ""; localStorage.removeItem(SESSION_TOKEN_KEY); return showAuth(); }
    me = await res.json();
    route();
  } catch (e) { showAuth(); }
}
function route() {
  hideEl("auth");
  if (me && me.role === "admin") {
    hideEl("userShell"); show("adminShell");
    document.getElementById("adWho").textContent = me.user;
    loadDashboard();
  } else {
    hideEl("adminShell"); show("userShell");
    document.getElementById("whoName").textContent = me.user;
    const rb = document.getElementById("whoRole");
    rb.textContent = me.role; rb.className = "badge " + me.role;
    connectEvents();
  }
}

/* ---------- auth ---------- */
function showAuth() { hideEl("userShell"); hideEl("adminShell"); show("auth"); }
function setMode(m) {
  mode = m;
  document.getElementById("segLogin").classList.toggle("active", m === "login");
  document.getElementById("segReg").classList.toggle("active", m === "register");
  document.getElementById("confirmField").classList.toggle("hide", m !== "register");
  document.getElementById("authBtn").textContent = m === "login" ? "Sign in" : "Create account";
  document.getElementById("authErr").textContent = "";
}
async function submitAuth() {
  const username = document.getElementById("uName").value.trim();
  const password = document.getElementById("uPass").value;
  const errEl = document.getElementById("authErr");
  errEl.textContent = "";
  if (!username || !password) { errEl.textContent = "Enter a username and password."; return; }
  if (mode === "register") {
    const p2 = document.getElementById("uPass2").value;
    if (password !== p2) { errEl.textContent = "Passwords do not match."; return; }
  }
  const btn = document.getElementById("authBtn"); btn.disabled = true;
  try {
    const res = await fetch(mode === "login" ? "/api/login" : "/api/register", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, device })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { errEl.textContent = data.error || "Something went wrong."; return; }
    sessionToken = data.token; localStorage.setItem(SESSION_TOKEN_KEY, sessionToken);
    me = { user: data.user, role: data.role };
    document.getElementById("uPass").value = ""; document.getElementById("uPass2").value = "";
    route();
  } catch (e) { errEl.textContent = "Connection error: " + e.message; }
  finally { btn.disabled = false; }
}
async function logout() {
  try { await fetch("/api/logout", { method: "POST", headers: authHeaders() }); } catch (e) {}
  sessionToken = ""; me = null; localStorage.removeItem(SESSION_TOKEN_KEY);
  if (es) { try { es.close(); } catch (e) {} }
  showAuth();
}

/* ---------- chat (user) ---------- */
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
  const d = document.getElementById("status"); if (d) d.classList.toggle("on", on);
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
      method: "POST", headers: authHeaders(),
      body: JSON.stringify({ prompt: text, session: chatSession })
    });
    if (res.status === 401) { bubble.textContent = "Session expired — sign in again."; logout(); return; }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "", acc = "", finished = false;
    while (!finished) {
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
        else if (evt.t === "final") { bubble.textContent = evt.x || acc; finished = true; }
        else if (evt.t === "done") { if (acc === "") bubble.textContent = evt.x || acc; finished = true; }
        else if (evt.t === "err") { bubble.textContent = "Error: " + evt.x; finished = true; }
        document.getElementById("chatView").scrollTop = 1e9;
      }
    }
    try { await reader.cancel(); } catch (e) {}
    if (acc === "" && bubble.textContent === "…") bubble.textContent = "(no response)";
  } catch (e) { bubble.textContent = "Connection error: " + e.message; }
  finally { document.getElementById("send").disabled = false; }
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
  setStatus(false);
  try { if (es) es.close(); } catch (e) {}
  es = new EventSource("/api/events?s=" + encodeURIComponent(sessionToken));
  es.onopen = () => setStatus(true);
  es.onerror = () => setStatus(false);
  es.onmessage = (m) => { try { const evt = JSON.parse(m.data); if (evt.kind !== "hello") addEvent(evt); setStatus(true); } catch (e) {} };
}

/* ---------- admin dashboard ---------- */
let dashTimer = null;
function showDash(which) {
  ["overview", "users", "security"].forEach(s => {
    document.getElementById("dash" + s.charAt(0).toUpperCase() + s.slice(1)).classList.toggle("hide", s !== which);
    document.getElementById("dtab" + s.charAt(0).toUpperCase() + s.slice(1)).classList.toggle("active", s === which);
  });
}
async function loadDashboard() {
  try {
    const res = await fetch("/api/admin/summary", { headers: authHeaders() });
    if (res.status === 401) { logout(); return; }
    if (res.status === 403) return;
    const d = await res.json();
    renderStats(d.stats); renderUsers(d.users, d.roles); renderActivity(d.activity); renderSecurity(d.security);
  } catch (e) {}
  if (dashTimer) clearTimeout(dashTimer);
  dashTimer = setTimeout(() => { if (me && me.role === "admin") loadDashboard(); }, 15000);
}
function renderStats(s) {
  const cards = [
    { n: s.users, l: "Accounts" },
    { n: s.active_sessions, l: "Active sessions" },
    { n: s.messages_today, l: "Messages today" },
    { n: s.messages_total, l: "Messages total" },
    { n: s.logins_today, l: "Logins today" },
    { n: s.failed_logins_today, l: "Failed logins", cls: s.failed_logins_today > 0 ? "warn" : "" },
    { n: s.denied_today, l: "Denied actions", cls: s.denied_today > 0 ? "bad" : "" },
  ];
  document.getElementById("statGrid").innerHTML = cards.map(c =>
    '<div class="stat ' + (c.cls || "") + '"><div class="n">' + esc(c.n) + '</div><div class="l">' + esc(c.l) + '</div></div>'
  ).join("");
}
function renderUsers(users, roles) {
  let html = "<tr><th>User</th><th>Role</th><th>Last seen</th><th></th></tr>";
  if (!users.length) html += '<tr><td colspan="4" class="empty">No accounts yet.</td></tr>';
  users.forEach(u => {
    const opts = roles.map(r => '<option value="' + r + '"' + (r === u.role ? " selected" : "") + '>' + r + "</option>").join("");
    const self = me && u.username === me.user;
    const seen = u.last_seen ? timeAgo(u.last_seen) : "never";
    html += "<tr><td>" + esc(u.username) + (self ? ' <span class="hint">(you)</span>' : "") + "</td>"
      + "<td>" + (self ? '<span class="badge admin">admin</span>'
                       : '<select onchange="changeRole(\'' + esc(u.username) + "', this.value)\">" + opts + "</select>") + "</td>"
      + "<td class=\"hint\">" + esc(seen) + "</td>"
      + "<td>" + (self ? "" : '<button class="link-danger" onclick="deleteUser(\'' + esc(u.username) + '\')">Delete</button>') + "</td></tr>";
  });
  document.getElementById("usersTable").innerHTML = html;
}
function renderActivity(items) {
  const el = document.getElementById("activityList");
  if (!items || !items.length) { el.innerHTML = '<div class="empty">No activity yet.</div>'; return; }
  el.innerHTML = items.map(e => {
    const label = { message: "sent a message", login: "signed in", register: "created an account" }[e.kind] || e.kind;
    return '<div class="item"><span class="who">' + esc(e.username || "?") + '</span>'
      + '<span class="k">' + esc(label) + '</span>'
      + '<span class="when">' + esc(timeAgo(e.ts)) + '</span></div>';
  }).join("");
}
function renderSecurity(items) {
  const el = document.getElementById("securityList");
  if (!items || !items.length) { el.innerHTML = '<div class="empty">Nothing flagged. All quiet.</div>'; return; }
  el.innerHTML = items.map(e => {
    let label;
    if (e.kind === "login_failed") label = "failed login";
    else label = "denied " + (e.detail && e.detail.tool ? e.detail.tool : "action");
    const src = e.source ? " · " + e.source : "";
    return '<div class="item alert"><span class="who">' + esc(e.username || "unknown") + '</span>'
      + '<span class="k">' + esc(label) + esc(src) + '</span>'
      + '<span class="when">' + esc(timeAgo(e.ts)) + '</span></div>';
  }).join("");
}
async function changeRole(username, role) {
  await fetch("/api/admin/user", { method: "POST", headers: authHeaders(),
    body: JSON.stringify({ action: "set_role", username, role }) });
  loadDashboard();
}
async function deleteUser(username) {
  if (!confirm("Delete account '" + username + "'? This cannot be undone.")) { loadDashboard(); return; }
  await fetch("/api/admin/user", { method: "POST", headers: authHeaders(),
    body: JSON.stringify({ action: "delete", username }) });
  loadDashboard();
}

document.getElementById("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
document.getElementById("uPass2").addEventListener("keydown", (e) => { if (e.key === "Enter") submitAuth(); });
document.getElementById("uPass").addEventListener("keydown", (e) => { if (e.key === "Enter" && mode === "login") submitAuth(); });
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
    print(f"AVIS running:  http://{ip}:{port}/")
    print(f"Accounts: {user_count()} (sign in or create an account on the page).")
    print("Admin dashboard: sign in as the 'admin' account.")
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
