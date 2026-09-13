"""Local global hotkeys for push-to-talk and speech interruption."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


def start_hotkeys(
    on_push_to_talk: Callable[[], None],
    on_interrupt: Callable[[], None],
) -> object | None:
    """Start Shift+Space followed by TT or I, when macOS allows key monitoring."""
    try:
        from pynput import keyboard
    except ImportError:
        return None

    pressed: set[object] = set()
    prefix_started = 0.0
    sequence = ""
    prefix_active = False
    callback_lock = threading.Lock()

    def key_name(key: object) -> str:
        if isinstance(key, keyboard.KeyCode) and key.char:
            return key.char.casefold()
        return ""

    def on_press(key: object) -> None:
        nonlocal prefix_started, sequence, prefix_active
        pressed.add(key)
        if (
            keyboard.Key.shift in pressed
            and keyboard.Key.space in pressed
            and key in {keyboard.Key.shift, keyboard.Key.space}
        ):
            prefix_started = time.monotonic()
            sequence = ""
            prefix_active = True
            return
        if prefix_active and time.monotonic() - prefix_started <= 1.5:
            character = key_name(key)
            if character in {"t", "i"}:
                sequence += character
                if character == "i":
                    prefix_started = 0.0
                    prefix_active = False
                    sequence = ""
                    with callback_lock:
                        on_interrupt()
                elif sequence == "tt":
                    prefix_started = 0.0
                    prefix_active = False
                    sequence = ""
                    with callback_lock:
                        on_push_to_talk()
        elif prefix_active:
            prefix_started = 0.0
            prefix_active = False
            sequence = ""

    def on_release(key: object) -> None:
        pressed.discard(key)

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.daemon = True
    listener.start()
    return listener
