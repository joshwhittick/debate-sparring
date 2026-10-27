"""Debate sparring app: FastAPI routes, objection bank loading and startup checks.

Run: python app.py  (serves http://127.0.0.1:8000)
"""
import json
import logging
import random
import sys
import time
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import judge
import store

log = logging.getLogger("sparring.app")
BANK = {}


# --- setup ---

def setup_logging():
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger("sparring")
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    file_handler = RotatingFileHandler(config.LOG_DIR / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    console = logging.StreamHandler()
    for h in (file_handler, console):
        h.setFormatter(fmt)
        root.addHandler(h)


def validate_bank(items):
    """Returns a list of error strings; empty means valid."""
    errors, seen = [], set()
    for i, item in enumerate(items):
        oid = item.get("id", f"<index {i}>")
        if oid in seen:
            errors.append(f"{oid}: duplicate id")
        seen.add(oid)
        if item.get("category") not in config.CATEGORIES:
            errors.append(f"{oid}: invalid category {item.get('category')!r}")
        if item.get("difficulty") not in (1, 2, 3):
            errors.append(f"{oid}: difficulty must be 1-3")
        if not item.get("key_points"):
            errors.append(f"{oid}: key_points empty")
        if not (item.get("text") or "").strip() or not (item.get("strongest_form") or "").strip():
            errors.append(f"{oid}: text and strongest_form required")
        if item.get("category") in config.SOURCED_CATEGORIES and not item.get("sources"):
            errors.append(f"{oid}: sources required for {item.get('category')}")
    return errors


def load_bank():
    items = json.loads(config.OBJECTIONS_PATH.read_text(encoding="utf-8"))
    errors = validate_bank(items)
    if errors:
        for e in errors:
            log.error("objection bank invalid: %s", e)
        sys.exit("Objection bank failed validation - see logs/app.log")
    unreviewed = sum(1 for i in items if not i.get("reviewed"))
    log.info("objection bank loaded: %d items, %d unreviewed", len(items), unreviewed)
    return {i["id"]: i for i in items}


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
    setup_logging()
    config.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    BANK.update(load_bank())
    store.init_db()
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


def err(status, code, **extra):
    return JSONResponse({"error": code, **extra}, status_code=status)


def speak_enabled():
    return config.GEMINI_TIER == "paid" or config.VOICE_ON_FREE_TIER


# --- helpers ---

def pick_objections(category, n=config.ROUND_SIZE, shuffle=False, exclude=(), hard=0):
    """Least-answered first, random within ties (shuffle: fully random), at most
    MAX_DIFFICULTY_3_PER_ROUND hard items counting the `hard` already in the round."""
    pool = [o for o in BANK.values() if (not category or o["category"] == category) and o["id"] not in exclude]
    random.shuffle(pool)
    if not shuffle:
        counts = store.times_answered()
        pool.sort(key=lambda o: counts.get(o["id"], 0))
    picked = []
    for o in pool:
        if o["difficulty"] == 3:
            if hard >= config.MAX_DIFFICULTY_3_PER_ROUND:
                continue
            hard += 1
        picked.append(o)
        if len(picked) == n:
            break
    return picked


def read_audio(upload):
    """Reads an upload in chunks; returns bytes, or None once it passes MAX_UPLOAD_MB."""
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    chunks, size = [], 0
    while chunk := upload.file.read(64 * 1024):
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def audio_path(answer_id, turn):
    return config.AUDIO_DIR / f"{answer_id}_{turn}.webm"


def load_transcripts(row):
    return json.loads(row["transcript_json"]) if row.get("transcript_json") else None


def save_transcript(answer_id, row, turn, result):
    data = load_transcripts(row) or {"opener": None, "reply": None, "delivery": {"opener": None, "reply": None}}
    data[turn] = result.get("transcript") or ""
    data["delivery"][turn] = result.get("delivery")
    store.update_answer(answer_id, transcript_json=json.dumps(data))
    return data


def answer_out(row):
    obj = BANK.get(row["objection_id"], {})
    transcripts = load_transcripts(row)
    return {
        "id": row["id"],
        "round_id": row["round_id"],
        "objection_id": row["objection_id"],
        "objection_text": obj.get("text"),
        "category": obj.get("category"),
        "status": row["status"],
        "opener_mode": row["opener_mode"],
        "opener_text": row["opener_text"],
        "followup_text": row["followup_text"],
        "reply_mode": row["reply_mode"],
        "reply_text": row["reply_text"],
        "has_opener_audio": bool(row["opener_audio_path"]),
        "has_reply_audio": bool(row["reply_audio_path"]),
        "transcripts": {"opener": transcripts.get("opener"), "reply": transcripts.get("reply")} if transcripts else None,
        "feedback": json.loads(row["feedback_json"]) if row["feedback_json"] else None,
        "overall": row["overall"],
        "cost_usd": row["cost_usd"],
        "calibrated": store.has_calibration(row["id"]),
    }


def run_judge(answer_id):
    """Judges stored turns; updates the row to judged or failed. Returns the fresh row."""
    row = store.get_answer(answer_id)
    obj = BANK[row["objection_id"]]
    transcripts = load_transcripts(row) or {}
    delivery = transcripts.get("delivery")
    started = time.perf_counter()
    try:
        feedback, usage = judge.judge_answer(obj, row["opener_text"] or "", row["followup_text"] or "",
                                             row["reply_text"] or "", delivery=delivery)
    except judge.JudgeError as e:
        store.add_usage(answer_id, getattr(e, "usage", None) or {})
        log.error("judge failed answer_id=%d: %s", answer_id, type(e).__name__)
        store.update_answer(answer_id, status="failed")
        return store.get_answer(answer_id)
    # Re-judging replaces the verdict; tokens and cost accumulate so total spend stays true.
    store.add_usage(answer_id, usage)
    store.update_answer(answer_id, status="judged", feedback_json=json.dumps(feedback), overall=feedback["overall"],
                        model=usage["model"], prompt_version=judge.PROMPT_VERSION)
    log.info("judged answer_id=%d overall=%d in %dms", answer_id, feedback["overall"],
             (time.perf_counter() - started) * 1000)
    return store.get_answer(answer_id)


def generate_followup(answer_id):
    """Returns followup text, or None on failure."""
    row = store.get_answer(answer_id)
    try:
        text, usage = judge.make_followup(BANK[row["objection_id"]], row["opener_text"] or "")
    except judge.JudgeError as e:
        store.add_usage(answer_id, getattr(e, "usage", None) or {})
        log.error("followup failed answer_id=%d: %s", answer_id, type(e).__name__)
        return None
    store.add_usage(answer_id, usage)
    store.update_answer(answer_id, followup_text=text)
    return text


def take_turn_input(mode, text, audio):
    """Validates a turn's input. Returns (text, audio_bytes, error_response)."""
    if mode not in ("type", "speak"):
        return None, None, err(400, "bad_mode")
    if mode == "type":
        return (text or "").strip(), None, None
    if not speak_enabled():
        return None, None, err(403, "speak_disabled", detail="Voice needs GEMINI_TIER=paid")
    if audio is None:
        return None, None, err(400, "no_audio")
    data = read_audio(audio)
    if data is None:
        return None, None, err(413, "too_large")
    if not data:
        return None, None, err(400, "no_audio")
    return None, data, None


def transcribe_turn(answer_id, turn, data):
    """Saves the recording, transcribes it and returns the transcript ('' if unintelligible)."""
    path = audio_path(answer_id, turn)
    path.write_bytes(data)
    store.update_answer(answer_id, **{f"{turn}_audio_path": path.name})
    try:
        result, usage = judge.transcribe(data, "audio/webm")
    except judge.JudgeError as e:
        store.add_usage(answer_id, getattr(e, "usage", None) or {})
        log.error("transcribe failed answer_id=%d turn=%s: %s", answer_id, turn, type(e).__name__)
        result = {"transcript": "", "delivery": None}
    else:
        store.add_usage(answer_id, usage)
    save_transcript(answer_id, store.get_answer(answer_id), turn, result)
    return (result.get("transcript") or "").strip()


# --- routes ---

@app.get("/")
def index():
    return FileResponse(config.STATIC_DIR / "index.html")


@app.get("/config")
def get_config():
    return {
        "round_size": config.ROUND_SIZE,
        "timers": config.TIMERS,
        "grace_s": config.GRACE_S,
        "tier": config.GEMINI_TIER,
        "speak_enabled": speak_enabled(),
        "max_upload_mb": config.MAX_UPLOAD_MB,
        "categories": config.CATEGORIES,
        "clip_url": config.STREAM_CLIP_URL,
    }


class RoundIn(BaseModel):
    category: str | None = None
    study_mode: bool = False
    shuffle: bool = False


def objection_out(o):
    return {"id": o["id"], "text": o["text"], "category": o["category"], "difficulty": o["difficulty"]}


@app.post("/rounds")
def create_round(body: RoundIn):
    category = body.category or None
    if category and category not in config.CATEGORIES:
        return err(400, "bad_category")
    picked = pick_objections(category, shuffle=body.shuffle)
    round_id = store.create_round(category, body.study_mode, [o["id"] for o in picked], body.shuffle)
    log.info("round created round_id=%d n=%d shuffle=%s", round_id, len(picked), body.shuffle)
    return {"round_id": round_id, "objections": [objection_out(o) for o in picked]}


class SkipIn(BaseModel):
    objection_id: str


@app.post("/rounds/{round_id}/skip")
def skip_objection(round_id: int, body: SkipIn):
    """Swaps an unanswered objection for a fresh one; objection is null when the bank is exhausted."""
    rnd = store.get_round(round_id)
    if not rnd or body.objection_id not in rnd["objection_ids"]:
        return err(404, "not_found")
    if any(a["objection_id"] == body.objection_id for a in store.round_answers(round_id)):
        return err(409, "already_answered")
    ids, skipped = rnd["objection_ids"], rnd["skipped_ids"] + [body.objection_id]
    remaining = [i for i in ids if i != body.objection_id]
    hard = sum(BANK[i]["difficulty"] == 3 for i in remaining)
    picked = pick_objections(rnd["category_filter"], n=1, shuffle=bool(rnd["shuffle"]), exclude=set(ids) | set(skipped),
                             hard=hard)
    new = picked[0] if picked else None
    ids = [new["id"] if (i == body.objection_id and new) else i for i in ids if i != body.objection_id or new]
    store.set_round_objections(round_id, ids, skipped)
    log.info("objection skipped round_id=%d replaced=%s", round_id, bool(new))
    return {"objection": objection_out(new) if new else None}


@app.post("/answers")
def create_answer(round_id: int = Form(...), objection_id: str = Form(...), mode: str = Form(...),
                        text: str | None = Form(None), audio: UploadFile | None = File(None)):
    rnd = store.get_round(round_id)
    if not rnd or objection_id not in rnd["objection_ids"]:
        return err(404, "not_found")
    opener_text, data, error = take_turn_input(mode, text, audio)
    if error:
        return error
    answer_id = store.insert_answer(round_id, objection_id, mode, opener_text)
    if answer_id is None:
        return err(409, "duplicate")
    out = {"answer_id": answer_id}
    if data is not None:
        opener_text = transcribe_turn(answer_id, "opener", data)
        store.update_answer(answer_id, opener_text=opener_text)
        out["opener_transcript"] = opener_text
    followup = generate_followup(answer_id)
    if followup is None:
        return err(502, "followup_failed", answer_id=answer_id)
    return {**out, "followup_text": followup}


@app.post("/answers/{answer_id}/followup")
def retry_followup(answer_id: int):
    row = store.get_answer(answer_id)
    if not row:
        return err(404, "not_found")
    if row["followup_text"]:
        return {"followup_text": row["followup_text"]}
    followup = generate_followup(answer_id)
    if followup is None:
        return err(502, "followup_failed", answer_id=answer_id)
    return {"followup_text": followup}


@app.post("/answers/{answer_id}/reply")
def create_reply(answer_id: int, mode: str = Form(...), text: str | None = Form(None),
                       audio: UploadFile | None = File(None)):
    row = store.get_answer(answer_id)
    if not row:
        return err(404, "not_found")
    if not row["followup_text"]:
        return err(409, "no_followup")
    if row["reply_mode"]:
        return err(409, "duplicate")
    reply_text, data, error = take_turn_input(mode, text, audio)
    if error:
        return error
    store.update_answer(answer_id, reply_mode=mode, reply_text=reply_text, status="pending")
    if data is not None:
        store.update_answer(answer_id, reply_text=transcribe_turn(answer_id, "reply", data))
    row = run_judge(answer_id)
    out = answer_out(row)
    return {"status": row["status"], "feedback": out["feedback"], "answer": out}


@app.post("/answers/{answer_id}/rejudge")
def rejudge(answer_id: int):
    row = store.get_answer(answer_id)
    if not row:
        return err(404, "not_found")
    if not row["followup_text"] or not row["reply_mode"]:
        return err(409, "incomplete", detail="Both turns must be saved before judging")
    store.delete_calibrations(answer_id)
    row = run_judge(answer_id)
    out = answer_out(row)
    return {"status": row["status"], "feedback": out["feedback"], "answer": out}


class CalibrationIn(BaseModel):
    reviewer: str = "josh"
    scores: dict[str, int]
    seeded_error: bool = False


@app.post("/answers/{answer_id}/calibration")
def calibrate(answer_id: int, body: CalibrationIn):
    row = store.get_answer(answer_id)
    if not row:
        return err(404, "not_found")
    if body.reviewer not in ("josh", "second"):
        return err(400, "bad_reviewer")
    if set(body.scores) != set(store.DIMENSIONS) or not all(1 <= v <= 5 for v in body.scores.values()):
        return err(400, "bad_scores", detail="Five scores, each 1-5")
    store.save_calibration(answer_id, body.reviewer, body.scores, body.seeded_error)
    log.info("calibration saved answer_id=%d reviewer=%s seeded=%s", answer_id, body.reviewer, body.seeded_error)
    return {"ok": True}


@app.delete("/answers/{answer_id}/audio")
def delete_audio(answer_id: int):
    row = store.get_answer(answer_id)
    if not row:
        return err(404, "not_found")
    for turn in ("opener", "reply"):
        audio_path(answer_id, turn).unlink(missing_ok=True)
    store.update_answer(answer_id, opener_audio_path=None, reply_audio_path=None)
    log.info("recordings deleted answer_id=%d", answer_id)
    return {"ok": True}


@app.get("/audio/{answer_id}/{turn}")
def get_audio(answer_id: int, turn: str):
    row = store.get_answer(answer_id)
    if turn not in ("opener", "reply") or not row or not row[f"{turn}_audio_path"]:
        return err(404, "not_found")
    path = audio_path(answer_id, turn)
    if not path.exists():
        return err(404, "not_found")
    return FileResponse(path, media_type="audio/webm")


@app.get("/answers/{answer_id}")
def get_answer(answer_id: int):
    row = store.get_answer(answer_id)
    return answer_out(row) if row else err(404, "not_found")


def mean(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 2) if values else None


@app.get("/rounds/{round_id}/summary")
def round_summary(round_id: int):
    rnd = store.get_round(round_id)
    if not rnd:
        return err(404, "not_found")
    by_obj = {a["objection_id"]: a for a in store.round_answers(round_id)}
    feedbacks = [json.loads(a["feedback_json"]) for a in by_obj.values() if a["status"] == "judged"]
    dims = {d: mean([f["scores"][d] for f in feedbacks]) for d in store.DIMENSIONS}

    per_cat = {}
    for a in by_obj.values():
        if a["status"] == "judged":
            per_cat.setdefault(BANK[a["objection_id"]]["category"], []).append(a["overall"])
    counts = store.times_answered()
    answered_per_cat = {c: sum(n for oid, n in counts.items() if BANK.get(oid, {}).get("category") == c)
                        for c in per_cat}
    weakest = min(per_cat, key=lambda c: (mean(per_cat[c]), answered_per_cat[c])) if per_cat else None

    objections = []
    for oid in rnd["objection_ids"]:
        a = by_obj.get(oid)
        objections.append({"answer_id": a["id"] if a else None, "objection_id": oid, "text": BANK[oid]["text"],
                           "category": BANK[oid]["category"], "overall": a["overall"] if a else None,
                           "status": a["status"] if a else "unanswered"})
    return {"round_id": round_id, "dimension_means": dims, "weakest_category": weakest, "objections": objections}


@app.get("/history")
def history():
    per_cat = {}
    for a in store.judged_in_rounds(store.last_round_ids(10)):
        cat = BANK.get(a["objection_id"], {}).get("category")
        if cat:
            per_cat.setdefault(cat, []).append(a["overall"])
    trend = [{"category": c, "mean_overall": mean(per_cat.get(c, [])), "n": len(per_cat.get(c, []))}
             for c in config.CATEGORIES]
    pending = [{"answer_id": a["id"], "status": a["status"],
                "objection_text": BANK.get(a["objection_id"], {}).get("text")} for a in store.unjudged()]
    return {
        "rounds": store.list_rounds(),
        "category_trend": trend,
        "calibration": store.calibration_stats(judge.PROMPT_VERSION),
        "prompt_version": judge.PROMPT_VERSION,
        "spend_usd": round(store.total_spend(), 4),
        "pending": pending,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
