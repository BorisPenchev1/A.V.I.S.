"""Local microphone capture and speech conversion for AVIS."""

from __future__ import annotations

import os
import platform
import re
import subprocess
import tempfile
import threading
import uuid
from queue import Queue
from pathlib import Path

from dotenv import load_dotenv


load_dotenv(dotenv_path=".env.local")


VOICE_STOP = threading.Event()
MICROPHONE_HINT = os.getenv("AVIS_MICROPHONE", "MacBook Air Microphone")


def record_audio(seconds: float = 6.0, output_path: str | None = None) -> str:
    """Record from the Mac microphone and convert it to Whisper's 16 kHz input."""
    try:
        import sounddevice as sd
        import soundfile as sf
    except ImportError as exc:  # pragma: no cover - depends on optional runtime packages.
        raise RuntimeError(
            "Voice input requires pip install mlx-whisper sounddevice soundfile."
        ) from exc

    if seconds <= 0:
        raise ValueError("Recording length must be positive.")
    if output_path is None:
        output_path = str(Path(tempfile.gettempdir()) / f"avis_voice_{uuid.uuid4().hex}.wav")

    import numpy as np

    devices = sd.query_devices()
    input_devices = [
        (index, device)
        for index, device in enumerate(devices)
        if device.get("max_input_channels", 0) > 0
    ]
    matching_device = next(
        ((index, device) for index, device in input_devices if MICROPHONE_HINT.casefold() in device["name"].casefold()),
        None,
    )
    default_input = sd.default.device[0]
    default_device = next(
        ((index, device) for index, device in input_devices if index == default_input),
        None,
    )
    device_index, device = matching_device or default_device or next(iter(input_devices), (None, None))
    if device_index is None or device is None:
        raise RuntimeError("No microphone input device is available.")
    source_rate = int(device.get("default_samplerate") or 44_100)
    target_rate = 16_000
    frame_count = int(source_rate * seconds)
    audio = sd.rec(
        frame_count,
        samplerate=source_rate,
        channels=1,
        dtype="float32",
        device=device_index,
    )
    sd.wait()
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak < 0.002:
        raise RuntimeError(
            f"Microphone input is nearly silent (peak {peak:.4f}). Check macOS microphone permission and input volume."
        )
    if source_rate != target_rate:
        target_length = round(len(audio) * target_rate / source_rate)
        source_times = np.linspace(0, 1, len(audio), endpoint=False)
        target_times = np.linspace(0, 1, target_length, endpoint=False)
        audio = np.interp(target_times, source_times, audio[:, 0]).astype("float32")[:, None]
    sf.write(output_path, audio, target_rate)
    return output_path


def transcribe_audio(audio_path: str) -> str:
    """Convert a recorded WAV file into text using the best available Whisper model."""
    try:
        import mlx_whisper
    except ImportError as exc:  # pragma: no cover - depends on optional runtime packages.
        raise RuntimeError(
            "Voice transcription requires pip install mlx-whisper sounddevice soundfile."
        ) from exc

    audio_file = Path(audio_path)
    if not audio_file.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    model_candidates = [
        os.getenv("AVIS_WHISPER_MODEL", "openai/whisper-small"),
        "openai/whisper-small",
        "mlx-community/whisper-tiny",
    ]
    transcription_options = {
        "language": os.getenv("AVIS_STT_LANGUAGE", "en"),
        "temperature": 0,
        "condition_on_previous_text": False,
    }
    errors: list[str] = []
    for model_name in dict.fromkeys(model_candidates):
        try:
            try:
                result = mlx_whisper.transcribe(
                    str(audio_file),
                    path_or_hf_repo=model_name,
                    fp16=False,
                    **transcription_options,
                )
            except TypeError:
                result = mlx_whisper.transcribe(str(audio_file), path_or_hf_repo=model_name)
            text = (result.get("text") or "").strip()
            if text:
                return text
            errors.append(f"{model_name}: no text returned")
        except Exception as exc:  # pragma: no cover - exact model availability varies by environment.
            errors.append(f"{model_name}: {exc}")

    raise RuntimeError(
        "Could not transcribe the recording with a valid Whisper model. "
        + "; ".join(errors[-2:])
    )


