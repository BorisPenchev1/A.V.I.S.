"""Ollama client, bounded conversation memory, and tool orchestration."""

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from context.avis_context import system_context
from security.security import execute_tool
from main.tools import TOOLS, TOOL_FUNCTIONS, _resolve_allowed_app


OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
MODEL_NAME = os.getenv("QWEN_MODEL", "qwen3:8b")
MAX_MESSAGES = 20
MAX_PREDICT = int(os.getenv("AVIS_MAX_PREDICT", "1024"))


def _normalize_prompt(prompt: str) -> str:
    return re.sub(r"\s+", " ", prompt.strip().casefold())


def _query_mode(prompt: str) -> str:
    """Choose the cheapest response path that can satisfy the request."""
    if re.search(r"\b(connect|disconnect|pause|play|toggle)\b", prompt, re.IGNORECASE):
        return "action"
    if re.search(
        r"\b(time|date|battery|bluetooth|locked|lock\s+status|notifications?|diagnostics?|hardware|performance)\b",
        prompt,
        re.IGNORECASE,
    ):
        return "check"
    if re.search(
        r"\b(check|read|show|get|search|find|look)\b.*\b(clipboard|calendar|file|files|schedule|battery|bluetooth|time|date|lock)\b",
        prompt,
        re.IGNORECASE,
    ):
        return "check"
    if re.search(
        r"\b(open|close|set|make|create|add|remind|launch|volume|lock|connect|disconnect|pause|play)\b",
        prompt,
        re.IGNORECASE,
    ):
        return "action"
    return "chat"


@dataclass
class AssistantState:
    """Small facts that are more useful than replaying the whole transcript."""

    last_app: str | None = None
    last_tool: str | None = None
    last_result: str | None = None
    last_path: str | None = None
    last_query: str | None = None
    turn_count: int = 0

    def record(self, tool_name: str, result: str, arguments: dict[str, Any]) -> None:
        self.last_tool = tool_name
        self.last_result = result[-500:]
        self.turn_count += 1
        if tool_name in {"open_app", "close_app"}:
            self.last_app = arguments.get("name")
        if tool_name == "open_file":
            self.last_path = arguments.get("path")
        if tool_name == "search_files":
            self.last_query = arguments.get("query")


