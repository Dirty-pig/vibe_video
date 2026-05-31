import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from openai import OpenAI
from faster_whisper import WhisperModel

from mic_stream_stt import (
    INITIAL_PROMPT,
    OUTPUT_FILE,
    cleanup_stale_temp_files,
    has_obvious_speech,
    looks_like_garbage_text,
    record_until_enter,
    safe_unlink,
    wait_for_enter,
)

PROMPT_DIR = Path("prompt_compiler")
RUN_OUTPUT_DIR = Path("prompt_outputs")
SYSTEM_PROMPT_FILE = PROMPT_DIR / "system_prompt.md"
MODES_FILE = PROMPT_DIR / "compiler_modes.json"
SCHEMA_FILE = PROMPT_DIR / "output_schema.json"
PROMPT_ARCHIVE_FILE = RUN_OUTPUT_DIR / "prompt_runs.md"
LATEST_AGENT_PAYLOAD_FILE = RUN_OUTPUT_DIR / "latest_agent_payload.json"

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-v4-flash"
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "sk-430831c27cee4a9d82a3960c53104c9e").strip() or "PASTE_DEEPSEEK_KEY_HERE"
DEFAULT_MODE = "feature"
DEBUG = True
SCHEMA_NAME = "voice_prompt_compiler_output"


def debug_log(step: str, message: str) -> None:
    if DEBUG:
        print(f"[DEBUG] {step}: {message}")


def ensure_run_output_dir() -> None:
    RUN_OUTPUT_DIR.mkdir(exist_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Microphone STT to DeepSeek prompt compiler")
    parser.add_argument("--mic-device", type=int, default=None, help="Microphone device index")
    parser.add_argument("--sample-rate", type=int, default=16000, help="Microphone sample rate")
    parser.add_argument(
        "--max-record-seconds",
        type=float,
        default=600.0,
        help="Maximum recording duration before auto-stop (default: 600)",
    )
    parser.add_argument("--list-devices", action="store_true", help="List audio devices and exit")
    parser.add_argument(
        "--context",
        default="",
        help="Optional extra context sent to the prompt compiler model",
    )
    parser.add_argument(
        "--print-payload-only",
        action="store_true",
        help="Print the latest agent payload path after each run",
    )
    return parser.parse_args()


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def detect_mode(raw_text: str, modes_config: dict[str, Any]) -> str:
    lowered = raw_text.lower().strip()
    if not lowered:
        return DEFAULT_MODE

    priority = modes_config["router"].get("priority", [])
    manual_prefix = modes_config["router"].get("manual_prefix", {})

    for mode in priority:
        for prefix in manual_prefix.get(mode, []):
            normalized_prefix = prefix.lower()
            if lowered.startswith(normalized_prefix):
                debug_log("route", f"matched manual prefix {normalized_prefix!r} -> {mode}")
                return mode

    keyword_map = {
        "debug": ["报错", "错误", "bug", "跑不起来", "为什么不运行", "日志", "依赖冲突", "结果不对"],
        "feature": ["实现", "新增", "功能", "接入", "做一个", "加一个"],
        "refactor": ["重构", "整理结构", "拆分", "降低耦合", "优化结构"],
        "plan": ["规划", "路线", "mvp", "阶段", "怎么做", "方案"],
        "research": ["调研", "对比", "分析", "推荐", "资料"],
        "writing": ["润色", "改写", "总结", "文档", "邮件", "论文"],
    }

    for mode in priority:
        for keyword in keyword_map.get(mode, []):
            if keyword in lowered:
                debug_log("route", f"matched keyword {keyword!r} -> {mode}")
                return mode

    debug_log("route", f"no route match; fallback -> {DEFAULT_MODE}")
    return DEFAULT_MODE


def build_schema_hint(schema: dict[str, Any]) -> dict[str, Any]:
    properties = schema.get("properties", {})
    return {
        "required": schema.get("required", []),
        "mode_enum": properties.get("mode", {}).get("enum", []),
        "confidence_rule": "confidence and agent_input.confidence must be numbers between 0 and 1, never words like low, medium, or high.",
        "extracted_slots_required": properties.get("extracted_slots", {}).get("required", []),
        "agent_input_required": properties.get("agent_input", {}).get("required", []),
    }


def build_user_message(
    raw_text: str,
    mode: str,
    modes_config: dict[str, Any],
    optional_context: str,
    schema: dict[str, Any],
) -> str:
    mode_block = modes_config["modes"][mode]
    prompt_size_policy = modes_config.get("prompt_size_policy", {})
    payload = {
        "task": "Compile the spoken transcript into the required structured output.",
        "selected_mode": mode,
        "mode_goal": mode_block["goal"],
        "mode_template": mode_block["template"],
        "prompt_size_policy": prompt_size_policy,
        "raw_transcript": raw_text,
        "optional_context": optional_context or "",
        "schema_hint": build_schema_hint(schema),
        "requirements": [
            "Return valid JSON only.",
            "Follow the provided JSON schema exactly.",
            "Keep corrections conservative and explicit.",
            "Keep agent_input consistent with compiled_prompt and extracted_slots.",
            "confidence and agent_input.confidence must be numeric values between 0 and 1.",
            "Keep compiled_prompt concise unless the request is genuinely complex.",
            "If information is uncertain, do not invent details; use 【待确认】 or missing_info where needed."
        ]
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise ValueError("Empty model response")

    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start == -1:
            raise ValueError("No JSON object found in model response")

        depth = 0
        in_string = False
        escape = False
        for index, char in enumerate(text[start:], start=start):
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start : index + 1])

        raise ValueError("Incomplete JSON object in model response")


