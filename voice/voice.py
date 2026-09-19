"""Local microphone capture and speech conversion for AVIS."""

from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from queue import Queue
from pathlib import Path

from dotenv import load_dotenv


load_dotenv(dotenv_path=".env.local")


VOICE_STOP = threading.Event()
MICROPHONE_HINT = os.getenv("AVIS_MICROPHONE", "MacBook Air Microphone")


TARGET_SAMPLE_RATE = 16_000


def select_input_device() -> tuple[int, dict, int]:
    """Pick the microphone to record from and its native sample rate.

    Prefers a device matching ``AVIS_MICROPHONE``, then the system default input,
    then the first available input. Shared by the fixed-length recorder here and
    the push-to-talk / conversation recorders in ``voice.bridge``.
    """
    try:
        import sounddevice as sd
    except ImportError as exc:  # pragma: no cover - depends on optional runtime packages.
        raise RuntimeError(
            "Voice input requires pip install mlx-whisper sounddevice soundfile."
        ) from exc

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
    return device_index, device, source_rate


def write_wav_16k(audio, source_rate: int, output_path: str) -> str:
    """Resample mono float32 audio to Whisper's 16 kHz input and write a WAV."""
    import numpy as np
    import soundfile as sf

    samples = np.asarray(audio, dtype="float32")
    if samples.ndim == 1:
        samples = samples[:, None]
    if source_rate != TARGET_SAMPLE_RATE and len(samples):
        target_length = round(len(samples) * TARGET_SAMPLE_RATE / source_rate)
        source_times = np.linspace(0, 1, len(samples), endpoint=False)
        target_times = np.linspace(0, 1, target_length, endpoint=False)
        samples = np.interp(target_times, source_times, samples[:, 0]).astype("float32")[:, None]
    sf.write(output_path, samples, TARGET_SAMPLE_RATE)
    return output_path


def record_audio(seconds: float = 6.0, output_path: str | None = None) -> str:
    """Record from the Mac microphone and convert it to Whisper's 16 kHz input."""
    try:
        import sounddevice as sd
    except ImportError as exc:  # pragma: no cover - depends on optional runtime packages.
        raise RuntimeError(
            "Voice input requires pip install mlx-whisper sounddevice soundfile."
        ) from exc

    if seconds <= 0:
        raise ValueError("Recording length must be positive.")
    if output_path is None:
        output_path = str(Path(tempfile.gettempdir()) / f"avis_voice_{uuid.uuid4().hex}.wav")

    import numpy as np

    device_index, _device, source_rate = select_input_device()
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
    return write_wav_16k(audio, source_rate, output_path)


def transcribe_audio(audio_path: str) -> str:
    """Convert a recorded WAV file into text using the best available Whisper model.

    The audio is loaded into a 16 kHz float32 array and handed to mlx-whisper
    directly, so transcription works without ffmpeg on the PATH (mlx-whisper only
    shells out to ffmpeg when given a file path, not an array). Models must be in
    MLX format (``mlx-community/*``); the original ``openai/*`` weights are not.
    """
    try:
        import mlx_whisper
    except ImportError as exc:  # pragma: no cover - depends on optional runtime packages.
        raise RuntimeError(
            "Voice transcription requires pip install mlx-whisper sounddevice soundfile."
        ) from exc

    audio_file = Path(audio_path)
    if not audio_file.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    import numpy as np
    import soundfile as sf

    samples, sample_rate = sf.read(str(audio_file), dtype="float32", always_2d=False)
    if samples.ndim > 1:  # collapse any stray stereo to mono
        samples = samples.mean(axis=1)
    if sample_rate != TARGET_SAMPLE_RATE and len(samples):
        target_length = round(len(samples) * TARGET_SAMPLE_RATE / sample_rate)
        source_times = np.linspace(0, 1, len(samples), endpoint=False)
        target_times = np.linspace(0, 1, target_length, endpoint=False)
        samples = np.interp(target_times, source_times, samples)
    samples = np.ascontiguousarray(samples, dtype="float32")

    model_candidates = [
        os.getenv("AVIS_WHISPER_MODEL", "mlx-community/whisper-small-mlx"),
        "mlx-community/whisper-small-mlx",
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
                    samples,
                    path_or_hf_repo=model_name,
                    fp16=False,
                    **transcription_options,
                )
            except TypeError:
                result = mlx_whisper.transcribe(samples, path_or_hf_repo=model_name)
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


def _describe_tts_error(error: Exception) -> str:
    """Pull the human-readable message out of an ElevenLabs API error.

    The SDK's ApiError stringifies to a wall of response headers; the useful part
    (e.g. a quota_exceeded message) lives in ``error.body['detail']``.
    """
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, dict) and detail.get("message"):
            return str(detail["message"])
        if isinstance(detail, str) and detail:
            return detail
    status = getattr(error, "status_code", None)
    return f"HTTP {status}" if status else str(error)


