"""Debate sparring app, local front end: FastAPI routes over `core`, plus startup checks.

Run: python app.py  (serves http://127.0.0.1:8000)
"""
import logging
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import core
import judge
import store

log = logging.getLogger("sparring.app")


# --- setup ---

def startup_checks():
    if not config.GEMINI_API_KEY:
        sys.exit("GEMINI_API_KEY is not set - copy .env.example to .env and add your key")
    try:
        judge.ping()
    except judge.KeyRejected:
        log.error("startup: API key rejected")
        sys.exit("API key rejected")
    log.info("startup: model=%s tier=%s prompt_version=%s", config.GEMINI_MODEL, config.GEMINI_TIER,
             judge.PROMPT_VERSION)
    log.info("startup: %d pending answers", store.count_pending())


@asynccontextmanager
async def lifespan(_app):
    try:
        core.init()
    except core.BankInvalid:
        sys.exit("Objection bank failed validation - see logs/app.log")
    startup_checks()
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    log.info("request %s %s -> %d in %dms", request.method, request.url.path, response.status_code,
             (time.perf_counter() - start) * 1000)
    return response


@app.exception_handler(core.CoreError)
async def core_error(_request, e):
    return JSONResponse({"error": e.code, **e.extra}, status_code=e.status)


def read_audio(upload):
    """Reads an upload in chunks; returns bytes, None if absent, or core.TOO_LARGE past MAX_UPLOAD_MB."""
    if upload is None:
        return None
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    chunks, size = [], 0
    while chunk := upload.file.read(64 * 1024):
        size += len(chunk)
        if size > limit:
            return core.TOO_LARGE
        chunks.append(chunk)
    return b"".join(chunks)


def upload_mime(upload):
    return (upload.content_type if upload else None) or "audio/webm"


# --- routes ---

@app.get("/")
def index():
    return FileResponse(config.STATIC_DIR / "index.html")


@app.get("/config")
def get_config():
    return core.client_config()


class RoundIn(BaseModel):
    category: str | None = None
    study_mode: bool = False
    shuffle: bool = False


@app.post("/rounds")
def create_round(body: RoundIn):
    return core.new_round(body.category, body.study_mode, body.shuffle)


class SkipIn(BaseModel):
    objection_id: str


@app.post("/rounds/{round_id}/skip")
def skip_objection(round_id: int, body: SkipIn):
    """Objection is null when the bank is exhausted."""
    return {"objection": core.skip(round_id, body.objection_id)}


@app.post("/answers")
def create_answer(round_id: int = Form(...), objection_id: str = Form(...), mode: str = Form(...),
                  text: str | None = Form(None), audio: UploadFile | None = File(None)):
    data = read_audio(audio) if mode == "speak" else None
    return core.start_answer(round_id, objection_id, mode, text, data, upload_mime(audio))


@app.post("/answers/{answer_id}/followup")
def retry_followup(answer_id: int):
    return {"followup_text": core.retry_followup(answer_id)}


@app.post("/answers/{answer_id}/reply")
def create_reply(answer_id: int, mode: str = Form(...), text: str | None = Form(None),
                 audio: UploadFile | None = File(None)):
    data = read_audio(audio) if mode == "speak" else None
    return core.submit_reply(answer_id, mode, text, data, upload_mime(audio))


@app.post("/answers/{answer_id}/rejudge")
def rejudge(answer_id: int):
    return core.rejudge(answer_id)


class CalibrationIn(BaseModel):
    reviewer: str = "josh"
    scores: dict[str, int]
    seeded_error: bool = False


@app.post("/answers/{answer_id}/calibration")
def calibrate(answer_id: int, body: CalibrationIn):
    core.calibrate(answer_id, body.reviewer, body.scores, body.seeded_error)
    return {"ok": True}


@app.delete("/answers/{answer_id}/audio")
def delete_audio(answer_id: int):
    core.delete_audio(answer_id)
    return {"ok": True}


@app.get("/audio/{answer_id}/{turn}")
def get_audio(answer_id: int, turn: str):
    path, mime = core.audio_file(answer_id, turn)
    return FileResponse(path, media_type=mime)


@app.get("/answers/{answer_id}")
def get_answer(answer_id: int):
    return core.get_answer(answer_id)


@app.get("/rounds/{round_id}/summary")
def round_summary(round_id: int):
    return core.round_summary(round_id)


@app.get("/history")
def history():
    return core.history()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
