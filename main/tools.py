"""Small, bounded tools available to the AVIS assistant."""

import difflib
import json
import os
import platform
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from security.security import PROJECT_ROOT
from context.avis_context import add_long_term
from main.web import web_search, web_fetch


ALLOWED_APPS = frozenset(
    app.strip()
    for app in os.getenv("AVIS_ALLOWED_APPS", "Spotify,Calculator").split(",")
    if app.strip()
)


def _normalize_app_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.casefold())


def _resolve_allowed_app(name: str) -> str | None:
    normalized_name = _normalize_app_name(name)
    if not normalized_name:
        return None
    for allowed_app in ALLOWED_APPS:
        if _normalize_app_name(allowed_app) == normalized_name:
            return allowed_app
    for allowed_app in ALLOWED_APPS:
        normalized_allowed = _normalize_app_name(allowed_app)
        if abs(len(normalized_name) - len(normalized_allowed)) <= max(2, len(normalized_allowed) // 4):
            if difflib.SequenceMatcher(None, normalized_name, normalized_allowed).ratio() >= 0.82:
                return allowed_app
    return None


def open_app(name: str) -> str:
    app_name = _resolve_allowed_app(name.strip())
    if app_name is None:
        raise PermissionError(f"Opening {name.strip() or 'that app'} is not permitted.")
    if platform.system() != "Darwin":
        raise RuntimeError("open_app is only supported on macOS.")
    result = subprocess.run(["open", "-a", app_name], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "macOS could not open the application.")
    return f"Opened {app_name}."


def close_app(name: str) -> str:
    app_name = _resolve_allowed_app(name.strip())
    if app_name is None:
        raise PermissionError(f"Closing {name.strip() or 'that app'} is not permitted.")
    if platform.system() != "Darwin":
        raise RuntimeError("close_app is only supported on macOS.")
    escaped_name = app_name.replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "{escaped_name}" to quit'
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "macOS could not close the application.")
    return f"Closed {app_name}."


def _safe_path(path: str) -> Path:
    requested_path = (PROJECT_ROOT / path).resolve()
    try:
        requested_path.relative_to(PROJECT_ROOT)
    except ValueError as error:
        raise PermissionError("File access is limited to the AVIS project directory.") from error
    return requested_path


def open_file(path: str) -> str:
    file_path = _safe_path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    if file_path.stat().st_size > 100_000:
        raise ValueError("Refusing to read files larger than 100 KB.")
    try:
        return file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Only UTF-8 text files can be opened.") from error


def search_files(query: str, path: str = ".") -> str:
    if not query.strip():
        raise ValueError("Search query cannot be empty.")
    search_root = _safe_path(path)
    if not search_root.exists():
        raise FileNotFoundError(f"Path not found: {path}")
    paths = [search_root] if search_root.is_file() else search_root.rglob("*")
    matches: list[str] = []
    for file_path in paths:
        if len(matches) >= 50 or not file_path.is_file() or any(part.startswith(".") for part in file_path.parts):
            continue
        try:
            for line_number, line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), 1):
                if query.casefold() in line.casefold():
                    matches.append(f"{file_path.relative_to(PROJECT_ROOT)}:{line_number}: {line.strip()}")
                if len(matches) >= 50:
                    break
        except (UnicodeDecodeError, OSError):
            continue
    return "\n".join(matches) if matches else "No matches found."