def _play_file(path: Path) -> None:
    """Play an audio file with afplay, stopping cleanly on interrupt."""
    process = subprocess.Popen(
        ["afplay", str(path)],
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
        raise RuntimeError(error.strip() or "Could not play audio.")


def _synth_elevenlabs(text: str) -> Path | None:
    """Synthesize text to an mp3 file with ElevenLabs, without playing it.

    Returns the file path, or ``None`` if interrupted mid-synthesis. Raising
    keeps synthesis separate from playback so a chunk can be generated while the
    previous one is still being spoken (the prefetch pipeline).
    """
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
    try:
        client = ElevenLabs(api_key=api_key)
        response = client.text_to_speech.convert(
            voice_id=voice_id,
            model_id=model_id,
            text=text,
        )
        with output_path.open("wb") as audio_file:
            for chunk in response:
                if VOICE_STOP.is_set():
                    output_path.unlink(missing_ok=True)
                    return None
                audio_file.write(chunk)
        return output_path
    except Exception as error:
        output_path.unlink(missing_ok=True)
        if isinstance(error, RuntimeError):
            raise
        raise RuntimeError(f"ElevenLabs speech failed: {_describe_tts_error(error)}") from error


def _speak_elevenlabs(text: str) -> None:
    """Synthesize the whole text in one ElevenLabs request and play it once."""
    path = _synth_elevenlabs(text)
    if path is None:
        return
    try:
        _play_file(path)
    finally:
        path.unlink(missing_ok=True)


def _speak_macos(text: str) -> None:
    """Free, offline speech with the built-in macOS `say` command."""
    arguments = ["say"]
    say_voice = os.getenv("AVIS_SAY_VOICE")
    if say_voice:
        arguments += ["-v", say_voice]
    say_rate = os.getenv("AVIS_SAY_RATE")
    if say_rate:
        arguments += ["-r", say_rate]
    arguments.append(text)
    process = subprocess.Popen(arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    while process.poll() is None:
        if VOICE_STOP.wait(0.05):
            process.terminate()
            process.wait(timeout=1)
            return


def speak(text: str, voice: str = "Samantha") -> None:
    """Read text aloud, stopping cleanly on interrupt.

    Uses ElevenLabs by default and falls back to the built-in macOS voice when
    ElevenLabs is unavailable (no key, no network, or out of quota) so speech
    keeps working for free. Set ``AVIS_TTS_ENGINE=say`` to always use the macOS
    voice, or ``AVIS_TTS_FALLBACK=off`` to surface ElevenLabs errors instead.
    """
    if not text or not text.strip():
        return
    if platform.system() != "Darwin":
        raise RuntimeError("Speech playback is only supported on macOS.")
    text = text.strip()
    VOICE_STOP.clear()

    engine = os.getenv("AVIS_TTS_ENGINE", "elevenlabs").strip().lower()
    if engine in {"say", "macos", "system"}:
        _speak_macos(text)
        return

    try:
        _speak_elevenlabs(text)
    except RuntimeError as error:
        fallback = os.getenv("AVIS_TTS_FALLBACK", "say").strip().lower()
        if fallback in {"", "off", "none", "0", "false"}:
            raise
        if VOICE_STOP.is_set():
            return
        sys.stderr.write(f"{error} Falling back to the macOS voice.\n")
        sys.stderr.flush()
        _speak_macos(text)


class StreamingSpeaker:
    """Speak a reply continuously as it is generated, with no gaps.

    Text is fed in as the model produces it and cut into chunks at sentence or
    clause boundaries near a target size. A synthesizer thread turns chunks into
    audio while a player thread plays the previous one, so after the first chunk
    the audio is prefetched and playback never waits on the network.

    Chunk size is chosen so a chunk always plays for longer than the next one
    takes to synthesize, which is what keeps the pipeline from starving. With
    speech playing at ``r`` chars/s, ElevenLabs generating at ``g`` chars/s
    (faster than real time) and a fixed per-request overhead ``a`` seconds, the
    no-gap condition ``chars/r >= a + chars/g`` gives ``chars >= a*r/(1 - r/g)``.
    For r=15, g=30 (~2x real time) and a=2s that floor is ~60 chars. Crucially
    the floor applies to the *first* chunk too: it must play long enough to hide
    the next chunk's synthesis, so a tiny opening chunk cannot be gapless under a
    2s overhead. The steady chunk sits comfortably above the floor at ~130 chars
    (~32 tokens, ~9s of speech) and the first chunk is only slightly smaller
    (~90 chars) — the minimum that starts speech quickly yet stays gap-free.
    A lower-overhead model (turbo/flash) can safely use much smaller chunks: set
    AVIS_TTS_FIRST_CHUNK_CHARS / AVIS_TTS_CHUNK_CHARS to start sooner.
    """

    def __init__(self) -> None:
        self._first_target = max(30, int(os.getenv("AVIS_TTS_FIRST_CHUNK_CHARS", "90")))
        self._steady_target = max(60, int(os.getenv("AVIS_TTS_CHUNK_CHARS", "130")))
        self._buffer = ""
        self._first_done = False
        self._chunks: Queue[str | None] = Queue()
        # Prefetch depth: the synthesizer may run up to two chunks ahead of the
        # player, which is what hides ElevenLabs' per-request latency.
        self._audio: Queue[tuple[str, str] | None] = Queue(maxsize=2)
        engine = os.getenv("AVIS_TTS_ENGINE", "elevenlabs").strip().lower()
        self._use_say = engine in {"say", "macos", "system"}
        self._fallback = os.getenv("AVIS_TTS_FALLBACK", "say").strip().lower() not in {"", "off", "none", "0", "false"}
        self._warned = False
        self._synth = threading.Thread(target=self._synth_loop, daemon=True)
        self._player = threading.Thread(target=self._play_loop, daemon=True)
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        VOICE_STOP.clear()
        self._synth.start()
        self._player.start()
        self._started = True

    def feed(self, text: str) -> None:
        """Add newly generated text and emit any chunks that are now complete."""
        if not text:
            return
        self._buffer += text
        self._drain(final=False)

    def finish(self) -> None:
        """Flush the remaining text and wait until everything has been spoken."""
        self._drain(final=True)
        self._chunks.put(None)
        if self._started:
            self._synth.join()
            self._player.join()

    # --- chunking -----------------------------------------------------------

    def _limits(self) -> tuple[int, int]:
        # `low` is a floor, not just a target: the first chunk must not be emitted
        # so short that it finishes playing before the next chunk is synthesized.
        if not self._first_done:
            target = self._first_target
            return int(target * 0.8), int(target * 1.7)
        target = self._steady_target
        return int(target * 0.7), int(target * 1.6)

    def _drain(self, final: bool) -> None:
        while True:
            buffer = self._buffer
            if not buffer.strip():
                if final:
                    self._buffer = ""
                return
            low, high = self._limits()
            if final:
                # Speak everything left, splitting on sentence ends if it is long.
                if len(buffer) <= high:
                    self._emit(buffer)
                    self._buffer = ""
                    return
                cut = (self._find_boundary(buffer, low, high, ".!?")
                       or self._find_boundary(buffer, low, high, ",;:")
                       or self._last_space(buffer, low, high) or high)
                self._emit(buffer[:cut])
                self._buffer = buffer[cut:]
                continue
            if len(buffer) < low:
                return
            # Prefer a sentence end; hold out for one until the chunk grows long,
            # then fall back to a clause break or a word gap. This keeps chunks
            # near an even size, so a short chunk never precedes a longer one and
            # starves the pipeline.
            cut = self._find_boundary(buffer, low, min(len(buffer), high), ".!?")
            if cut is None:
                if len(buffer) < high:
                    return  # wait for a sentence end or more text
                cut = (self._find_boundary(buffer, low, high, ",;:")
                       or self._last_space(buffer, low, high) or high)
            self._emit(buffer[:cut])
            self._buffer = buffer[cut:]

    def _emit(self, text: str) -> None:
        chunk = text.strip()
        if chunk:
            self._chunks.put(chunk)
            self._first_done = True

    @staticmethod
    def _find_boundary(buffer: str, low: int, high: int, marks: str) -> int | None:
        """Latest position in [low, high] right after one of `marks`, or None."""
        for j in range(min(high, len(buffer)), low - 1, -1):
            if 1 <= j and buffer[j - 1] in marks and (j == len(buffer) or buffer[j] in " \n\t"):
                return j
        return None

    @staticmethod
    def _last_space(buffer: str, low: int, high: int) -> int | None:
        for j in range(min(high, len(buffer)), low - 1, -1):
            if buffer[j - 1] == " ":
                return j
        return None

    # --- pipeline threads ---------------------------------------------------

    def _synth_loop(self) -> None:
        while True:
            chunk = self._chunks.get()
            if chunk is None:
                self._audio.put(None)
                return
            if VOICE_STOP.is_set():
                continue
            if self._use_say:
                self._audio.put(("say", chunk))
                continue
            try:
                path = _synth_elevenlabs(chunk)
                if path is None:  # interrupted mid-synthesis
                    continue
                self._audio.put(("file", str(path)))
            except RuntimeError as error:
                if self._fallback:
                    if not self._warned:
                        sys.stderr.write(f"{error} Falling back to the macOS voice.\n")
                        sys.stderr.flush()
                        self._warned = True
                    self._use_say = True
                    self._audio.put(("say", chunk))
                else:
                    self._audio.put(("error", str(error)))

    def _play_loop(self) -> None:
        while True:
            item = self._audio.get()
            if item is None:
                return
            kind, payload = item
            if VOICE_STOP.is_set():
                if kind == "file":
                    Path(payload).unlink(missing_ok=True)
                continue
            if kind == "file":
                try:
                    _play_file(Path(payload))
                except RuntimeError:
                    pass
                finally:
                    Path(payload).unlink(missing_ok=True)
            elif kind == "say":
                _speak_macos(payload)
            elif kind == "error":
                sys.stderr.write(payload + "\n")
                sys.stderr.flush()


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
