"""One-shot auth/admin bridge for the native macOS app.

The Swift app shells out to ``python -m main.auth_bridge '<json>'`` the same way
it uses ``main.bridge`` for chat, and reads a single JSON object from stdout.
This keeps all identity logic in Python (reusing :mod:`security.identity` and
:mod:`store.db`) instead of duplicating it in Swift.

Operations (the ``op`` field):
    login        {username, password}            -> {ok, user, role}
    register     {username, password}            -> {ok, user, role}
    summary      {as_user}                        -> {ok, stats, users, roles, ...}
    set_role     {as_user, username, role}        -> {ok}
    delete_user  {as_user, username}              -> {ok}

Admin operations require ``as_user`` to currently hold the ``admin`` role in the
database (defense in depth on top of the app's own routing).
"""

from __future__ import annotations

import json
import sys
import time

from security.identity import (
    DEFAULT_SIGNUP_ROLE,
    ROLES,
    create_user,
    user_exists,
    verify_user,
)
from store import db


def _out(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, default=str) + "\n")
    sys.stdout.flush()


def _is_admin(username: str) -> bool:
    record = db.get_user((username or "").strip().casefold())
    return bool(record and record.get("role") == "admin")


def _summary() -> dict:
    day_ago = time.time() - 86400
    stats = {
        "users": db.count_users(),
        "messages_total": db.count_events("message"),
        "messages_today": db.count_events("message", day_ago),
        "logins_today": db.count_events("login", day_ago),
        "failed_logins_today": db.count_events("login_failed", day_ago),
        "denied_today": db.count_events("denied", day_ago),
    }
    return {
        "ok": True,
        "stats": stats,
        "users": db.list_users(),
        "roles": list(ROLES),
        "activity": db.recent_events(40, kinds=("message", "login", "register")),
        "security": db.recent_events(40, kinds=("login_failed", "denied")),
    }


def _profile(username: str) -> dict:
    """Per-account settings payload: identity, usage counters, and devices."""
    record = db.get_user(username)
    if record is None:
        return {"ok": False, "error": "No such account."}
    day_ago = time.time() - 86400
    devices = db.user_devices(username)
    primary = devices[0] if devices else None
    return {
        "ok": True,
        "account": {
            "username": record["username"],
            "role": record.get("role", "guest"),
            "created": record.get("created", ""),
            "last_seen": record.get("last_seen"),
            "primary_device": _device_label(primary) if primary else "This Mac",
        },
        "usage": {
            "messages_total": db.count_events_for_user("message", username),
            "messages_today": db.count_events_for_user("message", username, day_ago),
            "logins_total": db.count_events_for_user("login", username),
            "logins_today": db.count_events_for_user("login", username, day_ago),
        },
        "devices": [
            {
                "label": _device_label(d),
                "source": d.get("source", "unknown"),
                "events": d.get("events", 0),
                "last_seen": d.get("last_seen"),
                "first_seen": d.get("first_seen"),
            }
            for d in devices
        ],
        "activity": db.recent_events_for_user(username, 20, kinds=("message", "login", "register")),
    }


def _device_label(device: dict) -> str:
    """A friendly name for a (source, device) pair from the event log."""
    source = (device.get("source") or "").casefold()
    name = (device.get("device") or "").strip()
    if source == "app":
        return "AVIS on this Mac"
    if source in {"lan", "remote"}:
        where = "local network" if source == "lan" else "remote"
        return (name or "iPhone mirror") + f" ({where})"
    return name or source or "Unknown device"


def handle(req: dict) -> dict:
    op = req.get("op")

    if op == "login":
        username = str(req.get("username", "")).strip()
        password = str(req.get("password", ""))
        record = verify_user(username, password)
        if record is None:
            db.log_event("login_failed", username=username.casefold() or None, source="app")
            return {"ok": False, "error": "Invalid username or password."}
        db.touch_user(record["user"])
        db.log_event("login", username=record["user"], role=record["role"], source="app")
        return {"ok": True, "user": record["user"], "role": record["role"]}

    if op == "register":
        username = str(req.get("username", "")).strip()
        password = str(req.get("password", ""))
        if len(username) < 3 or len(password) < 6:
            return {"ok": False, "error": "Username needs 3+ and password 6+ characters."}
        if user_exists(username):
            return {"ok": False, "error": "That username is taken."}
        create_user(username, password, DEFAULT_SIGNUP_ROLE)
        db.log_event("register", username=username.casefold(), role=DEFAULT_SIGNUP_ROLE, source="app")
        record = verify_user(username, password)
        db.touch_user(record["user"])
        return {"ok": True, "user": record["user"], "role": record["role"], "new_account": True}

    if op == "profile":
        # A signed-in account's own settings page: account info, usage, devices.
        # Any account may read its own profile; admins may read anyone's.
        username = str(req.get("username", "")).strip().casefold()
        as_user = str(req.get("as_user", "")).strip().casefold()
        if not username:
            return {"ok": False, "error": "No account specified."}
        if username != as_user and not _is_admin(as_user):
            return {"ok": False, "error": "You can only view your own profile."}
        return _profile(username)

    # ---- admin operations -------------------------------------------------
    as_user = str(req.get("as_user", "")).strip().casefold()
    if not _is_admin(as_user):
        return {"ok": False, "error": "Admin privileges required."}

    if op == "summary":
        return _summary()

    if op == "set_role":
        username = str(req.get("username", "")).strip().casefold()
        role = str(req.get("role", ""))
        if role not in ROLES:
            return {"ok": False, "error": f"Role must be one of {list(ROLES)}."}
        if username == as_user:
            return {"ok": False, "error": "You cannot change your own admin account."}
        ok = db.set_role(username, role)
        db.log_event("role_change", username=username, role=role, detail={"by": as_user})
        return {"ok": ok}

    if op == "delete_user":
        username = str(req.get("username", "")).strip().casefold()
        if username == as_user:
            return {"ok": False, "error": "You cannot delete your own admin account."}
        ok = db.delete_user(username)
        db.log_event("user_deleted", username=username, detail={"by": as_user})
        return {"ok": ok}

    return {"ok": False, "error": f"Unknown operation {op!r}."}


def main() -> None:
    try:
        req = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    except (json.JSONDecodeError, IndexError):
        _out({"ok": False, "error": "Malformed request."})
        return
    try:
        _out(handle(req))
    except Exception as error:  # never crash the app; report instead
        _out({"ok": False, "error": str(error)})


if __name__ == "__main__":
    main()
