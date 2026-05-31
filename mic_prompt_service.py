import json
import queue
import sys
import time
import traceback
from dataclasses import asdict
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

import sounddevice as sd
from openai import OpenAI
from faster_whisper import WhisperModel

import mic_prompt_deepseek as compiler
from mic_stream_stt import (
    INITIAL_PROMPT,
    RecordingSession,
    cleanup_stale_temp_files,
    create_temp_wav_path,
    has_obvious_speech,
    looks_like_garbage_text,
    safe_unlink,
    write_audio_worker,
)

compiler.DEBUG = False

DEFAULT_SAMPLE_RATE = 16000
DEFAULT_MAX_RECORD_SECONDS = 600.0


def configure_stdio_encoding() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


class BackgroundRecorder:
    def __init__(
        self,
        sample_rate: int,
        mic_device: int | None,
        max_record_seconds: float,
    ) -> None:
        self.sample_rate = sample_rate
        self.mic_device = mic_device
        self.max_record_seconds = max_record_seconds
        self.temp_path = create_temp_wav_path()
        self.audio_queue: queue.Queue = queue.Queue(maxsize=64)
        self.stop_event = Event()
        self.stream_closed_event = Event()
        self.state = {"voiced_samples": 0, "written_chunks": 0, "dropped_chunks": 0}
        self.state_lock = Lock()
        self.stop_reason = "manual"
        self.record_start = 0.0
        self.writer: Thread | None = None
        self.timeout_thread: Thread | None = None
        self.stream: sd.InputStream | None = None
        self.is_recording = False

    def _callback(self, indata, frames, callback_time, status) -> None:
        del frames, callback_time
        if status:
            print(f"Audio status: {status}", file=sys.stderr, flush=True)
        try:
            self.audio_queue.put_nowait(indata.copy())
        except queue.Full:
            with self.state_lock:
                self.state["dropped_chunks"] += 1

    def _watch_timeout(self) -> None:
        while not self.stop_event.is_set():
            if time.perf_counter() - self.record_start >= self.max_record_seconds:
                self.stop_reason = "timeout"
                self.stop_event.set()
                break
            time.sleep(0.05)

    def start(self) -> None:
        if self.is_recording:
            raise RuntimeError("Recorder is already running")

        self.writer = Thread(
            target=write_audio_worker,
            args=(
                self.audio_queue,
                self.stream_closed_event,
                self.sample_rate,
                self.temp_path,
                self.state,
                self.state_lock,
            ),
            daemon=True,
        )
        self.writer.start()
        self.stream = sd.InputStream(
            samplerate=self.sample_rate,
            blocksize=int(self.sample_rate * 0.5),
            device=self.mic_device,
            channels=1,
            dtype="float32",
            callback=self._callback,
        )
        self.stream.start()
        self.record_start = time.perf_counter()
        self.is_recording = True
        self.timeout_thread = Thread(target=self._watch_timeout, daemon=True)
        self.timeout_thread.start()

    def stop(self) -> RecordingSession:
        if not self.is_recording:
            raise RuntimeError("Recorder is not running")

        self.stop_event.set()
        try:
            if self.stream is not None:
                self.stream.stop()
                self.stream.close()
        finally:
            self.stream_closed_event.set()
            if self.writer is not None:
                self.writer.join()
            self.is_recording = False

        record_seconds = time.perf_counter() - self.record_start
        with self.state_lock:
            voiced_samples = self.state["voiced_samples"]
            written_chunks = self.state["written_chunks"]
            dropped_chunks = self.state["dropped_chunks"]

        return RecordingSession(
            temp_path=self.temp_path,
            record_seconds=record_seconds,
            voiced_samples=voiced_samples,
            written_chunks=written_chunks,
            dropped_chunks=dropped_chunks,
            stop_reason=self.stop_reason,
        )


