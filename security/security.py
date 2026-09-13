"""Permission policy and audit logging for AVIS tools."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUDIT_LOG = Path(os.getenv("AVIS_AUDIT_LOG", str(PROJECT_ROOT / "audit.log")))
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
}
VERIFY_WITH_QWEN = frozenset({
    "open_app",
    "close_app",
    "make_reminder",
    "add_event_in_calendar",
})


def requires_verification(tool_name: str) -> bool:
    """Return whether an otherwise successful outcome needs interpretation."""
    return tool_name in VERIFY_WITH_QWEN


def audit(tool_name: str, permission: str, outcome: str, arguments: dict[str, Any]) -> None:
    """Append one JSON-lines record for every tool decision."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool": tool_name,
        "permission": permission,
        "outcome": outcome,
        "arguments": arguments,
    }
    try:
        with AUDIT_LOG.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass


def execute_tool(
    name: str,
    arguments: dict[str, Any],
    functions: dict[str, Callable[..., str]],
    request_permission: Callable[[str, dict[str, Any]], bool] | None = None,
) -> str:
    """Apply ALLOW, ASK, or DENY before calling a tool function."""
    permission = PERMISSIONS.get(name, "DENY")
    if permission == "DENY":
        audit(name, permission, "denied", arguments)
        raise PermissionError(f"The {name} tool is not permitted.")
    if permission == "ASK" and (
        request_permission is None or not request_permission(name, arguments)
    ):
        audit(name, permission, "denied", arguments)
        raise PermissionError(f"Permission was not granted for {name}.")

    try:
        result = functions[name](**arguments)
    except Exception as error:
        audit(name, permission, f"error: {error}", arguments)
        raise
    audit(name, permission, "executed", arguments)
    return result
