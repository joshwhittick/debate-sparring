"""Offline route tests: Gemini is stubbed, the database is a temp file.

Run: python tests/test_app.py   (exit 0 on pass)
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="sparring_test_"))
config.DATA_DIR = TMP
config.AUDIO_DIR = TMP / "audio"
config.DB_PATH = TMP / "test.db"
config.LOG_DIR = TMP / "logs"
config.GEMINI_API_KEY = "test-key"
config.GEMINI_TIER = "paid"
config.VOICE_ON_FREE_TIER = False

import judge  # noqa: E402
import store  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

USAGE = {"model": "stub", "input_tokens": 100, "output_tokens": 50, "cost_usd": 0.001}
FEEDBACK = {
    "scores": {"accuracy": 4, "logic": 3, "crux": 4, "concision": 3, "concession_discipline": 4},
    "overall": 4,
    "flags": [{"type": "unsupported_claim", "turn": "reply", "detail": "stub"}],
    "tips": ["a", "b"],
    "model_answer": "stub answer",
    "notes": {"tone": "ok", "persuasiveness": "ok", "delivery": None},
}
STATE = {"judge_fails": False}


def fake_judge(objection, opener, followup, reply, delivery=None, spoken=None, model=None):
    STATE["last_spoken"] = spoken
    if STATE["judge_fails"]:
        raise judge.JudgeError("stub failure")
    return json.loads(json.dumps(FEEDBACK)), dict(USAGE)


judge.ping = lambda model=None: None
judge.make_followup = lambda objection, opener, model=None: ("But what about X?", dict(USAGE))
judge.judge_answer = fake_judge
def fake_transcribe(data, mime, objection=None, followup_text=None, model=None):
    STATE["transcribe_ctx"] = (objection or {}).get("id"), followup_text
    return {"transcript": "spoken words", "delivery": "steady"}, dict(USAGE)


judge.transcribe = fake_transcribe

import app as app_module  # noqa: E402

FAILURES = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


def play_answer(client, round_id, objection_id, mode="type"):
    if mode == "speak":
        files = {"audio": ("a.webm", b"\x1a\x45\xdf\xa3fakewebm", "audio/webm")}
        r = client.post("/answers", data={"round_id": round_id, "objection_id": objection_id, "mode": "speak"},
                        files=files)
    else:
        r = client.post("/answers", data={"round_id": round_id, "objection_id": objection_id, "mode": "type",
                                          "text": "my opener"})
    return r


def main():
    with TestClient(app_module.app) as client:
        cfg = client.get("/config").json()
        check("config speak_enabled when paid", cfg["speak_enabled"] is True)
        check("index served", client.get("/").status_code == 200)

        r = client.post("/rounds", json={"category": None, "study_mode": False}).json()
        rid, objs = r["round_id"], r["objections"]
        check("round has 5 objections", len(objs) == config.ROUND_SIZE)
        check("no key_points leaked", all("key_points" not in o for o in objs))
        check("at most 2 difficulty-3", sum(o["difficulty"] == 3 for o in objs) <= 2)

        first = play_answer(client, rid, objs[0]["id"])
        check("opener 200", first.status_code == 200)
        aid = first.json()["answer_id"]
        check("followup returned", first.json()["followup_text"] == "But what about X?")
        check("duplicate opener 409", play_answer(client, rid, objs[0]["id"]).status_code == 409)

        rep = client.post(f"/answers/{aid}/reply", data={"mode": "type", "text": "my reply"}).json()
        check("reply judged", rep["status"] == "judged" and rep["feedback"]["overall"] == 4)
        check("cost accumulated", abs(rep["answer"]["cost_usd"] - 0.002) < 1e-9)
        check("duplicate reply 409",
              client.post(f"/answers/{aid}/reply", data={"mode": "type", "text": "x"}).status_code == 409)

        # Speak turn: audio saved, transcribed, deletable.
        sp = play_answer(client, rid, objs[1]["id"], mode="speak").json()
        sid = sp["answer_id"]
        check("speak opener transcript", sp.get("opener_transcript") == "spoken words")
        files = {"audio": ("b.webm", b"reply-audio", "audio/webm")}
        sr = client.post(f"/answers/{sid}/reply", data={"mode": "speak"}, files=files).json()
        check("speak reply judged", sr["status"] == "judged" and sr["answer"]["has_reply_audio"])
        check("transcriber gets objection and pushback", STATE["transcribe_ctx"] == (objs[1]["id"], "But what about X?"))
        check("judge told both turns spoken", STATE["last_spoken"] == {"opener": True, "reply": True})
        check("audio streams", client.get(f"/audio/{sid}/opener").status_code == 200)
        check("speak without file 400",
              client.post("/answers", data={"round_id": rid, "objection_id": objs[2]["id"],
                                            "mode": "speak"}).status_code == 400)
        big = {"audio": ("big.webm", b"0" * (config.MAX_UPLOAD_MB * 1024 * 1024 + 1), "audio/webm")}
        check("oversize 413", client.post("/answers", data={"round_id": rid, "objection_id": objs[2]["id"],
                                                            "mode": "speak"}, files=big).status_code == 413)
        before = store.get_answer(sid)
        check("recordings recorded before delete", bool(before["opener_audio_path"] and before["reply_audio_path"])
              and all((config.AUDIO_DIR / before[f"{t}_audio_path"]).exists() for t in ("opener", "reply")))
        check("delete audio 200", client.delete(f"/answers/{sid}/audio").status_code == 200)
        row = store.get_answer(sid)
        check("delete removes the files from disk",
              not any((config.AUDIO_DIR / before[f"{t}_audio_path"]).exists() for t in ("opener", "reply")))
        check("delete nulls both *_audio_path columns",
              row["opener_audio_path"] is None and row["reply_audio_path"] is None)
        check("delete keeps transcript, scores and cost",
              json.loads(row["transcript_json"])["reply"] == "spoken words" and row["overall"] == 4
              and row["cost_usd"] > 0)
        deleted_lines = [ln for ln in (config.LOG_DIR / "app.log").read_text(encoding="utf-8").splitlines()
                         if "recordings deleted" in ln]
        check("delete is logged by id only", len(deleted_lines) == 1 and f"answer_id={sid}" in deleted_lines[0]
              and "spoken words" not in deleted_lines[0] and "steady" not in deleted_lines[0])
        after = client.get(f"/answers/{sid}").json()
        check("audio deleted", not after["has_opener_audio"] and not after["has_reply_audio"]
              and not list(config.AUDIO_DIR.glob(f"{sid}_*")))
        check("transcripts kept", after["transcripts"]["reply"] == "spoken words" and after["overall"] == 4)

        # Failed judge then rejudge.
        fid = play_answer(client, rid, objs[2]["id"]).json()["answer_id"]
        STATE["judge_fails"] = True
        fr = client.post(f"/answers/{fid}/reply", data={"mode": "type", "text": "r"}).json()
        check("failed judge saved", fr["status"] == "failed")
        STATE["judge_fails"] = False
        check("rejudge works", client.post(f"/answers/{fid}/rejudge").json()["status"] == "judged")

        # Calibration: josh within 1 on all 5, seeded error flagged.
        scores = {"accuracy": 5, "logic": 3, "crux": 3, "concision": 1, "concession_discipline": 4}
        check("calibration saved", client.post(f"/answers/{aid}/calibration",
                                               json={"reviewer": "josh", "scores": scores,
                                                     "seeded_error": True}).json() == {"ok": True})
        check("bad calibration 400", client.post(f"/answers/{aid}/calibration",
                                                 json={"reviewer": "josh", "scores": {"accuracy": 9}}).status_code == 400)

        for o in objs[3:]:
            a = play_answer(client, rid, o["id"]).json()["answer_id"]
            client.post(f"/answers/{a}/reply", data={"mode": "type", "text": "r"})
        summ = client.get(f"/rounds/{rid}/summary").json()
        check("summary has 5 scored", sum(o["overall"] is not None for o in summ["objections"]) == 5)
        check("summary dimension means", summ["dimension_means"]["accuracy"] == 4)
        check("weakest category set", summ["weakest_category"] in config.CATEGORIES)

        hist = client.get("/history").json()
        cal = hist["calibration"][0]
        check("calibration stat 4 of 5 within", (cal["within"], cal["total"]) == (4, 5))
        check("seeded flagged 1 of 1", (cal["seeded_flagged"], cal["seeded_total"]) == (1, 1))
        check("spend recorded", hist["spend_usd"] > 0)
        check("rejudge clears calibration", True if client.post(f"/answers/{aid}/rejudge").status_code == 200
              and client.get("/history").json()["calibration"] == [] else False)

        # Shuffle and skip on a fresh round.
        r2 = client.post("/rounds", json={"category": "health", "shuffle": True}).json()
        check("shuffle round has 5", len(r2["objections"]) == config.ROUND_SIZE)
        ids2 = [o["id"] for o in r2["objections"]]
        sk = client.post(f"/rounds/{r2['round_id']}/skip", json={"objection_id": ids2[0]}).json()
        new_id = sk["objection"]["id"]
        check("skip gives a fresh health objection", new_id not in ids2 and new_id.startswith("health"))
        summ2 = client.get(f"/rounds/{r2['round_id']}/summary").json()
        check("summary reflects the swap", [o["objection_id"] for o in summ2["objections"]] == [new_id] + ids2[1:])
        check("skipped id can't be answered", play_answer(client, r2["round_id"], ids2[0]).status_code == 404)
        play_answer(client, r2["round_id"], ids2[1])
        check("answered objection can't be skipped",
              client.post(f"/rounds/{r2['round_id']}/skip", json={"objection_id": ids2[1]}).status_code == 409)
        # 8 health items: 5 in round + 1 skipped already; skip until the bank runs dry.
        last = None
        for _ in range(4):
            cur = client.get(f"/rounds/{r2['round_id']}/summary").json()["objections"]
            target = next(o["objection_id"] for o in cur if o["status"] == "unanswered")
            last = client.post(f"/rounds/{r2['round_id']}/skip", json={"objection_id": target}).json()
        check("exhausted bank shrinks round", last["objection"] is None)

        config.GEMINI_TIER = "free"
        check("config speak_enabled false on free tier", client.get("/config").json()["speak_enabled"] is False)
        check("speak blocked on free tier",
              client.post("/answers", data={"round_id": rid, "objection_id": objs[0]["id"], "mode": "speak"},
                          files={"audio": ("a.webm", b"x", "audio/webm")}).status_code == 403)

    log_text = (config.LOG_DIR / "app.log").read_text(encoding="utf-8")
    check("log has no answer text", "my opener" not in log_text and "spoken words" not in log_text)
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