def get_clipboard() -> str:
    if platform.system() != "Darwin":
        raise RuntimeError("get_clipboard is only supported on macOS.")
    result = subprocess.run(["pbpaste"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Could not read the clipboard.")
    return result.stdout if result.stdout else "Clipboard is empty or contains no text."


def list_notifications() -> str:
    """Open Notification Center if needed and read visible notification text."""
    if platform.system() != "Darwin":
        raise RuntimeError("Notifications are only supported on macOS.")
    script = (
        'tell application "System Events"\n'
        'set panelWasClosed to false\n'
        'tell process "NotificationCenter"\n'
        'if (count of windows) is 0 then\n'
        'set panelWasClosed to true\n'
        'end if\n'
        'end tell\n'
        'if panelWasClosed then\n'
        'tell process "ControlCenter"\n'
        'repeat with menuItem in menu bar items of menu bar 1\n'
        'try\n'
        'if (description of menuItem as text) is "Clock" then\n'
        'click menuItem\n'
        'exit repeat\n'
        'end if\n'
        'end try\n'
        'end repeat\n'
        'end tell\n'
        'delay 0.3\n'
        'end if\n'
        'tell process "NotificationCenter"\n'
        'if (count of windows) is 0 then return "Notification Center is unavailable. Enable Accessibility access for AVIS."\n'
        'set output to ""\n'
        'repeat with itemRef in (every UI element of window 1)\n'
        'try\n'
        'set itemName to name of itemRef\n'
        'if itemName is not "" then set output to output & itemName & return\n'
        'end try\n'
        'end repeat\n'
        'return output\n'
        'end tell\n'
        'end tell'
    )
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Could not read notifications. Enable Accessibility access for AVIS.")
    entries = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and line.strip().casefold() != "missing value"
    ]
    return "\n".join(entries) if entries else "No visible notifications found."


def get_time_date() -> str:
    """Return local date and time without involving the language model."""
    now = datetime.now().astimezone()
    return now.strftime("%A, %B %-d, %Y at %-I:%M:%S %p %Z")


def run_mac_diagnostics() -> str:
    """Collect read-only hardware, usage, performance, and security status."""
    if platform.system() != "Darwin":
        raise RuntimeError("Mac diagnostics are only supported on macOS.")

    def command(args: list[str], fallback: str = "unavailable") -> str:
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        return (result.stdout or result.stderr).strip() or fallback

    hardware = command(["system_profiler", "SPHardwareDataType", "-detailLevel", "mini"])
    memory = command(["vm_stat"])
    cpu = command(["sysctl", "-n", "hw.ncpu", "hw.memsize"])
    storage = command(["df", "-h", "/"])
    performance = command(["top", "-l", "1", "-n", "0", "-stats", "cpu,mem,threads"])
    gatekeeper = command(["spctl", "--status"])
    return (
        "Hardware:\n" + hardware + "\n\n"
        "CPU and memory:\n" + cpu + "\n" + memory + "\n\n"
        "Storage:\n" + storage + "\n\n"
        "Performance snapshot:\n" + performance + "\n\n"
        "Gatekeeper:\n" + gatekeeper + "\n\n"
        "Malware scan: not performed. macOS does not expose a reliable general-purpose "
        "on-demand XProtect scan command. Use Malwarebytes or another trusted scanner "
        "for an actual scan."
    )


def remember_long_term(note: str, until: str | None = None) -> str:
    """Persist a user-approved note until an optional ISO date."""
    if not note.strip():
        raise ValueError("The long-term note cannot be empty.")
    add_long_term(note, until)
    return f"I will remember that until {until}." if until else "I will remember that."


def get_battery_status() -> str:
    """Read the Mac's internal battery from the native power utility."""
    if platform.system() != "Darwin":
        raise RuntimeError("Battery status is only supported on macOS.")
    result = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Could not read battery status.")
    match = re.search(r"(\d+)%.*?;\s*([^;\n]+)(?:;\s*([^\n]+))?", result.stdout)
    if not match:
        raise RuntimeError("Could not parse battery status.")
    details = f"Battery: {match.group(1)}%; {match.group(2).strip()}"
    if match.group(3):
        remaining = re.sub(r"\s+present:\s*(?:true|false)\s*$", "", match.group(3).strip(), flags=re.IGNORECASE)
        if remaining:
            details += f"; {remaining}"
    return details + "."


def _bluetooth_data() -> dict[str, Any]:
    if platform.system() != "Darwin":
        raise RuntimeError("Bluetooth status is only supported on macOS.")
    result = subprocess.run(
        ["system_profiler", "SPBluetoothDataType", "-json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Could not read Bluetooth status.")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Could not parse Bluetooth status.") from error


def _blueutil_devices(connected_only: bool = False) -> list[tuple[str, str, bool]]:
    """Read paired Bluetooth names quickly when blueutil is installed."""
    if not shutil.which("blueutil"):
        return []
    command = ["blueutil", "--connected" if connected_only else "--paired"]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return []
    records = re.split(r"(?=address:\s*[0-9a-f]{2}(?:-[0-9a-f]{2}){5})", result.stdout, flags=re.IGNORECASE)
    devices: list[tuple[str, str, bool]] = []
    for record in records:
        address_match = re.search(r"address:\s*([0-9a-f-]{17})", record, re.IGNORECASE)
        name_match = re.search(r'name:\s*"([^"]+)"', record)
        if address_match and name_match:
            devices.append((name_match.group(1), address_match.group(1), "connected" in record))
    return devices


def _bluetooth_devices(
    data: dict[str, Any],
    include_disconnected: bool = False,
) -> list[tuple[str, dict[str, Any]]]:
    devices: list[tuple[str, dict[str, Any]]] = []
    for controller in data.get("SPBluetoothDataType", []):
        groups = list(controller.get("device_connected", []))
        if include_disconnected:
            groups.extend(controller.get("device_not_connected", []))
        for device_group in groups:
            for name, details in device_group.items():
                if isinstance(details, dict):
                    devices.append((name, details))
    return devices


def _find_bluetooth_device(name: str) -> str:
    requested = re.sub(r"[^a-z0-9]", "", name.casefold())
    fast_devices = _blueutil_devices()
    candidates = [device_name for device_name, _, _ in fast_devices]
    devices = _bluetooth_devices(_bluetooth_data(), include_disconnected=True) if not candidates else []
    candidates.extend(device_name for device_name, _ in devices)
    if not requested:
        raise ValueError("Bluetooth device name cannot be empty.")
    exact = next((candidate for candidate in candidates if requested == re.sub(r"[^a-z0-9]", "", candidate.casefold())), None)
    if exact:
        return exact
    requested_tokens = set(re.findall(r"[a-z0-9]+", name.casefold()))
    aliases = {
        "airpods": {"airpods", "airpod", "buds", "earbuds", "headphones"},
        "headphones": {"airpods", "airpod", "buds", "earbuds", "headphones"},
        "earbuds": {"airpods", "airpod", "buds", "earbuds", "headphones"},
    }
    scored = sorted(
        (
            (
                max(
                    difflib.SequenceMatcher(None, requested, re.sub(r"[^a-z0-9]", "", candidate.casefold())).ratio(),
                    max(
                        (
                            0.7
                            for token in requested_tokens
                            if token in aliases
                            and any(alias in candidate.casefold() for alias in aliases[token])
                        ),
                        default=0.0,
                    ),
                ),
                candidate,
            )
            for candidate in candidates
        ),
        reverse=True,
    )
    if scored and (scored[0][0] >= 0.55 or requested in re.sub(r"[^a-z0-9]", "", scored[0][1].casefold())):
        return scored[0][1]
    raise ValueError(f"No connected Bluetooth device matched {name}.")


def get_bluetooth_devices() -> str:
    """List currently connected Bluetooth devices."""
    fast_devices = _blueutil_devices(connected_only=True)
    if fast_devices:
        return "Connected Bluetooth devices: " + ", ".join(name for name, _, _ in fast_devices) + "."
    devices = _bluetooth_devices(_bluetooth_data())
    if not devices:
        return "No Bluetooth devices are connected."
    return "Connected Bluetooth devices: " + ", ".join(name for name, _ in devices) + "."


def get_bluetooth_battery(name: str) -> str:
    """Read battery fields for one connected Bluetooth device."""
    selected = _find_bluetooth_device(name)
    for device_name, details in _bluetooth_devices(_bluetooth_data(), include_disconnected=True):
        selected_normalized = re.sub(r"[^a-z0-9]", "", selected.casefold())
        device_normalized = re.sub(r"[^a-z0-9]", "", device_name.casefold())
        if selected_normalized in device_normalized or device_normalized in selected_normalized:
            battery_fields = [
                f"{label.removeprefix('device_batteryLevel')} {value}"
                for label, value in details.items()
                if label.startswith("device_batteryLevel")
            ]
            if not battery_fields:
                return f"{device_name} is connected, but no battery level is available."
            return f"{device_name} battery: " + ", ".join(battery_fields) + "."
    return f"No Bluetooth device matched {name}."


def _change_bluetooth_connection(name: str, action: str) -> str:
    """Connect or disconnect a device using optional blueutil."""
    if platform.system() != "Darwin":
        raise RuntimeError("Bluetooth control is only supported on macOS.")
    if not shutil.which("blueutil"):
        raise RuntimeError("Bluetooth control requires blueutil. Install it with: brew install blueutil")
    device_name = _find_bluetooth_device(name)
    address = next((address for candidate, address, _ in _blueutil_devices() if candidate == device_name), device_name)
    command = ["blueutil", "--connect" if action == "connect" else "--disconnect", address]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"Could not {action} {device_name}.")
    return f"{action.title()}ed {device_name}."


def connect_bluetooth_device(name: str) -> str:
    return _change_bluetooth_connection(name, "connect")


def disconnect_bluetooth_device(name: str) -> str:
    return _change_bluetooth_connection(name, "disconnect")


def media_play_pause() -> str:
    """Send macOS's system-wide play/pause media key."""
    if platform.system() != "Darwin":
        raise RuntimeError("Media control is only supported on macOS.")
    try:
        import Quartz
    except ImportError as error:
        raise RuntimeError("Media control requires the pyobjc Quartz package. Run pip install pyobjc-framework-Quartz.") from error

    event_type_system_defined = 14
    event_data1_field = 0
    nx_key_type_play = 16
    for key_state in (1, 0):
        event = Quartz.CGEventCreate(None)
        Quartz.CGEventSetType(event, event_type_system_defined)
        Quartz.CGEventSetIntegerValueField(
            event,
            event_data1_field,
            (nx_key_type_play << 16) | (key_state << 8),
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    return "Play/pause command sent."


def get_lock_status() -> str:
    """Read the current macOS console lock state from the IORegistry."""
    if platform.system() != "Darwin":
        raise RuntimeError("Lock status is only supported on macOS.")
    result = subprocess.run(
        ["ioreg", "-n", "Root", "-d", "1"],
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout
    if re.search(r"CGSSessionScreenIsLocked\s*=\s*(Yes|1|true)", output, re.IGNORECASE):
        return "The Mac is locked."
    if "kCGSessionLoginDoneKey" in output and "kCGSSessionOnConsoleKey" in output:
        return "The Mac is unlocked."
    return "The Mac lock state is unavailable."


def lock_device() -> str:
    """Lock the Mac. Prefers methods that need no Accessibility permission."""
    if platform.system() != "Darwin":
        raise RuntimeError("Locking the device is only supported on macOS.")

    # 1. CGSession -suspend: a true lock, no Accessibility needed (older macOS).
    cg_session = "/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession"
    if os.path.exists(cg_session):
        result = subprocess.run([cg_session, "-suspend"], capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return "Locked the Mac."

    # 2. Sleep the display: no permission required. Locks immediately when
    #    "Require password after sleep/screen saver" is set to immediately.
    result = subprocess.run(["pmset", "displaysleepnow"], capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return (
            "Locked the Mac (display asleep). If it doesn't ask for a password on wake, "
            "set System Settings > Lock Screen > 'Require password after…' to Immediately."
        )

    # 3. Last resort: the Ctrl-Cmd-Q keystroke, which needs Accessibility access.
    result = subprocess.run(
        ["osascript", "-e", 'tell application "System Events" to keystroke "q" using {control down, command down}'],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Could not lock the Mac. Grant Accessibility access to the app that runs AVIS "
            "(System Settings > Privacy & Security > Accessibility), or set a password-on-sleep so display sleep locks."
        )
    return "Lock command sent to the Mac."


def set_volume(level: int) -> str:
    if platform.system() != "Darwin":
        raise RuntimeError("set_volume is only supported on macOS.")
    if not isinstance(level, int) or isinstance(level, bool) or not 0 <= level <= 100:
        raise ValueError("Volume must be an integer from 0 to 100.")
    result = subprocess.run(
        ["osascript", "-e", f"set volume output volume {level}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Could not set the volume.")
    return f"Volume set to {level}%."


def _apple_script_string(value: str) -> str:
    """Quote user text for AppleScript without allowing script injection."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def make_reminder(title: str, due_date: str | None = None, notes: str = "") -> str:
    """Create a reminder, optionally with an ISO date such as 2026-08-28."""
    if not title.strip():
        raise ValueError("Reminder title cannot be empty.")
    properties = f"name:{_apple_script_string(title.strip())}"
    if notes.strip():
        properties += f", body:{_apple_script_string(notes.strip())}"
    script = f"tell application \"Reminders\" to make new reminder with properties {{{properties}}}"
    if due_date:
        script += f"\n{_date_setup('dueDate', due_date)}\ntell application \"Reminders\" to set due date of result to dueDate"
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Could not create the reminder.")
    return f"Created reminder: {title.strip()}."


def _parse_date(value: str) -> str:
    from datetime import datetime

    for format_string in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value.strip(), format_string)
            return parsed.strftime("%B %-d, %Y %H:%M")
        except ValueError:
            continue
    raise ValueError("Date must use YYYY-MM-DD or YYYY-MM-DD HH:MM format.")


def _date_setup(variable: str, value: str) -> str:
    """Build locale-independent AppleScript date initialization."""
    from datetime import datetime

    for format_string in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value.strip(), format_string)
            month_name = parsed.strftime("%B")
            return (
                f"set {variable} to current date\n"
                f"set year of {variable} to {parsed.year}\n"
                f"set month of {variable} to {month_name}\n"
                f"set day of {variable} to {parsed.day}\n"
                f"set time of {variable} to {parsed.hour * 3600 + parsed.minute * 60}"
            )
        except ValueError:
            continue
    raise ValueError("Date must use YYYY-MM-DD or YYYY-MM-DD HH:MM format.")


def add_event_in_calendar(
    title: str,
    start: str,
    end: str | None = None,
    calendar: str = "Calendar",
) -> str:
    """Add an event using ISO date/time strings to a macOS calendar."""
    if not title.strip():
        raise ValueError("Event title cannot be empty.")
    _date_setup("startDate", start)
    if end:
        _date_setup("endDate", end)
    script = (
        f"tell application \"Calendar\" to tell calendar {_apple_script_string(calendar)} "
        f"to make new event with properties {{summary:{_apple_script_string(title.strip())}, "
        "start date:startDate"
    )
    if end:
        script += ", end date:endDate"
    script += "}"
    date_setup = _date_setup("startDate", start)
    if end:
        date_setup += "\n" + _date_setup("endDate", end)
    script = date_setup + "\n" + script
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Could not add the calendar event.")
    return f"Added calendar event: {title.strip()}."


def read_calendar_for_date(date: str) -> str:
    """Read calendar events for a YYYY-MM-DD date."""
    script = (
        f"{_date_setup('targetDate', date)}\n"
        "set nextDate to targetDate + (1 * days)\n"
        "tell application \"Calendar\"\n"
        "set output to \"\"\n"
        "repeat with currentCalendar in calendars\n"
        "repeat with currentEvent in (every event of currentCalendar whose start date is greater than or equal to targetDate and start date is less than nextDate)\n"
        "set output to output & (summary of currentEvent) & \" | \" & (start date of currentEvent as text) & return\n"
        "end repeat\nend repeat\nend tell\nreturn output"
    )
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Could not read the calendar.")
    return result.stdout.strip() or f"No calendar events found for {date}."


TOOL_FUNCTIONS = {
    "open_app": open_app,
    "close_app": close_app,
    "open_file": open_file,
    "search_files": search_files,
    "get_clipboard": get_clipboard,
    "list_notifications": list_notifications,
    "get_time_date": get_time_date,
    "get_battery_status": get_battery_status,
    "get_bluetooth_devices": get_bluetooth_devices,
    "get_bluetooth_battery": get_bluetooth_battery,
    "connect_bluetooth_device": connect_bluetooth_device,
    "disconnect_bluetooth_device": disconnect_bluetooth_device,
    "media_play_pause": media_play_pause,
    "get_lock_status": get_lock_status,
    "lock_device": lock_device,
    "set_volume": set_volume,
    "make_reminder": make_reminder,
    "add_event_in_calendar": add_event_in_calendar,
    "read_calendar_for_date": read_calendar_for_date,
    "run_mac_diagnostics": run_mac_diagnostics,
    "remember_long_term": remember_long_term,
    "web_search": web_search,
    "web_fetch": web_fetch,
}

TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "open_app", "description": "Open an allowlisted macOS app.", "parameters": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "close_app", "description": "Close an allowlisted macOS app.", "parameters": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "open_file", "description": "Read a UTF-8 text file inside the AVIS project directory.", "parameters": {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "search_files", "description": "Search text inside the AVIS project directory.", "parameters": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}, "path": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "get_clipboard", "description": "Read the current macOS clipboard text.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "list_notifications", "description": "List visible macOS notifications.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_time_date", "description": "Read the local date and time.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_battery_status", "description": "Read the Mac battery status.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_bluetooth_devices", "description": "List connected Bluetooth devices.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_bluetooth_battery", "description": "Read the battery level of a connected Bluetooth device.", "parameters": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "connect_bluetooth_device", "description": "Connect an available Bluetooth device after approval.", "parameters": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "disconnect_bluetooth_device", "description": "Disconnect a connected Bluetooth device after approval.", "parameters": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "media_play_pause", "description": "Toggle system media play/pause after approval.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_lock_status", "description": "Read whether the Mac is locked.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "lock_device", "description": "Lock the Mac after explicit user approval.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "set_volume", "description": "Set macOS output volume from 0 to 100.", "parameters": {"type": "object", "required": ["level"], "properties": {"level": {"type": "integer", "minimum": 0, "maximum": 100}}}}},
    {"type": "function", "function": {"name": "make_reminder", "description": "Create a macOS reminder. Requires approval.", "parameters": {"type": "object", "required": ["title"], "properties": {"title": {"type": "string"}, "due_date": {"type": "string", "description": "Optional YYYY-MM-DD or YYYY-MM-DD HH:MM."}, "notes": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "add_event_in_calendar", "description": "Add a macOS Calendar event. Requires approval.", "parameters": {"type": "object", "required": ["title", "start"], "properties": {"title": {"type": "string"}, "start": {"type": "string", "description": "YYYY-MM-DD HH:MM."}, "end": {"type": "string", "description": "Optional YYYY-MM-DD HH:MM."}, "calendar": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "read_calendar_for_date", "description": "Read macOS Calendar events for a date. Use YYYY-MM-DD.", "parameters": {"type": "object", "required": ["date"], "properties": {"date": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "run_mac_diagnostics", "description": "Run read-only Mac hardware, usage, performance, and security diagnostics after approval. Does not perform a malware scan.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "remember_long_term", "description": "Store a user-approved long-term note, optionally until YYYY-MM-DD.", "parameters": {"type": "object", "required": ["note"], "properties": {"note": {"type": "string"}, "until": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "web_search", "description": "Search the internet for current or factual information and return the top results as titles, URLs, and snippets. Use for anything about live data, recent events, or things not on this Mac.", "parameters": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 10}}}}},
    {"type": "function", "function": {"name": "web_fetch", "description": "Fetch a public http/https web page and return its readable text. Use it to open a result returned by web_search when you need the details on the page.", "parameters": {"type": "object", "required": ["url"], "properties": {"url": {"type": "string"}}}}},
]
