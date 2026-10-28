# debate-sparring

Vegan-debate practice app. Spec: `debate-sparring-mvp-prd.md` (milestones M1-M8, decisions table), kept locally and gitignored along with `TIDY_*.md`; the repo is meant to go public.

- `core.py`: all shared logic (bank, rounds, answers, judging, calibration, summary, history). Raises `CoreError(status, code, **extra)`; put new behaviour here so both front ends get it.
- `app.py`: thin FastAPI routes over `core` (sync `def`), maps `CoreError` to JSON, startup checks.
- `streamlit_app.py`: Streamlit front end over `core`, local or Streamlit Cloud. Root-level `st.secrets` are copied into `os.environ` before `config` imports. No auto-submit on the timer. Public mode (Cloud, non-localhost host, or `PUBLIC_DEPLOY=1`) fails closed: needs `APP_PASSWORD`, hides History and calibration, caps session calls, deletes recordings after judging. Keep new features safe for a shared, public database.
- `judge.py`: Gemini calls (`make_followup`, `judge_answer`, `transcribe`, `ping`), retry budget, cost, and the `--selftest` CLI.
- `store.py`: SQLite, one connection per call.
- `config.py`: every tunable value. `static/` is a vanilla JS single page, mounted at `/static`.
- Voice uses the two-call path: `transcribe`, then judge the text. The one-call path was tested (`tests/spike_one_call_audio.py`, 2026-10-28) and dropped: about 2% cheaper, less predictable latency.
- Re-judging overwrites the verdict but adds to tokens and cost, so total spend stays true. This deviates from the PRD wording on purpose.
- `data/objections.json` is a Claude draft: all 53 items are `reviewed: false` until Josh reads them. The citations were web-checked by agents on 2026-10-28; the open points are in `data/bank_fixes_2026-10-28.md` and `data/gap_shortlist_2026-10-28.md` (gitignored).
- `tests/selftest.json` answers are Claude drafts for Josh to rewrite.
- Never log answer text or transcripts. Never name Jack Symes in the UI.
- Run `python tests/test_app.py` and `python tests/test_streamlit.py` after `core`/backend changes, and `python tests/e2e_stub.py` after `static/` changes.
- `research/carnismdebunked/`: local scrape of carnismdebunked.com, used as reference for the bank. Only the script is tracked; the pages are gitignored. Never paste its wording into `data/objections.json`.
- Remote: `origin` = github.com/joshwhittick/debate-sparring. Never commit absolute local paths; refer to files outside the repo in words.
