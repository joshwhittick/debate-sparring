"""SQLite persistence: one connection per call, WAL mode, tables created at startup."""
import json
import sqlite3
from datetime import datetime, timezone

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    category_filter TEXT,
    study_mode INTEGER NOT NULL DEFAULT 0,
    objection_ids TEXT NOT NULL DEFAULT '[]',
    shuffle INTEGER NOT NULL DEFAULT 0,
    skipped_ids TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL REFERENCES rounds(id),
    objection_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    opener_mode TEXT,
    opener_text TEXT,
    opener_audio_path TEXT,
    followup_text TEXT,
    reply_mode TEXT,
    reply_text TEXT,
    reply_audio_path TEXT,
    transcript_json TEXT,
    feedback_json TEXT,
    overall INTEGER,
    model TEXT,
    prompt_version TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(round_id, objection_id)
);
CREATE TABLE IF NOT EXISTS calibrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    answer_id INTEGER NOT NULL REFERENCES answers(id),
    reviewer TEXT NOT NULL CHECK (reviewer IN ('josh', 'second')),
    scores_json TEXT NOT NULL,
    seeded_error INTEGER NOT NULL DEFAULT 0,
    flagged INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(answer_id, reviewer)
);
"""

DIMENSIONS = ["accuracy", "logic", "crux", "concision", "concession_discipline"]


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_conn():
    conn = sqlite3.connect(config.DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Columns added after the first release; ALTER keeps existing databases working.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(rounds)")}
        if "shuffle" not in cols:
            conn.execute("ALTER TABLE rounds ADD COLUMN shuffle INTEGER NOT NULL DEFAULT 0")
        if "skipped_ids" not in cols:
            conn.execute("ALTER TABLE rounds ADD COLUMN skipped_ids TEXT NOT NULL DEFAULT '[]'")


def query(sql, params=()):
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def query_one(sql, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql, params=()):
    conn = get_conn()
    try:
        with conn:
            cur = conn.execute(sql, params)
            return cur.lastrowid
    finally:
        conn.close()


# --- rounds ---

def create_round(category_filter, study_mode, objection_ids, shuffle=False):
    return execute(
        "INSERT INTO rounds (started_at, category_filter, study_mode, objection_ids, shuffle) VALUES (?, ?, ?, ?, ?)",
        (now_iso(), category_filter, int(bool(study_mode)), json.dumps(objection_ids), int(bool(shuffle))),
    )


def get_round(round_id):
    row = query_one("SELECT * FROM rounds WHERE id = ?", (round_id,))
    if row:
        row["objection_ids"] = json.loads(row["objection_ids"])
        row["skipped_ids"] = json.loads(row["skipped_ids"])
    return row


def set_round_objections(round_id, objection_ids, skipped_ids):
    execute("UPDATE rounds SET objection_ids = ?, skipped_ids = ? WHERE id = ?",
            (json.dumps(objection_ids), json.dumps(skipped_ids), round_id))


def times_answered():
    """objection_id -> number of saved answers."""
    rows = query("SELECT objection_id, COUNT(*) AS n FROM answers GROUP BY objection_id")
    return {r["objection_id"]: r["n"] for r in rows}


# --- answers ---

def insert_answer(round_id, objection_id, opener_mode, opener_text):
    """Returns the new id, or None if (round_id, objection_id) already exists."""
    try:
        return execute(
            "INSERT INTO answers (round_id, objection_id, opener_mode, opener_text, created_at) VALUES (?, ?, ?, ?, ?)",
            (round_id, objection_id, opener_mode, opener_text, now_iso()),
        )
    except sqlite3.IntegrityError:
        return None


def get_answer(answer_id):
    return query_one("SELECT * FROM answers WHERE id = ?", (answer_id,))


def update_answer(answer_id, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    execute(f"UPDATE answers SET {cols} WHERE id = ?", (*fields.values(), answer_id))


def add_usage(answer_id, usage):
    execute(
        "UPDATE answers SET input_tokens = input_tokens + ?, output_tokens = output_tokens + ?, "
        "cost_usd = cost_usd + ? WHERE id = ?",
        (usage.get("input_tokens", 0), usage.get("output_tokens", 0), usage.get("cost_usd", 0.0), answer_id),
    )


def count_pending():
    return query_one("SELECT COUNT(*) AS n FROM answers WHERE status = 'pending'")["n"]


def round_answers(round_id):
    return query("SELECT * FROM answers WHERE round_id = ? ORDER BY id", (round_id,))


# --- calibration ---

def save_calibration(answer_id, reviewer, scores, seeded_error):
    execute(
        "INSERT INTO calibrations (answer_id, reviewer, scores_json, seeded_error, created_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(answer_id, reviewer) DO UPDATE SET scores_json = excluded.scores_json, "
        "seeded_error = excluded.seeded_error, created_at = excluded.created_at",
        (answer_id, reviewer, json.dumps(scores), int(bool(seeded_error)), now_iso()),
    )


def delete_calibrations(answer_id):
    execute("DELETE FROM calibrations WHERE answer_id = ?", (answer_id,))


def has_calibration(answer_id):
    return query_one("SELECT 1 AS x FROM calibrations WHERE answer_id = ?", (answer_id,)) is not None


def seeded_flagged(feedback):
    """A seeded-error answer counts as flagged if the AI raised any error-type flag (PRD M5)."""
    error_types = {"fallacy", "unsupported_claim", "missed_point"}
    return any(f.get("type") in error_types for f in (feedback or {}).get("flags", []))


def calibration_stats(prompt_version):
    """Per reviewer: dimension-scores within +-1, and seeded errors flagged, current prompt_version only."""
    rows = query(
        "SELECT c.reviewer, c.scores_json, c.seeded_error, a.feedback_json FROM calibrations c "
        "JOIN answers a ON a.id = c.answer_id WHERE a.status = 'judged' AND a.prompt_version = ?",
        (prompt_version,),
    )
    stats = {}
    for r in rows:
        s = stats.setdefault(r["reviewer"], {"reviewer": r["reviewer"], "prompt_version": prompt_version,
                                             "within": 0, "total": 0, "seeded_flagged": 0, "seeded_total": 0,
                                             "answers": 0})
        human = json.loads(r["scores_json"])
        feedback = json.loads(r["feedback_json"])
        ai = feedback.get("scores", {})
        s["answers"] += 1
        for d in DIMENSIONS:
            if d in human and d in ai:
                s["total"] += 1
                s["within"] += int(abs(int(human[d]) - int(ai[d])) <= 1)
        if r["seeded_error"]:
            s["seeded_total"] += 1
            s["seeded_flagged"] += int(seeded_flagged(feedback))
    for s in stats.values():
        s["pct"] = round(100 * s["within"] / s["total"], 1) if s["total"] else None
    return sorted(stats.values(), key=lambda s: s["reviewer"] != "josh")


# --- history ---

def list_rounds():
    return query(
        "SELECT r.id, r.started_at, r.category_filter, r.study_mode, "
        "COUNT(a.id) AS answered, ROUND(AVG(a.overall), 2) AS mean_overall "
        "FROM rounds r LEFT JOIN answers a ON a.round_id = r.id "
        "GROUP BY r.id ORDER BY r.id DESC"
    )


def last_round_ids(n=10):
    return [r["id"] for r in query("SELECT id FROM rounds ORDER BY id DESC LIMIT ?", (n,))]


def judged_in_rounds(round_ids):
    if not round_ids:
        return []
    marks = ",".join("?" * len(round_ids))
    return query(f"SELECT objection_id, overall FROM answers WHERE status = 'judged' AND round_id IN ({marks})",
                 tuple(round_ids))


def total_spend():
    return query_one("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM answers")["s"]


def spend_since(iso_ts):
    return query_one("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM answers WHERE created_at >= ?", (iso_ts,))["s"]


def unjudged():
    return query("SELECT id, objection_id, status FROM answers WHERE status IN ('pending', 'failed') ORDER BY id")
