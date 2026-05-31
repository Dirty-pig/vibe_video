import argparse
import sys
import time
from pathlib import Path

from faster_whisper import WhisperModel


SUPPORTED_EXTENSIONS = {
    ".wav",
    ".wave",
    ".mp3",
    ".m4a",
    ".flac",
    ".aac",
    ".ogg",
    ".opus",
    ".wma",
    ".mp4",
    ".mkv",
    ".mov",
    ".webm",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transcribe audio/video file to text with faster-whisper."
    )
    parser.add_argument("audio_path", help="Path to input audio/video file")
    return parser.parse_args()


def main() -> int:
    start_time = time.perf_counter()
    args = parse_args()

    audio_path = Path(args.audio_path)
    if not audio_path.exists():
        print(f"Error: file not found -> {audio_path}", file=sys.stderr)
        return 1
    if audio_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        exts = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        print(f"Error: unsupported file extension. Supported: {exts}", file=sys.stderr)
        return 1

    load_start = time.perf_counter()
    try:
        model = WhisperModel("small", device="cuda", compute_type="int8_float16")
    except Exception as exc:
        print(f"Error loading model: {exc}", file=sys.stderr)
        return 2
    load_seconds = time.perf_counter() - load_start

    try:
        segments, info = model.transcribe(
            str(audio_path),
            language="zh",
            task="transcribe",
            beam_size=8,
            condition_on_previous_text=True,
            initial_prompt="以下是普通话语音识别结果，请输出准确文本并保留标点。",
        )
    except Exception as exc:
        print(f"Error during transcription: {exc}", file=sys.stderr)
        return 3

    infer_start = time.perf_counter()
    text_parts = []
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        text_parts.append(text)
    infer_seconds = time.perf_counter() - infer_start

    merged_text = "".join(text_parts)
    total_seconds = time.perf_counter() - start_time

    if merged_text:
        print(merged_text)
    else:
        print("No speech text recognized.")

    print(f"Detected language: {info.language} ({info.language_probability:.2f})")
    print(f"Model load time: {load_seconds:.2f}s")
    print(f"Inference time: {infer_seconds:.2f}s")
    print(f"Total time: {total_seconds:.2f}s")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
