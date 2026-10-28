"""Local speech-to-text with faster-whisper, used when TRANSCRIBER=whisper.

Audio never leaves the server: only the transcript goes on to Gemini, like a typed answer.
The model downloads from Hugging Face on first use (small.en is about 460 MB) and is then cached.

    transcribe(audio_bytes, mime, objection=None, followup_text=None) -> ({"transcript", "delivery"}, usage)

Same contract as judge.transcribe. delivery is always None: Whisper hears words, not tone.
"""
import io
import logging
import threading
import time

import config
from judge import JudgeError

log = logging.getLogger("sparring.whisper")
_model = None
_lock = threading.Lock()


def get_model():
    """Loads the model once per process (thread-safe)."""
    global _model
    with _lock:
        if _model is None:
            from faster_whisper import WhisperModel
            started = time.perf_counter()
            _model = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8")
            log.info("whisper model %s loaded in %dms", config.WHISPER_MODEL, (time.perf_counter() - started) * 1000)
    return _model


def _prompt(objection, followup_text):
    """Vocabulary hint for Whisper: the objection (and pushback) the speaker is answering."""
    parts = [(objection or {}).get("text"), followup_text]
    return " ".join(p for p in parts if p) or None


def transcribe(audio_bytes, mime, objection=None, followup_text=None, model=None):
    if not audio_bytes:
        raise JudgeError("transcribe: no audio data")
    started = time.perf_counter()
    try:
        segments, _info = get_model().transcribe(io.BytesIO(audio_bytes), language="en", vad_filter=True,
                                                 initial_prompt=_prompt(objection, followup_text))
        transcript = " ".join(s.text.strip() for s in segments).strip()
    except Exception as e:  # decode or model failure; core treats it like a failed Gemini transcription
        raise JudgeError(f"whisper: {type(e).__name__}") from e
    log.info("whisper transcribed %d bytes (%s) in %dms", len(audio_bytes), mime,
             (time.perf_counter() - started) * 1000)
    usage = {"model": f"whisper-{config.WHISPER_MODEL}", "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    return {"transcript": transcript, "delivery": None}, usage
