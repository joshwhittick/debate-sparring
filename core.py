"""Shared app logic for both front ends (FastAPI `app.py` and Streamlit `streamlit_app.py`).

Bank loading, round and answer flow, judging, calibration, summaries and history. Front ends call the
public functions below; a CoreError carries the HTTP status and error code a route should return.
"""
import json
import logging
import random
import time
from logging.handlers import RotatingFileHandler

import config
import judge
import store

log = logging.getLogger("sparring.core")
BANK = {}
TURNS = ("opener", "reply")
TOO_LARGE = object()  # sentinel a front end passes when an upload passed MAX_UPLOAD_MB
AUDIO_EXT = {"audio/webm": "webm", "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav",
             "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/mpeg": "mp3"}
AUDIO_MIME = {ext: mime for mime, ext in reversed(AUDIO_EXT.items())}


class CoreError(Exception):
    def __init__(self, status, code, **extra):
        super().__init__(code)
        self.status, self.code, self.extra = status, code, extra


class BankInvalid(Exception):
    def __init__(self, errors):
        super().__init__(f"{len(errors)} bank error(s)")
        self.errors = errors


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
    """Reads and validates the bank into BANK. Raises BankInvalid."""
    items = json.loads(config.OBJECTIONS_PATH.read_text(encoding="utf-8"))
    errors = validate_bank(items)
    if errors:
        for e in errors:
            log.error("objection bank invalid: %s", e)
        raise BankInvalid(errors)
    unreviewed = sum(1 for i in items if not i.get("reviewed"))
    log.info("objection bank loaded: %d items, %d unreviewed", len(items), unreviewed)
    BANK.clear()
    BANK.update({i["id"]: i for i in items})
    return BANK


def init():
    """Logging, audio dir, bank and database. Raises BankInvalid."""
    setup_logging()
    config.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    load_bank()
    store.init_db()


def speak_enabled():
    return config.GEMINI_TIER == "paid" or config.VOICE_ON_FREE_TIER


def client_config():
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


def objection_out(o):
    return {"id": o["id"], "text": o["text"], "category": o["category"], "difficulty": o["difficulty"]}


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


def require_answer(answer_id):
    row = store.get_answer(answer_id)
    if not row:
        raise CoreError(404, "not_found")
    return row


def run_judge(answer_id):
    """Judges stored turns; updates the row to judged or failed. Returns the fresh row."""
    row = store.get_answer(answer_id)
    obj = BANK[row["objection_id"]]
    transcripts = load_transcripts(row) or {}
    delivery = transcripts.get("delivery")
    started = time.perf_counter()
    try:
        feedback, usage = judge.judge_answer(obj, row["opener_text"] or "", row["followup_text"] or "",
                                             row["reply_text"] or "", delivery=delivery,
                                             spoken={turn: row[f"{turn}_mode"] == "speak" for turn in TURNS})
    except (judge.JudgeError, judge.KeyRejected) as e:
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
    except (judge.JudgeError, judge.KeyRejected) as e:
        store.add_usage(answer_id, getattr(e, "usage", None) or {})
        log.error("followup failed answer_id=%d: %s", answer_id, type(e).__name__)
        return None
    store.add_usage(answer_id, usage)
    store.update_answer(answer_id, followup_text=text)
    return text


def check_turn(mode, text, data):
    """Validates a turn's input. data is audio bytes, None, or TOO_LARGE. Returns (text, audio_bytes)."""
    if mode not in ("type", "speak"):
        raise CoreError(400, "bad_mode")
    if mode == "type":
        text = (text or "").strip()
        if len(text) > config.MAX_ANSWER_CHARS:
            raise CoreError(413, "too_long", detail=f"Answers are limited to {config.MAX_ANSWER_CHARS} characters")
        return text, None
    if not speak_enabled():
        raise CoreError(403, "speak_disabled", detail="Voice needs GEMINI_TIER=paid")
    if data is None:
        raise CoreError(400, "no_audio")
    if data is TOO_LARGE or len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise CoreError(413, "too_large")
    if not data:
        raise CoreError(400, "no_audio")
    return None, data


