# debate-sparring

Local vegan-debate practice app. Spec: `debate-sparring-mvp-prd.md` (milestones M1-M8, decisions table).

- `app.py`: FastAPI routes, bank loading and validation, startup checks. Routes are sync `def`.
- `judge.py`: Gemini calls (`make_followup`, `judge_answer`, `transcribe`, `ping`), retry budget, cost, and the `--selftest` CLI.
- `store.py`: SQLite, one connection per call.
- `config.py`: every tunable value. `static/` is a vanilla JS single page, mounted at `/static`.
- Voice uses the two-call path: `transcribe`, then judge the text. The one-call spike (M7) still needs a real recording.
- Re-judging overwrites the verdict but adds to tokens and cost, so total spend stays true. This deviates from the PRD wording on purpose.
- `data/objections.json` is a Claude draft: all items are `reviewed: false` and the citations were not web-checked.
- `tests/selftest.json` answers are Claude drafts for Josh to rewrite.
- Never log answer text or transcripts. Never name Jack Symes in the UI.
- Run `python tests/test_app.py` after backend changes, and `python tests/e2e_stub.py` after frontend changes.
- `research/carnismdebunked/`: local scrape of carnismdebunked.com, used as reference for the bank. Only the script is tracked; the pages are gitignored. Never paste its wording into `data/objections.json`.
