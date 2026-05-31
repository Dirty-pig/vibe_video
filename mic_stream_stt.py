import argparse
import msvcrt
import queue
import sys
import time
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event, Lock, Thread
from uuid import uuid4

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel


OUTPUT_FILE = Path("mic_transcripts.md")
CACHE_DIR = Path("transcripts_cache")
SILENCE_RMS_THRESHOLD = 0.012
MIN_VOICE_SECONDS = 1.0
ENTER_DEBOUNCE_SECONDS = 0.5
AUDIO_QUEUE_MAXSIZE = 64
STALE_TEMP_FILE_SECONDS = 600
INITIAL_PROMPT = (
    "中文普通话。"
    "术语：prompt编译、提示词编译、system prompt、结构化输出、JSON schema、"
    "compiled_prompt、missing_info、confidence、mode、debug、feature、refactor、plan、research、writing、"
    "推理、模型推理、推理时间、延迟、显存、GPU、CUDA、阅读项目、脚本重构、环境配置、"
    "VS Code、Codex、Copilot、Cline、faster-whisper、API。"
)


@dataclass
class RecordingSession:
    temp_path: Path
    record_seconds: float
    voiced_samples: int
    written_chunks: int
    dropped_chunks: int
    stop_reason: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Microphone recording STT")
    parser.add_argument("--mic-device", type=int, default=None, help="Microphone device index")
    parser.add_argument("--sample-rate", type=int, default=16000, help="Microphone sample rate")
    parser.add_argument(
        "--max-record-seconds",
        type=float,
        default=600.0,
        help="Maximum recording duration before auto-stop (default: 600)",
    )
    parser.add_argument("--list-devices", action="store_true", help="List audio devices and exit")
    return parser.parse_args()


def append_markdown(transcript: str, language: str, probability: float, infer_seconds: float) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = (
        f"## Recording {timestamp}\n\n"
        f"- Language: {language} ({probability:.2f})\n"
        f"- Inference time: {infer_seconds:.2f}s\n\n"
        f"{transcript or 'No speech text recognized.'}\n\n"
    )
    with OUTPUT_FILE.open("a", encoding="utf-8") as file:
        file.write(entry)


def ensure_cache_dir() -> None:
    CACHE_DIR.mkdir(exist_ok=True)


