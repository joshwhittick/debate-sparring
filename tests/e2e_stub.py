"""Browser end-to-end check against the real backend with Gemini stubbed (no API key, no cost).

Plays one study-mode round of typed answers to the summary, opens History, and fails on any
page error or console error. Needs: pip install playwright && playwright install chromium.

Run: python tests/e2e_stub.py   (exit 0 on pass)
"""
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="sparring_e2e_"))
config.DATA_DIR, config.AUDIO_DIR, config.DB_PATH, config.LOG_DIR = TMP, TMP / "audio", TMP / "e2e.db", TMP / "logs"
config.GEMINI_API_KEY = "stub"

import judge  # noqa: E402

USAGE = {"model": "stub", "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.0001}
FEEDBACK = {
    "scores": {"accuracy": 4, "logic": 3, "crux": 4, "concision": 2, "concession_discipline": 5},
    "overall": 4,
    "flags": [{"type": "missed_point", "turn": "opener", "detail": "Stub flag <b>escaped</b>"}],
    "tips": ["Lead with the crux.", "Cut the preamble."],
    "model_answer": "Stub model answer.",
    "notes": {"tone": "Calm", "persuasiveness": "Fair", "delivery": None},
}
judge.ping = lambda model=None: None
judge.make_followup = lambda o, t, model=None: ("Stub comeback: what about the strongest form?", dict(USAGE))
judge.judge_answer = lambda o, a, f, r, delivery=None, spoken=None, model=None: (json.loads(json.dumps(FEEDBACK)), dict(USAGE))

import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

import app as app_module  # noqa: E402

PORT = 5058
BASE = f"http://127.0.0.1:{PORT}"


def serve():
    server = uvicorn.Server(uvicorn.Config(app_module.app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        if server.started:
            return server
        time.sleep(0.1)
    raise RuntimeError("server did not start")


def main():
    server = serve()
    errors, results = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: m.type == "error" and errors.append(f"console: {m.text}"))
        page.goto(BASE + "/#start")
        page.check("#study")
        page.check("#shuffle")
        page.get_by_role("button", name="Start round").click()
        # Skip once on the first objection: the text must change and the round stays the same length.
        first = page.locator(".objection-text").inner_text()
        page.get_by_role("button", name="Skip", exact=True).click()
        page.wait_for_function(
            "t => document.querySelector('.objection-text') && document.querySelector('.objection-text').textContent !== t",
            arg=first,
        )
        results.append(("skip changed the objection", page.locator(".objection-text").inner_text() != first))
        results.append(("progress is 1 of round size", f"Objection 1 of {config.ROUND_SIZE}" in page.inner_text("body")))
        for i in range(config.ROUND_SIZE):
            page.get_by_label("Your answer").fill(f"Opener {i}")
            page.get_by_role("button", name="Submit").click()
            page.get_by_text("Stub comeback").wait_for()
            if i == 0:
                results.append(("skip hidden after opener", page.get_by_role("button", name="Skip", exact=True).count() == 0))
            page.get_by_label("Your answer").fill(f"Reply {i}")
            page.get_by_role("button", name="Submit").click()
            page.get_by_text("Lead with the crux.").wait_for()
            results.append((f"feedback {i} has medical line", "not medical or dietary advice" in page.inner_text("body")))
            last = i == config.ROUND_SIZE - 1
            page.get_by_role("button", name="See round summary" if last else "Next objection").last.click()
        page.get_by_text("New round").wait_for()
        body = page.inner_text("body")
        results.append(("summary reached", "New round" in body))
        results.append(("flag text escaped", page.locator("b", has_text="escaped").count() == 0))
        page.goto(BASE + "/#history")
        page.get_by_text("Category trend").wait_for()
        results.append(("history shows spend", "$" in page.inner_text("body")))
        browser.close()
    server.should_exit = True
    shutil.rmtree(TMP, ignore_errors=True)
    for name, ok in results:
        print(("PASS " if ok else "FAIL ") + name)
    for e in errors:
        print("FAIL " + e)
    failed = [n for n, ok in results if not ok] + errors
    print(f"\n{len(failed)} failure(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
