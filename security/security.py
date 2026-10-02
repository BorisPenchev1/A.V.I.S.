"""Permission policy and audit logging for AVIS tools.

Permission is decided per request from the caller's :class:`~security.identity.Principal`
(who, which device, which network, how trusted) rather than from a single global
table. The audit log is hash-chained so a stored record cannot be altered
without breaking the chain — it is the trustworthy memory a future security
subsystem ("avis-guard") reads from.
"""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from security.identity import OWNER, Principal
from store import db


PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUDIT_LOG = Path(os.getenv("AVIS_AUDIT_LOG", str(PROJECT_ROOT / "audit.log")))
GUARD_LOG = Path(os.getenv("AVIS_GUARD_LOG", str(PROJECT_ROOT / "guard.log")))
PERMISSIONS = {
    "open_app": "ASK",
    "close_app": "ASK",
    "search_files": "ASK",
    "get_clipboard": "ALLOW",
    "get_time_date": "ALLOW",
    "get_battery_status": "ALLOW",
    "get_bluetooth_devices": "ALLOW",
    "get_bluetooth_battery": "ALLOW",
    "get_lock_status": "ALLOW",
    "list_notifications": "ASK",
    "connect_bluetooth_device": "ASK",
    "disconnect_bluetooth_device": "ASK",
    "media_play_pause": "ASK",
    "lock_device": "ASK",
    "open_file": "ASK",
    "set_volume": "ASK",
    "make_reminder": "ASK",
    "add_event_in_calendar": "ASK",
    "read_calendar_for_date": "ALLOW",
    "run_mac_diagnostics": "ASK",
    "remember_long_term": "ASK",
    "web_search": "ALLOW",
    "web_fetch": "ALLOW",
}
VERIFY_WITH_QWEN = frozenset({
    "open_app",
    "close_app",
    "make_reminder",
    "add_event_in_calendar",
})

# Tools that can change device or account state, or reach sensitive data. These
# are gated harder for low-privilege roles and for anything arriving remotely or
# from an untrusted device.
SENSITIVE = frozenset({
    "open_app",
    "close_app",
    "open_file",
    "search_files",
    "connect_bluetooth_device",
    "disconnect_bluetooth_device",
    "lock_device",
    "set_volume",
    "media_play_pause",
    "make_reminder",
    "add_event_in_calendar",
    "run_mac_diagnostics",
    "remember_long_term",
})

# The least-privileged role permitted to use a tool at all. Anything not listed
# is available to every role (subject to the ALLOW/ASK base and context checks).
MIN_ROLE = {
    "open_app": "teen",
    "close_app": "teen",
    "open_file": "adult",
    "search_files": "adult",
    "connect_bluetooth_device": "adult",
    "disconnect_bluetooth_device": "adult",
    "lock_device": "adult",
    "set_volume": "teen",
    "media_play_pause": "child",
    "make_reminder": "child",
    "add_event_in_calendar": "teen",
    "run_mac_diagnostics": "adult",
    "remember_long_term": "adult",
}


def requires_verification(tool_name: str) -> bool:
    """Return whether an otherwise successful outcome needs interpretation."""
    return tool_name in VERIFY_WITH_QWEN


def decide(tool_name: str, principal: Principal) -> str:
    """Return ALLOW, ASK, or DENY for ``tool_name`` in this principal's context.

    Starts from the tool's base policy, then tightens it based on role, network
    origin, and device trust. It never *loosens* the base policy.
    """
    base = PERMISSIONS.get(tool_name, "DENY")
    if base == "DENY":
        return "DENY"

    # Role floor: a caller below the tool's minimum role cannot use it.
    floor = MIN_ROLE.get(tool_name)
    if floor is not None and not principal.at_least(floor):
        return "DENY"

    # A quarantined device can only ever read-only-nothing: deny everything.
    if principal.trust == "quarantined":
        return "DENY"

    # Sensitive tools get stricter treatment away from a trusted LAN device.
    if tool_name in SENSITIVE:
        if principal.source == "remote" or principal.trust != "trusted":
            # Force an explicit confirmation instead of silently allowing.
            return "ASK"

    # A new (un-enrolled) device may only do things that were already ALLOW.
    if principal.trust != "trusted" and base == "ALLOW":
        return "ASK"

    return base


_GENESIS_HASH = "0" * 64


def _last_audit_hash() -> str:
    """Read the hash of the last audit record so the next one can chain to it."""
    try:
        last_line = ""
        with AUDIT_LOG.open("r", encoding="utf-8") as log_file:
            for line in log_file:
                if line.strip():
                    last_line = line
        if not last_line:
            return _GENESIS_HASH
        return json.loads(last_line).get("hash", _GENESIS_HASH)
    except (OSError, json.JSONDecodeError):
        return _GENESIS_HASH


def audit(
    tool_name: str,
    permission: str,
    outcome: str,
    arguments: dict[str, Any],
    principal: Principal = OWNER,
) -> None:
    """Append one hash-chained JSON-lines record for every tool decision.

    Each record embeds ``prev`` (the previous record's hash) and its own
    ``hash`` over the record body, so any later edit or deletion breaks the
    chain and is detectable.
    """
    body = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user": principal.user,
        "role": principal.role,
        "device": principal.device_id,
        "source": principal.source,
        "trust": principal.trust,
        "tool": tool_name,
        "permission": permission,
        "outcome": outcome,
        "arguments": arguments,
    }
    prev = _last_audit_hash()
    digest = hashlib.sha256(
        (prev + json.dumps(body, default=str, sort_keys=True)).encode("utf-8")
    ).hexdigest()
    record = {**body, "prev": prev, "hash": digest}
    try:
        with AUDIT_LOG.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass


def guard_event(kind: str, principal: Principal, detail: dict[str, Any]) -> None:
    """Emit a structured security event for a future 'avis-guard' to consume."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "user": principal.user,
        "role": principal.role,
        "device": principal.device_id,
        "source": principal.source,
        "trust": principal.trust,
        **detail,
    }
    try:
        with GUARD_LOG.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass
    # Mirror the event into the database so dashboards can query it uniformly.
    db.log_event(
        kind, username=principal.user, role=principal.role,
        device=principal.device_id, source=principal.source, detail=detail,
    )


def execute_tool(
    name: str,
    arguments: dict[str, Any],
    functions: dict[str, Callable[..., str]],
    request_permission: Callable[[str, dict[str, Any]], bool] | None = None,
    principal: Principal | None = None,
) -> str:
    """Apply the identity-aware policy before calling a tool function.

    ``principal`` defaults to the owner on a trusted local device, so existing
    local callers (CLI, desktop app) keep their current behavior.
    """
    principal = principal or OWNER
    permission = decide(name, principal)

    if permission == "DENY":
        audit(name, permission, "denied", arguments, principal)
        guard_event("denied", principal, {"tool": name, "reason": "policy"})
        raise PermissionError(f"The {name} tool is not permitted for {principal.role}.")

    if permission == "ASK" and (
        request_permission is None or not request_permission(name, arguments)
    ):
        audit(name, permission, "denied", arguments, principal)
        guard_event("denied", principal, {"tool": name, "reason": "not_confirmed"})
        raise PermissionError(f"Permission was not granted for {name}.")

    try:
        result = functions[name](**arguments)
    except Exception as error:
        audit(name, permission, f"error: {error}", arguments, principal)
        raise
    audit(name, permission, "executed", arguments, principal)
    return result