def cleanup_stale_temp_files() -> None:
    ensure_cache_dir()
    cutoff = time.time() - STALE_TEMP_FILE_SECONDS
    for path in CACHE_DIR.glob("*.wav"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def create_temp_wav_path() -> Path:
    ensure_cache_dir()
    return CACHE_DIR / f"recording_{uuid4().hex}.wav"


def safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def clear_pending_enters() -> None:
    while msvcrt.kbhit():
        key = msvcrt.getwch()
        if key not in {"\r", "\n"}:
            continue


def wait_for_enter(prompt: str, min_wait_seconds: float = 0.0) -> bool:
    if prompt:
        print(prompt, end="", flush=True)

    start_time = time.perf_counter()
    try:
        while True:
            if not msvcrt.kbhit():
                time.sleep(0.01)
                continue

            key = msvcrt.getwch()
            if key not in {"\r", "\n"}:
                continue

            if time.perf_counter() - start_time < min_wait_seconds:
                clear_pending_enters()
                continue

            time.sleep(ENTER_DEBOUNCE_SECONDS)
            clear_pending_enters()
            return True
    except (EOFError, KeyboardInterrupt):
        return False


def has_obvious_speech(voiced_samples: int, sample_rate: int) -> bool:
    return voiced_samples >= int(sample_rate * MIN_VOICE_SECONDS)


def looks_like_garbage_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False

    if len(stripped) < 8:
        return False

    unique_ratio = len(set(stripped)) / len(stripped)
    if unique_ratio < 0.12:
        return True

    repeated_char_count = max(stripped.count(char) for char in set(stripped))
    if repeated_char_count / len(stripped) > 0.45:
        return True

    punctuation_chars = '"“”\'\'。，、！？：；（）()[]{}<>-_=+*/\\|~`^…'
    punctuation_ratio = sum(1 for char in stripped if char in punctuation_chars) / len(stripped)
    if punctuation_ratio > 0.5:
        return True

    return False


def write_audio_worker(
    audio_queue: queue.Queue[np.ndarray],
    stream_closed_event: Event,
    sample_rate: int,
    temp_path: Path,
    state: dict[str, int],
    state_lock: Lock,
) -> None:
    with wave.open(str(temp_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)

        while not (stream_closed_event.is_set() and audio_queue.empty()):
            try:
                chunk = audio_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            mono_audio = chunk[:, 0] if chunk.ndim == 2 else chunk
            mono_audio = np.ascontiguousarray(mono_audio, dtype=np.float32)

            rms = float(np.sqrt(np.mean(np.square(mono_audio), dtype=np.float64)))
            pcm16_audio = np.clip(mono_audio, -1.0, 1.0)
            pcm16_audio = (pcm16_audio * 32767.0).astype(np.int16)
            wav_file.writeframes(pcm16_audio.tobytes())

            with state_lock:
                state["written_chunks"] += 1
                if rms >= SILENCE_RMS_THRESHOLD:
                    state["voiced_samples"] += mono_audio.size


def record_until_enter(
    sample_rate: int,
    mic_device: int | None,
    max_record_seconds: float,
) -> RecordingSession:
    temp_path = create_temp_wav_path()
    audio_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=AUDIO_QUEUE_MAXSIZE)
    stop_event = Event()
    stream_closed_event = Event()
    state = {"voiced_samples": 0, "written_chunks": 0, "dropped_chunks": 0}
    state_lock = Lock()
    stop_reason = "manual"

    def callback(indata: np.ndarray, frames: int, callback_time, status) -> None:
        del frames, callback_time
        if status:
            print(f"Audio status: {status}", file=sys.stderr)
        try:
            audio_queue.put_nowait(indata.copy())
        except queue.Full:
            with state_lock:
                state["dropped_chunks"] += 1

    def wait_stop() -> None:
        if wait_for_enter("", min_wait_seconds=ENTER_DEBOUNCE_SECONDS):
            stop_event.set()

    writer = Thread(
        target=write_audio_worker,
        args=(audio_queue, stream_closed_event, sample_rate, temp_path, state, state_lock),
        daemon=True,
    )

    record_start = time.perf_counter()
    writer.start()
    record_failed = False
    try:
        with sd.InputStream(
            samplerate=sample_rate,
            blocksize=int(sample_rate * 0.5),
            device=mic_device,
            channels=1,
            dtype="float32",
            callback=callback,
        ):
            print(
                f"Recording... Press Enter again to stop. "
                f"Max {max_record_seconds:.0f}s."
            )
            stopper = Thread(target=wait_stop, daemon=True)
            stopper.start()
            while not stop_event.is_set():
                if time.perf_counter() - record_start >= max_record_seconds:
                    stop_reason = "timeout"
                    stop_event.set()
                    break
                time.sleep(0.05)
    except Exception:
        record_failed = True
        raise
    finally:
        stream_closed_event.set()
        writer.join()
        if record_failed:
            safe_unlink(temp_path)

    record_seconds = time.perf_counter() - record_start
    with state_lock:
        voiced_samples = state["voiced_samples"]
        written_chunks = state["written_chunks"]
        dropped_chunks = state["dropped_chunks"]

    return RecordingSession(
        temp_path=temp_path,
        record_seconds=record_seconds,
        voiced_samples=voiced_samples,
        written_chunks=written_chunks,
        dropped_chunks=dropped_chunks,
        stop_reason=stop_reason,
    )


def main() -> int:
    args = parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return 0

    cleanup_stale_temp_files()

    load_start = time.perf_counter()
    try:
        model = WhisperModel("small", device="cuda", compute_type="int8_float16")
    except Exception as exc:
        print(f"Error loading model: {exc}", file=sys.stderr)
        return 1
    print(f"Model ready in {time.perf_counter() - load_start:.2f}s")

    try:
        while True:
            if not wait_for_enter("Press Enter to start recording, or Ctrl+C to exit.\n"):
                print("Exiting.")
                return 0

            session = record_until_enter(
                args.sample_rate,
                args.mic_device,
                args.max_record_seconds,
            )

            if session.stop_reason == "timeout":
                print("Max record duration reached. Stopped automatically.")

            try:
                if session.written_chunks == 0:
                    print("No audio captured.\n")
                    continue

                if not has_obvious_speech(session.voiced_samples, args.sample_rate):
                    print("No obvious speech detected. Skipping inference.\n")
                    continue

                infer_start = time.perf_counter()
                segments, info = model.transcribe(
                    str(session.temp_path),
                    language="zh",
                    task="transcribe",
                    beam_size=1,
                    best_of=1,
                    temperature=0.0,
                    condition_on_previous_text=False,
                    initial_prompt=INITIAL_PROMPT,
                )
                raw_text = "".join(seg.text.strip() for seg in segments if seg.text.strip())
                infer_seconds = time.perf_counter() - infer_start
            finally:
                safe_unlink(session.temp_path)

            if looks_like_garbage_text(raw_text):
                raw_text = ""

            print("Raw text:")
            print(raw_text or "No speech text recognized.")
            print(f"Language: {info.language} ({info.language_probability:.2f})")
            print(f"Record time: {session.record_seconds:.2f}s")
            print(f"Inference time: {infer_seconds:.2f}s")
            if session.dropped_chunks > 0:
                print(
                    "Warning: dropped "
                    f"{session.dropped_chunks} audio chunks due to a full buffer."
                )
            print()

            append_markdown(raw_text, info.language, info.language_probability, infer_seconds)

    except KeyboardInterrupt:
        print("\nExiting.")
        return 0
    except Exception as exc:
        print(f"Runtime error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