def build_response_format(schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": SCHEMA_NAME,
            "schema": schema,
            "strict": True,
        },
    }


def build_json_object_response_format() -> dict[str, Any]:
    return {"type": "json_object"}


def should_fallback_to_json_object(exc: Exception) -> bool:
    message = str(exc).lower()
    return "response_format type is unavailable" in message or "json_schema" in message


def validate_payload_shape(payload: dict[str, Any], schema: dict[str, Any]) -> None:
    required_fields = schema.get("required", [])
    missing_required = [field for field in required_fields if field not in payload]
    if missing_required:
        raise ValueError(f"Missing required top-level fields: {', '.join(missing_required)}")

    agent_input = payload.get("agent_input")
    if not isinstance(agent_input, dict):
        raise ValueError("agent_input must be an object")

    compiled_prompt = payload.get("compiled_prompt")
    agent_prompt = agent_input.get("prompt")
    if isinstance(compiled_prompt, str) and isinstance(agent_prompt, str):
        if compiled_prompt.strip() and not agent_prompt.strip():
            raise ValueError("agent_input.prompt must not be empty when compiled_prompt is present")

    if payload.get("mode") not in schema["properties"]["mode"].get("enum", []):
        raise ValueError("mode is outside the configured enum")


def normalize_confidence_value(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default

    if isinstance(value, (int, float)):
        return max(0.0, min(float(value), 1.0))

    if isinstance(value, str):
        normalized = value.strip().lower()
        if not normalized:
            return default

        label_map = {
            "low": 0.35,
            "medium": 0.6,
            "high": 0.85,
            "very high": 0.95,
            "very low": 0.15,
            "低": 0.35,
            "中": 0.6,
            "中等": 0.6,
            "高": 0.85,
            "较高": 0.8,
            "较低": 0.3,
        }
        if normalized in label_map:
            return label_map[normalized]

        try:
            return max(0.0, min(float(normalized), 1.0))
        except ValueError:
            return default

    return default


def normalize_corrections_confidence(payload: dict[str, Any]) -> None:
    corrections = payload.get("corrections")
    if not isinstance(corrections, list):
        return

    for item in corrections:
        if isinstance(item, dict):
            item["confidence"] = normalize_confidence_value(item.get("confidence"), default=0.0)


def ensure_agent_input(payload: dict[str, Any]) -> dict[str, Any]:
    agent_input = payload.get("agent_input") or {}
    compiled_prompt = str(payload.get("compiled_prompt", "")).strip()
    extracted_slots = payload.get("extracted_slots") or {}
    mode = str(payload.get("mode") or DEFAULT_MODE)
    missing_info = payload.get("missing_info") or []
    confidence = normalize_confidence_value(payload.get("confidence"), default=0.0)
    normalize_corrections_confidence(payload)

    context_parts = []
    for key in ("task_goal", "current_state", "constraints", "inputs"):
        value = str(extracted_slots.get(key, "")).strip()
        if value:
            context_parts.append(f"{key}: {value}")
    context_summary = " | ".join(context_parts)

    agent_input.setdefault("target", "coding_agent")
    agent_input.setdefault("mode", mode)
    agent_input.setdefault("prompt", compiled_prompt)
    agent_input.setdefault("context_summary", context_summary)
    agent_input.setdefault("missing_info", missing_info)
    agent_input["confidence"] = normalize_confidence_value(agent_input.get("confidence"), default=confidence)

    payload["mode"] = mode
    payload["compiled_prompt"] = compiled_prompt
    payload["missing_info"] = missing_info
    payload["confidence"] = confidence
    payload["agent_input"] = agent_input
    return payload


def append_combined_markdown(
    raw_text: str,
    payload: dict[str, Any],
    language: str,
    probability: float,
    infer_seconds: float,
    api_seconds: float,
) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = (
        f"## Recording {timestamp}\n\n"
        f"- Language: {language} ({probability:.2f})\n"
        f"- STT inference time: {infer_seconds:.2f}s\n"
        f"- Compiler API time: {api_seconds:.2f}s\n"
        f"- Mode: {payload.get('mode', '')}\n"
        f"- Confidence: {float(payload.get('confidence', 0.0)):.2f}\n\n"
        f"### Raw Transcript\n\n{raw_text or 'No speech text recognized.'}\n\n"
        f"### Compiled Prompt\n\n{payload.get('compiled_prompt', '') or 'No compiled prompt.'}\n\n"
        f"### Agent Input\n\n```json\n{json.dumps(payload.get('agent_input', {}), ensure_ascii=False, indent=2)}\n```\n\n"
    )
    with OUTPUT_FILE.open("a", encoding="utf-8") as file:
        file.write(entry)


def append_prompt_archive(raw_text: str, payload: dict[str, Any]) -> None:
    ensure_run_output_dir()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = (
        f"## Prompt Run {timestamp}\n\n"
        f"### Raw Transcript\n\n{raw_text or 'No speech text recognized.'}\n\n"
        f"### Full Payload\n\n```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```\n\n"
    )
    with PROMPT_ARCHIVE_FILE.open("a", encoding="utf-8") as file:
        file.write(entry)


def write_latest_agent_payload(payload: dict[str, Any]) -> None:
    ensure_run_output_dir()
    LATEST_AGENT_PAYLOAD_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def compile_with_deepseek(
    client: OpenAI,
    system_prompt: str,
    user_message: str,
    schema: dict[str, Any],
) -> tuple[dict[str, Any], float]:
    debug_log("api", f"requesting model {DEEPSEEK_MODEL}")
    started = time.perf_counter()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    try:
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=messages,
            response_format=build_response_format(schema),
            stream=False,
            reasoning_effort="high",
        )
    except Exception as exc:
        if not should_fallback_to_json_object(exc):
            raise
        debug_log("api", "json_schema response_format unsupported; retrying with json_object")
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=messages,
            response_format=build_json_object_response_format(),
            stream=False,
            reasoning_effort="high",
        )
    api_seconds = time.perf_counter() - started
    text = response.choices[0].message.content or ""
    debug_log("api", f"received response in {api_seconds:.2f}s")
    payload = extract_json_object(text)
    payload = ensure_agent_input(payload)
    validate_payload_shape(payload, schema)
    return payload, api_seconds


