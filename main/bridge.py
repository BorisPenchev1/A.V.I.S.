"""JSON bridge between the AVIS macOS app and the Python backend.

The app spawns this script with a single JSON argument describing the turn and
reads a stream of JSON-lines from stdout:

    {"t": "tok",   "x": "<partial text>"}   # streamed token (chat replies)
    {"t": "done"}                             # streaming finished, text already sent
    {"t": "final", "x": "<full text>"}       # non-streamed result (tool/action turns)
    {"t": "err",   "x": "<message>"}          # something went wrong

Request JSON (argv[1]):
    {
      "prompt": "user text",
      "history": [{"role": "user"|"assistant", "text": "..."}],  # prior turns in this chat
      "grants": ["open apps", "lock the device"],   # in-chat permission phrases
      "grant_all": false                             # blanket permission for this chat
    }
"""

import json
import re
import sys
from typing import Any, Callable

from context.avis_context import add_long_term
from main.model import ConversationMemory, run_assistant


# Distinctive keywords that let a free-text permission grant match a tool.
TOOL_KEYWORDS: dict[str, tuple[str, ...]] = {
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

_BLANKET = {"everything", "anything", "all", "do anything", "whatever", "all actions", "any action"}


def _emit(kind: str, text: str | None = None) -> None:
    record: dict[str, Any] = {"t": kind}
    if text is not None:
        record["x"] = text
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


def _make_permission(grants: list[str], grant_all: bool) -> Callable[[str, dict[str, Any]], bool]:
    phrases = " ; ".join(g.strip().casefold() for g in grants if g.strip())
    blanket = grant_all or any(word in phrases for word in _BLANKET)

    def request_permission(name: str, arguments: dict[str, Any]) -> bool:
        if blanket:
            return True
        if not phrases:
            return False
        if name.replace("_", " ") in phrases:
            return True
        for keyword in TOOL_KEYWORDS.get(name, ()):  # distinctive per-tool keywords
            if keyword in phrases:
                return True
        # Allow when the grant names a specific target the tool is acting on.
        for value in arguments.values():
            if isinstance(value, str) and value.strip() and value.strip().casefold() in phrases:
                return True
        return False

    return request_permission


def _handle_remember(prompt: str) -> str | None:
    """Explicit cross-chat memory: 'remember (that) ...' persists a long-term note."""
    match = re.fullmatch(r"\s*remember(?:\s+that)?[:,]?\s+(.+?)\s*[.!]?\s*", prompt, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    note = match.group(1).strip()
    if not note:
        return None
    add_long_term(note)
    return f"Got it — I'll remember that: {note}"


def main() -> None:
    try:
        request = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    except (json.JSONDecodeError, IndexError):
        _emit("err", "The AVIS app sent a malformed request.")
        return

    prompt = str(request.get("prompt", "")).strip()
    if not prompt:
        _emit("err", "No prompt was provided.")
        return

    remembered = _handle_remember(prompt)
    if remembered is not None:
        _emit("final", remembered)
        return

    memory = ConversationMemory()
    for item in request.get("history", []):
        role = item.get("role")
        text = item.get("text")
        if role in {"user", "assistant"} and isinstance(text, str) and text:
            memory.add({"role": role, "content": text})

    request_permission = _make_permission(
        request.get("grants", []) or [],
        bool(request.get("grant_all", False)),
    )

    streamed = False

    def on_token(token: str) -> None:
        nonlocal streamed
        streamed = True
        _emit("tok", token)

    try:
        response = run_assistant(
            prompt,
            request_permission,
            memory,
            on_token,
            force_agent=bool(request.get("agent", False)),
        )
    except PermissionError as error:
        _emit("final", f"I need permission for that. {error} Tell me \"you have permission to …\" and I'll proceed.")
        return
    except (RuntimeError, ValueError, OSError) as error:
        _emit("err", str(error))
        return

    if streamed:
        _emit("done")
    else:
        if "Permission was not granted" in response or "is not permitted" in response:
            response += "\n\nTell me \"you have permission to …\" and I'll do it for this chat."
        _emit("final", response)


if __name__ == "__main__":
    main()