class PromptCompilerService:
    def __init__(self) -> None:
        self.system_prompt = compiler.load_text(compiler.SYSTEM_PROMPT_FILE)
        self.modes_config = compiler.load_json(compiler.MODES_FILE)
        self.schema = compiler.load_json(compiler.SCHEMA_FILE)
        cleanup_stale_temp_files()
        self.stt_model = WhisperModel("small", device="cuda", compute_type="int8_float16")
        self.client = OpenAI(api_key=compiler.DEEPSEEK_API_KEY, base_url=compiler.DEEPSEEK_BASE_URL)
        self.recorder: BackgroundRecorder | None = None

    def emit_event(self, event: str, payload: dict[str, Any] | None = None) -> None:
        message = {"type": "event", "event": event, "payload": payload or {}}
        print(json.dumps(message, ensure_ascii=False), flush=True)

    def respond(self, request_id: str | int | None, ok: bool, payload: dict[str, Any] | None = None, error: str | None = None) -> None:
        message = {"type": "response", "id": request_id, "ok": ok}
        if payload is not None:
            message["payload"] = payload
        if error is not None:
            message["error"] = error
        print(json.dumps(message, ensure_ascii=False), flush=True)

    def serialize_result(
        self,
        raw_text: str,
        session: RecordingSession,
        info,
        infer_seconds: float,
        payload: dict[str, Any],
        api_seconds: float,
    ) -> dict[str, Any]:
        return {
            "raw_text": raw_text,
            "compiled_prompt": payload.get("compiled_prompt", ""),
            "agent_input": payload.get("agent_input", {}),
            "mode": payload.get("mode", compiler.DEFAULT_MODE),
            "confidence": payload.get("confidence", 0.0),
            "language": getattr(info, "language", "zh"),
            "language_probability": getattr(info, "language_probability", 1.0),
            "record_seconds": session.record_seconds,
            "infer_seconds": infer_seconds,
            "api_seconds": api_seconds,
            "dropped_chunks": session.dropped_chunks,
            "stop_reason": session.stop_reason,
        }

    def handle_start_recording(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.recorder and self.recorder.is_recording:
            raise RuntimeError("Recording is already in progress")

        sample_rate = int(payload.get("sample_rate") or DEFAULT_SAMPLE_RATE)
        mic_device = payload.get("mic_device")
        max_record_seconds = float(payload.get("max_record_seconds") or DEFAULT_MAX_RECORD_SECONDS)
        self.recorder = BackgroundRecorder(sample_rate, mic_device, max_record_seconds)
        self.recorder.start()
        self.emit_event("recording_state", {"recording": True})
        return {
            "recording": True,
            "sample_rate": sample_rate,
            "mic_device": mic_device,
            "max_record_seconds": max_record_seconds,
        }

    def handle_stop_recording(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        if not self.recorder or not self.recorder.is_recording:
            raise RuntimeError("No active recording to stop")

        session = self.recorder.stop()
        self.emit_event("recording_state", {"recording": False})
        try:
            if session.written_chunks == 0:
                return {
                    "raw_text": "",
                    "compiled_prompt": "",
                    "agent_input": {},
                    "mode": compiler.DEFAULT_MODE,
                    "confidence": 0.0,
                    "language": "zh",
                    "language_probability": 1.0,
                    "record_seconds": session.record_seconds,
                    "infer_seconds": 0.0,
                    "api_seconds": 0.0,
                    "dropped_chunks": session.dropped_chunks,
                    "stop_reason": session.stop_reason,
                    "message": "No audio captured.",
                }

            if not has_obvious_speech(session.voiced_samples, self.recorder.sample_rate):
                return {
                    "raw_text": "",
                    "compiled_prompt": "",
                    "agent_input": {},
                    "mode": compiler.DEFAULT_MODE,
                    "confidence": 0.0,
                    "language": "zh",
                    "language_probability": 1.0,
                    "record_seconds": session.record_seconds,
                    "infer_seconds": 0.0,
                    "api_seconds": 0.0,
                    "dropped_chunks": session.dropped_chunks,
                    "stop_reason": session.stop_reason,
                    "message": "No obvious speech detected.",
                }

            infer_start = time.perf_counter()
            segments, info = self.stt_model.transcribe(
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
            if looks_like_garbage_text(raw_text):
                raw_text = ""

            if not raw_text:
                payload_result = {
                    "mode": compiler.DEFAULT_MODE,
                    "compiled_prompt": "",
                    "confidence": 0.0,
                    "agent_input": {},
                }
                compiler.append_combined_markdown(
                    raw_text,
                    payload_result,
                    info.language,
                    info.language_probability,
                    infer_seconds,
                    0.0,
                )
                return self.serialize_result(raw_text, session, info, infer_seconds, payload_result, 0.0)

            mode = compiler.detect_mode(raw_text, self.modes_config)
            user_message = compiler.build_user_message(raw_text, mode, self.modes_config, "", self.schema)
            compiled_payload, api_seconds = compiler.compile_with_deepseek(
                self.client,
                self.system_prompt,
                user_message,
                self.schema,
            )
            compiler.write_latest_agent_payload(compiled_payload)
            compiler.append_combined_markdown(
                raw_text,
                compiled_payload,
                info.language,
                info.language_probability,
                infer_seconds,
                api_seconds,
            )
            compiler.append_prompt_archive(raw_text, compiled_payload)
            return self.serialize_result(raw_text, session, info, infer_seconds, compiled_payload, api_seconds)
        finally:
            safe_unlink(session.temp_path)
            self.recorder = None

    def handle_get_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        return {
            "recording": bool(self.recorder and self.recorder.is_recording),
            "backend_ready": True,
            "model": compiler.DEEPSEEK_MODEL,
            "prompt_archive_file": str(compiler.PROMPT_ARCHIVE_FILE),
            "latest_payload_file": str(compiler.LATEST_AGENT_PAYLOAD_FILE),
        }

    def handle_shutdown(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        if self.recorder and self.recorder.is_recording:
            session = self.recorder.stop()
            safe_unlink(session.temp_path)
            self.recorder = None
        return {"shutting_down": True}

    def dispatch(self, request: dict[str, Any]) -> tuple[bool, dict[str, Any] | None, str | None]:
        command = request.get("command")
        payload = request.get("payload") or {}
        if command == "start_recording":
            return True, self.handle_start_recording(payload), None
        if command == "stop_recording":
            return True, self.handle_stop_recording(payload), None
        if command == "get_state":
            return True, self.handle_get_state(payload), None
        if command == "shutdown":
            return True, self.handle_shutdown(payload), None
        return False, None, f"Unknown command: {command}"


def main() -> int:
    configure_stdio_encoding()

    if not compiler.DEEPSEEK_API_KEY:
        print("DeepSeek API key is not configured.", file=sys.stderr, flush=True)
        return 1

    try:
        service = PromptCompilerService()
    except Exception as exc:
        print(f"Failed to initialize service: {exc}", file=sys.stderr, flush=True)
        return 2

    service.emit_event(
        "ready",
        {
            "backend_ready": True,
            "model": compiler.DEEPSEEK_MODEL,
        },
    )

    for line in sys.stdin:
        raw = line.strip()
        if not raw:
            continue

        request_id = None
        try:
            request = json.loads(raw)
            request_id = request.get("id")
            ok, payload, error = service.dispatch(request)
            service.respond(request_id, ok, payload=payload, error=error)
            if request.get("command") == "shutdown":
                return 0
        except Exception as exc:
            service.respond(
                request_id,
                False,
                error=f"{exc}\n{traceback.format_exc()}",
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