class ConversationMemory:
    """Keep a bounded session history so prompts retain recent context."""

    def __init__(self, max_messages: int = MAX_MESSAGES) -> None:
        self.messages: list[dict[str, Any]] = []
        self.max_messages = max_messages
        self.state = AssistantState()

    def add(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        self.messages = self.messages[-self.max_messages :]

    def clear(self) -> None:
        self.messages.clear()
        self.state = AssistantState()


def _context_message(
    state: AssistantState,
    short_term: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Give Qwen only compact, actionable state instead of a long preamble."""
    values = [
        f"last_app={state.last_app or 'none'}",
        f"last_tool={state.last_tool or 'none'}",
        f"last_path={state.last_path or 'none'}",
        f"last_query={state.last_query or 'none'}",
        f"last_result={state.last_result or 'none'}",
    ]
    return {
        "role": "system",
        "content": (
            "You are AVIS, a local assistant. Use a tool when one directly matches "
            "the request. Never claim a tool ran unless its result says it did. "
            f"Today is {date.today().isoformat()}. Resolve relative dates using today. "
            "Current state: " + "; ".join(values) + "\n" + system_context(short_term or [])
        ),
    }


def _chat(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": MODEL_NAME,
        "messages": messages,
        "stream": False,
        "keep_alive": -1,
        "think": False,
        "options": {"temperature": 0, "num_predict": MAX_PREDICT},
    }
    if tools:
        payload["tools"] = tools
    request = Request(
        f"{OLLAMA_URL.rstrip('/')}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=120) as response:
            return json.load(response)
    except HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama returned HTTP {error.code}: {details}") from error
    except URLError as error:
        raise RuntimeError(f"Could not connect to Ollama at {OLLAMA_URL}. Start Ollama and try again.") from error


def _stream_chat(messages: list[dict[str, Any]]) -> Iterator[str]:
    """Yield response text as Ollama generates it."""
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "stream": True,
        "keep_alive": -1,
        "think": False,
        "options": {"temperature": 0, "num_predict": MAX_PREDICT},
    }
    request = Request(
        f"{OLLAMA_URL.rstrip('/')}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=120) as response:
            for line in response:
                if not line.strip():
                    continue
                chunk = json.loads(line)
                text = chunk.get("message", {}).get("content", "")
                if text:
                    yield text
                if chunk.get("done") and chunk.get("done_reason") == "length":
                    yield "\n[Response limit reached. Ask me to continue for the rest.]"
    except HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama returned HTTP {error.code}: {details}") from error
    except URLError as error:
        raise RuntimeError(f"Could not connect to Ollama at {OLLAMA_URL}. Start Ollama and try again.") from error


def ask_qwen(prompt: str) -> str:
    result = _chat([{"role": "user", "content": prompt}])
    return result.get("message", {}).get("content", "")


def _verify_tool_result(
    tool_name: str,
    arguments: dict[str, Any],
    tool_result: str,
    state: AssistantState,
) -> str:
    """Ask Qwen to interpret only outcomes that are hard to verify locally."""
    verification_prompt = (
        "Assess whether this tool operation should be reported as successful. "
        "Use only the supplied tool result; do not assume or invent facts. "
        "If it reports an error, report failure. Reply briefly.\n"
        f"Tool: {tool_name}\nArguments: {json.dumps(arguments, default=str)}\n"
        f"Wrapper result: {tool_result}"
    )
    result = _chat(
        [_context_message(state), {"role": "user", "content": verification_prompt}],
    )
    return result.get("message", {}).get("content", "") or tool_result


def _analyze_diagnostics(report: str, state: AssistantState) -> str:
    """Interpret a read-only diagnostics report without taking any action."""
    prompt = (
        "Analyze this read-only Mac diagnostics report. Give a brief, practical assessment. "
        "Identify measured usage levels, possible performance concerns, overheating evidence "
        "if present, storage or memory pressure, and anything that needs attention. "
        "Do not invent temperatures, malware findings, or measurements that are absent. "
        "State clearly when the report cannot determine something. Do not recommend or execute "
        "system changes.\n\nDiagnostics report:\n" + report
    )
    result = _chat(
        [_context_message(state), {"role": "user", "content": prompt}],
    )
    analysis = result.get("message", {}).get("content", "").strip()
    return analysis or "Qwen could not analyze the diagnostics report. The raw report is shown above."


def _direct_command(
    prompt: str,
    request_permission: Callable[[str, dict[str, Any]], bool] | None,
    state: AssistantState,
) -> str | None:
    prompt = _normalize_prompt(prompt)
    patterns = [
        ("open_file", r"\s*(?:open|read)\s+file\s+(.+?)\s*[.!]?\s*", "path"),
        ("search_files", r"\s*search\s+files?\s+for\s+(.+?)\s*[.!]?\s*", "query"),
    ]
    for tool_name, pattern, argument_name in patterns:
        match = re.fullmatch(pattern, prompt, re.IGNORECASE)
        if match:
            try:
                arguments = {argument_name: match.group(1)}
                result = execute_tool(tool_name, arguments, TOOL_FUNCTIONS, request_permission)
                state.record(tool_name, result, arguments)
                return result
            except (PermissionError, RuntimeError, ValueError, OSError) as error:
                return f"Error: {error}"
    instant_checks = [
        ("get_time_date", r"\s*(?:what(?:'s| is)\s+(?:the\s+)?(?:current\s+)?(?:time|date|day)|what\s+time\s+is\s+it|today(?:'s)?\s+date|current\s+(?:time|date)|(?:the\s+)?date)\s*[.!]?\s*"),
        ("get_battery_status", r"\s*(?:(?:what(?:'s| is)\s+)?(?:(?:the|my)\s+)?|(?:show|check|get)\s+)battery(?:\s+status)?\s*[.!]?\s*"),
        ("get_bluetooth_devices", r"\s*(?:(?:which|what)\s+bluetooth\s+devices\s+(?:are\s+)?connected|(?:show|list|get|check)\s+(?:the\s+)?connected\s+bluetooth\s+devices|(?:show|list|get|check)\s+bluetooth)\s*[.!]?\s*"),
        ("get_lock_status", r"\s*(?:(?:am\s+i|is\s+(?:the\s+)?device)\s+(?:locked|unlocked)|what(?:'s| is)\s+the\s+lock\s+status)\s*[.!]?\s*"),
        ("list_notifications", r"\s*(?:read|list|show|get|check)\s+(?:my\s+|the\s+)?notifications?\s*[.!]?\s*"),
        ("run_mac_diagnostics", r"\s*(?:run|start|perform)\s+(?:a\s+)?(?:mac|computer|system)?\s*(?:diagnostics?|health\s+check|performance\s+check)\s*[.!]?\s*"),
    ]
    for tool_name, pattern in instant_checks:
        if re.fullmatch(pattern, prompt, re.IGNORECASE):
            try:
                return _run_direct_tool(tool_name, {}, request_permission, state)
            except (PermissionError, RuntimeError, ValueError, OSError) as error:
                return f"Error: {error}"
    if re.fullmatch(
        r"\s*(?:what(?:'s| is)\s+)?(?:the\s+)?(?:battery|battery\s+status)\s+(?:of|for)\s+(?:this\s+device|my\s+(?:mac|computer|device)|the\s+mac)\s*[.!]?\s*",
        prompt,
        re.IGNORECASE,
    ):
        try:
            return _run_direct_tool("get_battery_status", {}, request_permission, state)
        except (PermissionError, RuntimeError, ValueError, OSError) as error:
            return f"Error: {error}"
    bluetooth_battery_match = re.fullmatch(
        r"\s*(?:what(?:'s| is)\s+)?(?:the\s+)?battery(?:\s+level)?\s+(?:of|for)\s+(?:my\s+)?(.+?)\s*[.!]?\s*",
        prompt,
        re.IGNORECASE,
    )
    if bluetooth_battery_match:
        try:
            return _run_direct_tool(
                "get_bluetooth_battery",
                {"name": bluetooth_battery_match.group(1)},
                request_permission,
                state,
            )
        except (PermissionError, RuntimeError, ValueError, OSError) as error:
            return f"Error: {error}"
    bluetooth_change = re.fullmatch(
        r"\s*(connect|disconnect)\s+(?:to\s+|from\s+)?(?:the\s+)?bluetooth\s+(?:device\s+)?(.+?)\s*[.!]?\s*",
        prompt,
        re.IGNORECASE,
    )
    if bluetooth_change:
        tool_name = f"{bluetooth_change.group(1).lower()}_bluetooth_device"
        try:
            return _run_direct_tool(
                tool_name,
                {"name": bluetooth_change.group(2)},
                request_permission,
                state,
            )
        except (PermissionError, RuntimeError, ValueError, OSError) as error:
            return f"Error: {error}"
    if re.fullmatch(r"\s*(?:pause|play|play/pause|toggle)\s*(?:music|audio|video|media)?\s*[.!]?\s*", prompt, re.IGNORECASE):
        try:
            return _run_direct_tool("media_play_pause", {}, request_permission, state)
        except (PermissionError, RuntimeError, ValueError, OSError) as error:
            return f"Error: {error}"
    if re.fullmatch(
        r"\s*(?:lock|lock\s+my|lock\s+the)\s+(?:device|mac|computer)\s*[.!]?\s*",
        prompt,
        re.IGNORECASE,
    ):
        try:
            return _run_direct_tool("lock_device", {}, request_permission, state)
        except (PermissionError, RuntimeError, ValueError, OSError) as error:
            return f"Error: {error}"
    if re.fullmatch(r"\s*(?:get|read|show)\s+clipboard\s*[.!]?\s*", prompt, re.IGNORECASE):
        try:
            result = execute_tool("get_clipboard", {}, TOOL_FUNCTIONS, request_permission)
            state.record("get_clipboard", result, {})
            return result
        except (PermissionError, RuntimeError, ValueError, OSError) as error:
            return f"Error: {error}"
    calendar_match = re.fullmatch(
        r"\s*(?:read|show|check|what(?:'s| is)\s+on)\s+(?:my\s+)?calendar(?:\s+for)?\s+(today|tomorrow|\d{4}-\d{2}-\d{2})\s*[.!]?\s*",
        prompt,
        re.IGNORECASE,
    )
    if calendar_match:
        requested_date = calendar_match.group(1).casefold()
        if requested_date == "today":
            requested_date = date.today().isoformat()
        elif requested_date == "tomorrow":
            requested_date = (date.today() + timedelta(days=1)).isoformat()
        try:
            return _run_direct_tool(
                "read_calendar_for_date",
                {"date": requested_date},
                request_permission,
                state,
            )
        except (PermissionError, RuntimeError, ValueError, OSError) as error:
            return f"Error: {error}"
    volume_match = re.fullmatch(
        r"\s*(?:set\s+)?volume\s+(?:to\s+)?(\d{1,3})\s*%?\s*[.!]?\s*",
        prompt,
        re.IGNORECASE,
    )
    if volume_match:
        arguments = {"level": int(volume_match.group(1))}
        try:
            return _run_direct_tool("set_volume", arguments, request_permission, state)
        except (PermissionError, RuntimeError, ValueError, OSError) as error:
            return f"Error: {error}"
    close_match = re.fullmatch(r"\s*close\s+(.+?)\s*[.!]?\s*", prompt, re.IGNORECASE)
    if close_match and _resolve_allowed_app(close_match.group(1)) is not None:
        try:
            arguments = {"name": close_match.group(1)}
            return _run_direct_tool("close_app", arguments, request_permission, state)
        except (PermissionError, RuntimeError, ValueError, OSError) as error:
            return f"Error: {error}"
    open_match = re.fullmatch(r"\s*open\s+(.+?)\s*[.!]?\s*", prompt, re.IGNORECASE)
    if open_match and _resolve_allowed_app(open_match.group(1)) is not None:
        try:
            arguments = {"name": open_match.group(1)}
            return _run_direct_tool("open_app", arguments, request_permission, state)
        except (PermissionError, RuntimeError, ValueError, OSError) as error:
            return f"Error: {error}"
    return None


def _run_direct_tool(
    tool_name: str,
    arguments: dict[str, Any],
    request_permission: Callable[[str, dict[str, Any]], bool] | None,
    state: AssistantState,
) -> str:
    result = execute_tool(tool_name, arguments, TOOL_FUNCTIONS, request_permission)
    if not result or not result.strip():
        result = f"{tool_name} completed, but returned no details."
    if tool_name == "run_mac_diagnostics":
        result += "\n\nQwen analysis:\n" + _analyze_diagnostics(result, state)
    state.record(tool_name, result, arguments)
    return result


MAX_TOOL_STEPS = 5


def _replay(text: str, on_token: Callable[[str], None]) -> None:
    """Reveal an already-composed answer gradually, word by word."""
    for token in re.findall(r"\S+\s*", text):
        on_token(token)
        time.sleep(0.02)


def _execute_tool_call(
    call: dict[str, Any],
    request_permission: Callable[[str, dict[str, Any]], bool] | None,
    memory: ConversationMemory,
) -> None:
    """Run one Qwen-requested tool call and record its result in memory."""
    function = call.get("function", {})
    tool_name = function.get("name")
    arguments = function.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as error:
            arguments = {}
            memory.add({"role": "tool", "content": f"Tool error: invalid arguments: {error}", "tool_name": tool_name or "unknown"})
            return
    if not isinstance(arguments, dict):
        arguments = {}
    if tool_name not in TOOL_FUNCTIONS:
        memory.add({"role": "tool", "content": f"Tool error: unknown tool {tool_name!r}.", "tool_name": str(tool_name)})
        return
    try:
        tool_result = execute_tool(tool_name, arguments, TOOL_FUNCTIONS, request_permission)
    except (KeyError, TypeError, ValueError, PermissionError, RuntimeError, OSError) as error:
        tool_result = f"Tool error: {error}"
    tool_result = tool_result or f"{tool_name} completed, but returned no details."
    memory.add({"role": "tool", "content": tool_result, "tool_name": tool_name})
    memory.state.record(tool_name, tool_result, arguments)


def run_assistant(
    prompt: str,
    request_permission: Callable[[str, dict[str, Any]], bool] | None = None,
    memory: ConversationMemory | None = None,
    on_token: Callable[[str], None] | None = None,
    force_agent: bool = False,
) -> str:
    """Run one turn as an agent: chain tools as needed, then compose an answer.

    Qwen drives the logic. It may call tools repeatedly; their results are fed
    back to it until it produces a final natural-language answer. Set
    ``force_agent`` to guarantee tool access even for conversational-looking
    instructions (used by custom tools and automations).
    """
    memory = memory or ConversationMemory()
    mode = _query_mode(prompt)

    if not force_agent:
        direct_result = _direct_command(prompt, request_permission, memory.state)
        if direct_result is not None:
            memory.add({"role": "user", "content": prompt})
            memory.add({"role": "assistant", "content": direct_result})
            return direct_result

    memory.add({"role": "user", "content": prompt})

    # Purely conversational turns stream directly for a fast first token.
    if on_token is not None and mode == "chat" and not force_agent:
        messages = [_context_message(memory.state, memory.messages), *memory.messages]
        response_parts: list[str] = []
        for part in _stream_chat(messages):
            response_parts.append(part)
            on_token(part)
        response = "".join(response_parts).strip() or "I could not produce a response for that request."
        memory.add({"role": "assistant", "content": response})
        return response

    # Agentic loop: let Qwen call tools until it is ready to answer.
    final_message: dict[str, Any] | None = None
    for _ in range(MAX_TOOL_STEPS):
        messages = [_context_message(memory.state, memory.messages), *memory.messages]
        result = _chat(messages, TOOLS)
        message = result.get("message", {})
        tool_calls = message.get("tool_calls", [])
        if not tool_calls:
            final_message = message
            break
        memory.add(message)
        for call in tool_calls:
            _execute_tool_call(call, request_permission, memory)

    if final_message is None:
        # Hit the step cap; ask Qwen to summarize the tool results it gathered.
        messages = [
            _context_message(memory.state, memory.messages),
            *memory.messages,
            {"role": "user", "content": "Give me a clear, final answer based on the tool results above."},
        ]
        final_message = _chat(messages).get("message", {})

    response = (final_message.get("content") or "").strip()
    if not response:
        response = "I ran the requested tools but could not compose a summary."
    memory.add({"role": "assistant", "content": response})
    if on_token is not None:
        _replay(response, on_token)
    return response
