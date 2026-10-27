"""Offline Streamlit tests: drives streamlit_app.py with AppTest; Gemini is stubbed, the database is a temp file.

Run: python tests/test_streamlit.py   (exit 0 on pass)
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="sparring_st_test_"))
config.DATA_DIR = TMP
config.AUDIO_DIR = TMP / "audio"
config.DB_PATH = TMP / "test.db"
config.LOG_DIR = TMP / "logs"
config.GEMINI_API_KEY = "test-key"
config.GEMINI_TIER = "free"
config.VOICE_ON_FREE_TIER = False
config.APP_PASSWORD = ""

import judge  # noqa: E402
import store  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

USAGE = {"model": "stub", "input_tokens": 100, "output_tokens": 50, "cost_usd": 0.001}
FEEDBACK = {
    "scores": {"accuracy": 4, "logic": 3, "crux": 4, "concision": 3, "concession_discipline": 4},
    "overall": 4,
    "flags": [{"type": "unsupported_claim", "turn": "reply", "detail": "stub"}],
    "tips": ["a", "b"],
    "model_answer": "stub answer",
    "notes": {"tone": "ok", "persuasiveness": "ok", "delivery": None},
}
judge.ping = lambda model=None: None
judge.make_followup = lambda objection, opener, model=None: ("But what about X?", dict(USAGE))
judge.judge_answer = lambda *a, **k: (json.loads(json.dumps(FEEDBACK)), dict(USAGE))

FAILURES = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


def button(at, label):
    return next(b for b in at.button if b.label == label)


def texts(at):
    return " ".join([m.value for m in at.markdown] + [i.value for i in at.info] + [c.value for c in at.caption])


def main():
    at = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=30)
    at.run()
    check("start screen renders without error", not at.exception and any(b.label == "Start round" for b in at.button))

    at.checkbox[0].check()  # study mode: no countdown fragment
    button(at, "Start round").click().run()
    check("round created", store.get_round(1) is not None and len(store.get_round(1)["objection_ids"]) == 5)
    check("objection screen shows 1 of 5", "Objection 1 of 5" in texts(at))

    first = store.get_round(1)["objection_ids"][0]
    button(at, "Skip this objection").click().run()
    check("skip swaps the objection", first in store.get_round(1)["skipped_ids"])

    at.text_area[0].input("my opener").run()
    button(at, "Submit").click().run()
    check("opener saved and pushback shown", "But what about X?" in texts(at))

    at.text_area[0].input("my reply").run()
    button(at, "Submit").click().run()
    check("feedback shows overall metric", any(m.label == "Overall" and m.value == "4/5" for m in at.metric))
    check("flags grouped by turn", "**Reply**" in texts(at))
    check("feedback has medical line", "not medical or dietary advice" in texts(at))
    check("answer judged in store", store.get_answer(1)["status"] == "judged")

    button(at, "Next objection").click().run()
    check("next objection is 2 of 5", "Objection 2 of 5" in texts(at))
    button(at, "End round").click().run()
    check("round summary shows answered and unanswered",
          any(s.value == "Round summary" for s in at.subheader) and len(at.dataframe) == 1)

    at = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=30)
    at.run()
    at.checkbox[0].check()
    at.checkbox[2].check()  # calibration mode
    button(at, "Start round").click().run()
    at.text_area[0].input("opener").run()
    button(at, "Submit").click().run()
    at.text_area[0].input("reply").run()
    button(at, "Submit").click().run()
    check("calibration form shown before feedback",
          len(at.slider) == 5 and not any(m.label == "Overall" for m in at.metric))
    at.slider[0].set_value(5)
    at.button[0].click().run()  # form submit
    answer_id = max(r["id"] for r in store.query("SELECT id FROM answers"))
    check("calibration saved, then feedback shown",
          store.has_calibration(answer_id) and any(m.label == "Overall" for m in at.metric))

    at = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=30)
    at.run()
    at.sidebar.radio[0].set_value("History").run()
    check("history renders", not at.exception and any(m.label == "Total spend" for m in at.metric))
    check("no exceptions", not at.exception)

    print(f"\n{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
