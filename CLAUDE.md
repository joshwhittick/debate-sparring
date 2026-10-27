# debate-sparring

Local vegan-debate practice app. Spec: `debate-sparring-mvp-prd.md` (milestones M1-M8, decisions table).

- `core.py`: all shared logic (bank, rounds, answers, judging, calibration, summary, history). Raises `CoreError(status, code, **extra)`; put new behaviour here so both front ends get it.
- `app.py`: thin FastAPI routes over `core` (sync `def`), maps `CoreError` to JSON, startup checks.
- `streamlit_app.py`: Streamlit front end over `core`, local or Streamlit Cloud. Root-level `st.secrets` are copied into `os.environ` before `config` imports. `APP_PASSWORD` gates it. No auto-submit on the timer.
- `judge.py`: Gemini calls (`make_followup`, `judge_answer`, `transcribe`, `ping`), retry budget, cost, and the `--selftest` CLI.
- `store.py`: SQLite, one connection per call.
- `config.py`: every tunable value. `static/` is a vanilla JS single page, mounted at `/static`.
- Voice uses the two-call path: `transcribe`, then judge the text. The one-call spike (M7) still needs a real recording.
- Re-judging overwrites the verdict but adds to tokens and cost, so total spend stays true. This deviates from the PRD wording on purpose.
- `data/objections.json` is a Claude draft: all items are `reviewed: false` and the citations were not web-checked.
- `tests/selftest.json` answers are Claude drafts for Josh to rewrite.
- Never log answer text or transcripts. Never name Jack Symes in the UI.
- Run `python tests/test_app.py` and `python tests/test_streamlit.py` after `core`/backend changes, and `python tests/e2e_stub.py` after `static/` changes.
- `research/carnismdebunked/`: local scrape of carnismdebunked.com, used as reference for the bank. Only the script is tracked; the pages are gitignored. Never paste its wording into `data/objections.json`.
