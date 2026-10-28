"""Live local check of the Whisper transcriber on a real recording. No Gemini calls.

Run: python tests/check_whisper.py [PATH] [--model base.en]   (default PATH tests/fixtures/sample.webm)
Prints model load time, transcription time and the transcript. The first run downloads the model.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

MIME = {".webm": "audio/webm", ".wav": "audio/wav", ".ogg": "audio/ogg", ".mp3": "audio/mpeg", ".m4a": "audio/mp4"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default=str(config.TESTS_DIR / "fixtures" / "sample.webm"))
    ap.add_argument("--model", default=config.WHISPER_MODEL)
    args = ap.parse_args()
    config.WHISPER_MODEL = args.model
    import whisper_local

    path = Path(args.path)
    if not path.exists():
        print(f"missing recording: {path}")
        return 1
    t0 = time.perf_counter()
    whisper_local.get_model()
    t1 = time.perf_counter()
    result, usage = whisper_local.transcribe(path.read_bytes(), MIME.get(path.suffix.lower(), "audio/webm"))
    t2 = time.perf_counter()
    words = len(result["transcript"].split())
    print(f"model {usage['model']}: load {t1 - t0:.1f}s, transcribe {t2 - t1:.1f}s, {words} words")
    print(result["transcript"])
    return 0 if words else 1


if __name__ == "__main__":
    sys.exit(main())