def speak(text: str, voice: str = "Samantha") -> None:
    """Generate and play speech with ElevenLabs, stopping cleanly on interrupt."""
    if not text or not text.strip():
        return
    if platform.system() != "Darwin":
        raise RuntimeError("ElevenLabs playback is only supported on macOS.")
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is missing from .env.local.")
    try:
        from elevenlabs.client import ElevenLabs
    except ImportError as exc:
        raise RuntimeError("ElevenLabs support requires pip install elevenlabs python-dotenv.") from exc

    voice_id = os.getenv("ELEVENLABS_VOICE_ID")
    if not voice_id:
        raise RuntimeError(
            "ELEVENLABS_VOICE_ID is missing. Copy the ID of a voice in My Voices into .env.local."
        )
    model_id = os.getenv("ELEVENLABS_MODEL_ID", "eleven_v3")
    output_path = Path(tempfile.gettempdir()) / f"avis_tts_{uuid.uuid4().hex}.mp3"
    VOICE_STOP.clear()
    try:
        client = ElevenLabs(api_key=api_key)
        response = client.text_to_speech.convert(
            voice_id=voice_id,
            model_id=model_id,
            text=text.strip(),
        )
        with output_path.open("wb") as audio_file:
            for chunk in response:
                if VOICE_STOP.is_set():
                    return
                audio_file.write(chunk)
        process = subprocess.Popen(
            ["afplay", str(output_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        while process.poll() is None:
            if VOICE_STOP.wait(0.05):
                process.terminate()
                process.wait(timeout=1)
                return
        if process.returncode != 0:
            error = process.stderr.read() if process.stderr else ""
            raise RuntimeError(error.strip() or "Could not play ElevenLabs audio.")
    except Exception as error:
        if isinstance(error, RuntimeError):
            raise
        raise RuntimeError(f"ElevenLabs speech failed: {error}") from error
    finally:
        output_path.unlink(missing_ok=True)


class SpeechQueue:
    """Generate and play completed sentences while a response is still streaming."""

    def __init__(self, voice: str = "Daniel") -> None:
        self.voice = voice
        self._queue: Queue[str | None] = Queue()
        self._buffer = ""
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._started = False

    def start(self) -> None:
        if not self._started:
            VOICE_STOP.clear()
            self._worker.start()
            self._started = True

    def feed(self, text: str) -> None:
        """Queue complete sentences and retain the unfinished fragment."""
        if not text:
            return
        self._buffer += text
        while True:
            match = re.search(r"(.+?[.!?](?:\s+|$))", self._buffer, re.DOTALL)
            if not match:
                if len(self._buffer) > 220:
                    split_at = self._buffer.rfind(" ", 0, 220)
                    if split_at > 40:
                        self._queue.put(self._buffer[:split_at].strip())
                        self._buffer = self._buffer[split_at:]
                return
            sentence = match.group(1).strip()
            self._buffer = self._buffer[match.end():]
            if sentence:
                self._queue.put(sentence)

    def finish(self) -> None:
        """Flush the final fragment and wait until queued speech has played."""
        if self._buffer.strip():
            self._queue.put(self._buffer.strip())
            self._buffer = ""
        if self._started:
            self._queue.join()

    def close(self) -> None:
        if self._started:
            self._queue.put(None)
            self._worker.join(timeout=2)

    def _run(self) -> None:
        while True:
            text = self._queue.get()
            try:
                if text is None:
                    return
                if not VOICE_STOP.is_set():
                    speak(text, self.voice)
            except RuntimeError as error:
                print(f"Speech error: {error}")
            finally:
                self._queue.task_done()


def listen() -> str:
    """Record a voice input and transcribe it to text."""
    audio_path = record_audio()
    try:
        return transcribe_audio(audio_path)
    finally:
        try:
            Path(audio_path).unlink(missing_ok=True)
        except OSError:
            pass


def interrupt() -> None:
    """Stop current speech and mark voice work for interruption."""
    VOICE_STOP.set()