def main() -> int:
    args = parse_args()

    if args.list_devices:
        import sounddevice as sd

        print(sd.query_devices())
        return 0

    if DEEPSEEK_API_KEY == "PASTE_DEEPSEEK_KEY_HERE":
        print(
            "Error: set DEEPSEEK_API_KEY in environment or write it into mic_prompt_deepseek.py.",
            file=sys.stderr,
        )
        return 1

    debug_log("assets", "loading prompt assets")
    system_prompt = load_text(SYSTEM_PROMPT_FILE)
    modes_config = load_json(MODES_FILE)
    schema = load_json(SCHEMA_FILE)
    debug_log("assets", f"loaded schema keys: {', '.join(schema.get('required', []))}")

    cleanup_stale_temp_files()

    debug_log("stt", "loading whisper model")
    load_start = time.perf_counter()
    try:
        stt_model = WhisperModel("small", device="cuda", compute_type="int8_float16")
    except Exception as exc:
        print(f"Error loading STT model: {exc}", file=sys.stderr)
        return 2
    debug_log("stt", f"whisper ready in {time.perf_counter() - load_start:.2f}s")

    debug_log("api", "creating DeepSeek client")
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

    try:
        while True:
            if not wait_for_enter("Press Enter to start recording, or Ctrl+C to exit.\n"):
                print("Exiting.")
                return 0

            debug_log("record", "starting microphone capture")
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

                debug_log("stt", "running transcription")
                infer_start = time.perf_counter()
                segments, info = stt_model.transcribe(
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

            if not raw_text:
                debug_log("pipeline", "empty transcript after cleanup; skipping compiler call")
                append_combined_markdown(
                    raw_text,
                    {
                        "mode": DEFAULT_MODE,
                        "compiled_prompt": "",
                        "confidence": 0.0,
                        "agent_input": {},
                    },
                    info.language,
                    info.language_probability,
                    infer_seconds,
                    0.0,
                )
                continue

            mode = detect_mode(raw_text, modes_config)
            debug_log("route", f"selected mode -> {mode}")
            user_message = build_user_message(raw_text, mode, modes_config, args.context, schema)
            debug_log("prompt", f"user message size -> {len(user_message)} chars")

            try:
                payload, api_seconds = compile_with_deepseek(
                    client,
                    system_prompt,
                    user_message,
                    schema,
                )
            except Exception as exc:
                print(f"Compiler API error: {exc}", file=sys.stderr)
                return 3

            debug_log("archive", "writing payload backups")
            write_latest_agent_payload(payload)
            append_combined_markdown(
                raw_text,
                payload,
                info.language,
                info.language_probability,
                infer_seconds,
                api_seconds,
            )
            append_prompt_archive(raw_text, payload)

            print("Compiled prompt:")
            print(payload.get("compiled_prompt", ""))
            print()
            print(f"Agent payload saved to: {LATEST_AGENT_PAYLOAD_FILE}")
            if args.print_payload_only:
                print(LATEST_AGENT_PAYLOAD_FILE)

    except KeyboardInterrupt:
        print("\nExiting.")
        return 0
    except Exception as exc:
        print(f"Runtime error: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