def check_budget():
    """Raises 429 daily_limit once today's (UTC) Gemini spend reaches DAILY_SPEND_LIMIT_USD."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if store.spend_since(today) >= config.DAILY_SPEND_LIMIT_USD:
        raise CoreError(429, "daily_limit", detail="Today's spending limit is reached; try again tomorrow")


def audio_file(answer_id, turn):
    """Path and mime of a stored recording. Raises CoreError 404."""
    row = store.get_answer(answer_id)
    if turn not in TURNS or not row or not row[f"{turn}_audio_path"]:
        raise CoreError(404, "not_found")
    path = config.AUDIO_DIR / row[f"{turn}_audio_path"]
    if not path.exists():
        raise CoreError(404, "not_found")
    return path, AUDIO_MIME.get(path.suffix.lstrip("."), "audio/webm")


def transcribe_turn(answer_id, turn, data, mime="audio/webm"):
    """Saves the recording, transcribes it and returns the transcript ('' if unintelligible)."""
    mime = (mime or "audio/webm").split(";")[0].strip().lower()
    path = config.AUDIO_DIR / f"{answer_id}_{turn}.{AUDIO_EXT.get(mime, 'webm')}"
    path.write_bytes(data)
    store.update_answer(answer_id, **{f"{turn}_audio_path": path.name})
    try:
        row = store.get_answer(answer_id)
        followup = row["followup_text"] if turn == "reply" else None
        result, usage = judge.transcribe(data, mime, objection=BANK[row["objection_id"]], followup_text=followup)
    except (judge.JudgeError, judge.KeyRejected) as e:
        store.add_usage(answer_id, getattr(e, "usage", None) or {})
        log.error("transcribe failed answer_id=%d turn=%s: %s", answer_id, turn, type(e).__name__)
        result = {"transcript": "", "delivery": None}
    else:
        store.add_usage(answer_id, usage)
    save_transcript(answer_id, store.get_answer(answer_id), turn, result)
    return (result.get("transcript") or "").strip()


def mean(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 2) if values else None


# --- flow ---

def new_round(category=None, study_mode=False, shuffle=False):
    category = category or None
    if category and category not in config.CATEGORIES:
        raise CoreError(400, "bad_category")
    picked = pick_objections(category, shuffle=shuffle)
    round_id = store.create_round(category, study_mode, [o["id"] for o in picked], shuffle)
    log.info("round created round_id=%d n=%d shuffle=%s", round_id, len(picked), shuffle)
    return {"round_id": round_id, "objections": [objection_out(o) for o in picked]}


def skip(round_id, objection_id):
    """Swaps an unanswered objection for a fresh one; returns None when the bank is exhausted."""
    rnd = store.get_round(round_id)
    if not rnd or objection_id not in rnd["objection_ids"]:
        raise CoreError(404, "not_found")
    if any(a["objection_id"] == objection_id for a in store.round_answers(round_id)):
        raise CoreError(409, "already_answered")
    ids, skipped = rnd["objection_ids"], rnd["skipped_ids"] + [objection_id]
    remaining = [i for i in ids if i != objection_id]
    hard = sum(BANK[i]["difficulty"] == 3 for i in remaining)
    picked = pick_objections(rnd["category_filter"], n=1, shuffle=bool(rnd["shuffle"]), exclude=set(ids) | set(skipped),
                             hard=hard)
    new = picked[0] if picked else None
    ids = [new["id"] if (i == objection_id and new) else i for i in ids if i != objection_id or new]
    store.set_round_objections(round_id, ids, skipped)
    log.info("objection skipped round_id=%d replaced=%s", round_id, bool(new))
    return objection_out(new) if new else None


def start_answer(round_id, objection_id, mode, text=None, data=None, mime="audio/webm"):
    """Saves the opener (transcribing a recording) and gets the pushback."""
    rnd = store.get_round(round_id)
    if not rnd or objection_id not in rnd["objection_ids"]:
        raise CoreError(404, "not_found")
    opener_text, data = check_turn(mode, text, data)
    check_budget()
    answer_id = store.insert_answer(round_id, objection_id, mode, opener_text)
    if answer_id is None:
        raise CoreError(409, "duplicate")
    out = {"answer_id": answer_id}
    if data is not None:
        opener_text = transcribe_turn(answer_id, "opener", data, mime)
        store.update_answer(answer_id, opener_text=opener_text)
        out["opener_transcript"] = opener_text
    followup = generate_followup(answer_id)
    if followup is None:
        raise CoreError(502, "followup_failed", answer_id=answer_id)
    return {**out, "followup_text": followup}


def retry_followup(answer_id):
    row = require_answer(answer_id)
    if row["followup_text"]:
        return row["followup_text"]
    check_budget()
    followup = generate_followup(answer_id)
    if followup is None:
        raise CoreError(502, "followup_failed", answer_id=answer_id)
    return followup


def judged_out(row):
    out = answer_out(row)
    return {"status": row["status"], "feedback": out["feedback"], "answer": out}


def submit_reply(answer_id, mode, text=None, data=None, mime="audio/webm"):
    """Saves the reply (transcribing a recording) and judges both turns."""
    row = require_answer(answer_id)
    if not row["followup_text"]:
        raise CoreError(409, "no_followup")
    if row["reply_mode"]:
        raise CoreError(409, "duplicate")
    reply_text, data = check_turn(mode, text, data)
    check_budget()
    store.update_answer(answer_id, reply_mode=mode, reply_text=reply_text, status="pending")
    if data is not None:
        store.update_answer(answer_id, reply_text=transcribe_turn(answer_id, "reply", data, mime))
    return judged_out(run_judge(answer_id))


def rejudge(answer_id):
    row = require_answer(answer_id)
    if not row["followup_text"] or not row["reply_mode"]:
        raise CoreError(409, "incomplete", detail="Both turns must be saved before judging")
    check_budget()
    store.delete_calibrations(answer_id)
    return judged_out(run_judge(answer_id))


def calibrate(answer_id, reviewer, scores, seeded_error=False):
    require_answer(answer_id)
    if reviewer not in ("josh", "second"):
        raise CoreError(400, "bad_reviewer")
    if set(scores) != set(store.DIMENSIONS) or not all(1 <= v <= 5 for v in scores.values()):
        raise CoreError(400, "bad_scores", detail="Five scores, each 1-5")
    store.save_calibration(answer_id, reviewer, scores, seeded_error)
    log.info("calibration saved answer_id=%d reviewer=%s seeded=%s", answer_id, reviewer, seeded_error)


def delete_audio(answer_id):
    row = require_answer(answer_id)
    for turn in TURNS:
        if row[f"{turn}_audio_path"]:
            (config.AUDIO_DIR / row[f"{turn}_audio_path"]).unlink(missing_ok=True)
    store.update_answer(answer_id, opener_audio_path=None, reply_audio_path=None)
    log.info("recordings deleted answer_id=%d", answer_id)


def get_answer(answer_id):
    return answer_out(require_answer(answer_id))


def round_summary(round_id):
    rnd = store.get_round(round_id)
    if not rnd:
        raise CoreError(404, "not_found")
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
