"""User-controlled constant, long-term, and short-term AVIS context."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
CONSTANT_PATH = Path(os.getenv("AVIS_CONSTANT_CONTEXT", str(PROJECT_ROOT / "constant_context.json")))
LONG_TERM_PATH = Path(os.getenv("AVIS_LONG_TERM_CONTEXT", str(PROJECT_ROOT / "long_term_context.json")))

DEFAULT_CONSTANT = {
    "user": "",
    "assistant": "AVIS, a local assistant running on this Mac.",
    "working_on": "A.V.I.S. local assistant project.",
    "helps_with": ["OS checks", "safe Mac actions", "local automation", "voice interaction"],
}


def _load(path: Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as context_file:
            value = json.load(context_file)
        return value
    except (OSError, json.JSONDecodeError):
        return default.copy() if isinstance(default, dict) else list(default)


def load_constant() -> dict[str, Any]:
    return _load(CONSTANT_PATH, DEFAULT_CONSTANT)


def load_long_term() -> list[dict[str, Any]]:
    value = _load(LONG_TERM_PATH, [])
    return value if isinstance(value, list) else []


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as context_file:
            json.dump(value, context_file, indent=2, ensure_ascii=True)
            context_file.write("\n")
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def save_constant(value: dict[str, Any]) -> None:
    _write(CONSTANT_PATH, value)


def add_long_term(note: str, until: str | None = None) -> None:
    notes = load_long_term()
    notes.append({"note": note.strip(), "until": until, "created": date.today().isoformat()})
    _write(LONG_TERM_PATH, notes)


def active_long_term() -> list[dict[str, Any]]:
    today = date.today()
    active: list[dict[str, Any]] = []
    for item in load_long_term():
        until = item.get("until")
        if not until:
            active.append(item)
            continue
        try:
            if date.fromisoformat(until) >= today:
                active.append(item)
        except (TypeError, ValueError):
            active.append(item)
    return active


def system_context(short_term: list[dict[str, Any]]) -> str:
    constant = load_constant()
    long_term = active_long_term()
    return (
        "Constant context (user-controlled): " + json.dumps(constant, ensure_ascii=True) + "\n"
        "Long-term context (user-controlled, do not modify unless explicitly asked): "
        + json.dumps(long_term, ensure_ascii=True)
        + "\nShort-term context: "
        + json.dumps(short_term[-12:], ensure_ascii=True)
    )
