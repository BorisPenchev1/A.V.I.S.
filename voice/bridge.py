"""Voice bridge between the AVIS macOS app and the Python speech stack.

The app spawns this module and reads a stream of JSON-lines from stdout:

    {"t": "listening"}            # the microphone stream is open, capturing now
    {"t": "text", "x": "<said>"}  # transcription of what the user spoke
    {"t": "silence"}              # nothing intelligible was captured
    {"t": "spoke"}                # speech playback finished
    {"t": "error", "x": "<msg>"}  # something went wrong

Subcommands (argv[1]):

    listen-ptt   Push-to-talk. Records until the app writes a line to stdin
                 (the "Send" press) or stdin closes, then transcribes.
    converse     Conversation turn. Records with silence detection and stops on
                 its own once the speaker finishes, then transcribes. One turn
                 per invocation; the app relaunches it after AVIS finishes
                 speaking, giving natural back-and-forth.
    speak        Reads the text to speak from stdin and plays it with
                 ElevenLabs. Terminating the process interrupts playback.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

from voice.voice import (
    StreamingSpeaker,
    interrupt,
    select_input_device,
    speak,
    transcribe_audio,
    write_wav_16k,
)


# Set by SIGTERM/SIGINT (the app terminating us) and by the stdin watcher.
_STOP = threading.Event()


def _emit(kind: str, text: str | None = None) -> None:
    record: dict[str, Any] = {"t": kind}
    if text is not None:
        record["x"] = text
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


def _watch_stdin(stop: threading.Event) -> None:
    """Stop recording as soon as the app sends anything (or closes stdin)."""
    try:
        for _line in sys.stdin:
            break
    except (ValueError, OSError):
        pass
    stop.set()


def _record(*, vad: bool):
    """Capture microphone audio into a buffer.

    With ``vad`` off (push-to-talk) it records until ``_STOP`` is set. With
    ``vad`` on (conversation) it waits for speech, then stops after a trailing
    pause. Returns ``(audio, source_rate)`` or ``None`` when nothing was said.
    """
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError as exc:  # pragma: no cover - optional runtime packages.
        raise RuntimeError(
            "Voice input requires pip install mlx-whisper sounddevice soundfile."
        ) from exc

    device_index, _device, source_rate = select_input_device()
    block = max(1, int(source_rate * 0.03))
    threshold = float(os.getenv("AVIS_VAD_THRESHOLD", "0.012"))
    hangover = float(os.getenv("AVIS_VAD_HANGOVER", "1.0"))
    max_seconds = float(os.getenv("AVIS_VAD_MAX", "30"))
    min_speech = float(os.getenv("AVIS_VAD_MIN_SPEECH", "0.3"))

    frames: list = []
    speech_started = False
    speech_time = 0.0
    silence_time = 0.0

    _emit("listening")
    with sd.InputStream(
        samplerate=source_rate,
        channels=1,
        dtype="float32",
        device=device_index,
        blocksize=block,
    ) as stream:
        while not _STOP.is_set():
            data, _overflowed = stream.read(block)
            mono = np.asarray(data[:, 0], dtype="float32")
            frames.append(mono.copy())
            if not vad:
                continue
            rms = float(np.sqrt(np.mean(np.square(mono)))) if mono.size else 0.0
            dt = len(mono) / source_rate
            if rms >= threshold:
                speech_started = True
                speech_time += dt
                silence_time = 0.0
            elif speech_started:
                silence_time += dt
            if speech_started and silence_time >= hangover and speech_time >= min_speech:
                break
            if speech_started and (speech_time + silence_time) >= max_seconds:
                break

    if not frames or (vad and not speech_started):
        return None
    return np.concatenate(frames), source_rate


def _transcribe_and_emit(result) -> None:
    if result is None:
        _emit("silence")
        return
    import numpy as np

    audio, source_rate = result
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak < 0.003:
        _emit("silence")
        return
    output_path = str(Path(tempfile.gettempdir()) / f"avis_voice_{uuid.uuid4().hex}.wav")
    write_wav_16k(audio, source_rate, output_path)
    try:
        text = transcribe_audio(output_path).strip()
    finally:
        try:
            Path(output_path).unlink(missing_ok=True)
        except OSError:
            pass
    if text:
        _emit("text", text)
    else:
        _emit("silence")


def cmd_listen_ptt() -> None:
    watcher = threading.Thread(target=_watch_stdin, args=(_STOP,), daemon=True)
    watcher.start()
    _transcribe_and_emit(_record(vad=False))


def cmd_converse() -> None:
    _transcribe_and_emit(_record(vad=True))


def cmd_speak() -> None:
    # Synthesize the whole reply in a single ElevenLabs request and play it once.
    text = (sys.stdin.read() or "").strip()
    if not text:
        return
    speak(text)
    _emit("spoke")


def cmd_speak_stream() -> None:
    # Read the reply from stdin as the model streams it and speak it through the
    # prefetch pipeline, so audio starts after the first chunk and then plays
    # continuously while later chunks are synthesized in the background.
    import codecs

    speaker = StreamingSpeaker()
    speaker.start()
    decoder = codecs.getincrementaldecoder("utf-8")()
    file_descriptor = sys.stdin.fileno()
    while True:
        data = os.read(file_descriptor, 4096)   # returns as soon as any bytes arrive
        if not data:
            break
        text = decoder.decode(data)
        if text:
            speaker.feed(text)
    tail = decoder.decode(b"", final=True)
    if tail:
        speaker.feed(tail)
    speaker.finish()
    _emit("spoke")


def _handle_terminate(_signum, _frame) -> None:
    _STOP.set()
    interrupt()  # stop any in-flight ElevenLabs playback immediately


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_terminate)
    signal.signal(signal.SIGINT, _handle_terminate)

    command = sys.argv[1] if len(sys.argv) > 1 else ""
    handlers = {
        "listen-ptt": cmd_listen_ptt,
        "converse": cmd_converse,
        "speak": cmd_speak,
        "speak-stream": cmd_speak_stream,
    }
    handler = handlers.get(command)
    if handler is None:
        _emit("error", f"Unknown voice command: {command!r}")
        return
    try:
        handler()
    except Exception as error:  # surface any failure to the app as a clean line.
        _emit("error", str(error))


if __name__ == "__main__":
    main()
