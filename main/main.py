"""Command-line interface for the local AVIS assistant."""

from typing import Any
import threading

from main.model import ConversationMemory, run_assistant
from voice.hotkeys import start_hotkeys
from voice.voice import SpeechQueue, interrupt, listen


def request_permission(tool_name: str, arguments: dict[str, Any]) -> bool:
    details = ", ".join(f"{key}={value!r}" for key, value in arguments.items())
    answer = input(f"Permission required for {tool_name} ({details}). Allow? [y/N] ")
    return answer.strip().casefold() in {"y", "yes"}


def main() -> None:
    memory = ConversationMemory()
    assistant_lock = threading.Lock()

    def process_prompt(prompt: str) -> None:
        if not prompt:
            return
        with assistant_lock:
            speech_queue = SpeechQueue(voice="Daniel")
            speech_queue.start()
            try:
                streamed = False

                def show_token(token: str) -> None:
                    nonlocal streamed
                    if not streamed:
                        print("Qwen: ", end="", flush=True)
                        streamed = True
                    print(token, end="", flush=True)
                    speech_queue.feed(token)

                response = run_assistant(prompt, request_permission, memory, show_token)
            except (RuntimeError, PermissionError, ValueError, OSError) as error:
                print(f"Error: {error}")
                speech_queue.close()
                return
            if streamed:
                print()
            else:
                print(f"Qwen: {response}")
                speech_queue.feed(response)
            threading.Thread(
                target=lambda: (speech_queue.finish(), speech_queue.close()),
                daemon=True,
            ).start()

    def push_to_talk() -> None:
        print("\nListening...", flush=True)
        try:
            process_prompt(listen())
        except (RuntimeError, OSError) as error:
            print(f"Voice error: {error}")

    def stop_current_response() -> None:
        interrupt()
        print("\nInterrupted.", flush=True)

    hotkey_listener = start_hotkeys(push_to_talk, stop_current_response)
    hotkey_status = "enabled" if hotkey_listener is not None else "unavailable"
    print(
        "AVIS is ready. Type 'exit', 'quit', 'clear', or '/voice' to talk. "
        f"Hotkeys: Shift+Space, T,T to talk; Shift+Space, I to interrupt ({hotkey_status})."
    )
    while True:
        try:
            prompt = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if prompt.lower() in {"exit", "quit"}:
            break
        if prompt.lower() == "clear":
            memory.clear()
            print("Memory cleared.")
            continue
        if prompt == "/voice":
            print("Listening...", flush=True)
            try:
                prompt = listen()
                print(f"You said: {prompt!r}")
            except (RuntimeError, OSError) as error:
                print(f"Voice error: {error}")
                continue
        process_prompt(prompt)


if __name__ == "__main__":
    main()
