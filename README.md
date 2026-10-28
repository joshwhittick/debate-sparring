# Sparring Partner

A practice tool for answering common objections to veganism under time pressure. Each round has 5 objections. You answer each one by typing or speaking, the AI pushes back once, you reply, and Gemini scores both turns on accuracy, logic, addressing the crux, concision and concession discipline. You also get tips and a model answer.

**Live app: https://vegan-debate.streamlit.app/** (password protected).

**Debate practice, not medical or dietary advice.**

Two front ends share one core (`core.py`) and one database:

| Front end | Run | Notes |
|---|---|---|
| Local web app (FastAPI + vanilla JS) | `python app.py` -> http://127.0.0.1:8000 | Timers auto-submit after the grace period; records WebM in the browser |
| Streamlit | `streamlit run streamlit_app.py` -> http://localhost:8501 | Runs locally or on Streamlit Community Cloud. The countdown shows "Time's up" but does not auto-submit; records WAV via `st.audio_input`. Public mode off localhost (see below) |

## Contents

| Path | What it does |
|---|---|
| `core.py` | Shared logic: bank loading and validation, round/answer flow, judging, calibration, summaries, history. Raises `CoreError(status, code)` |
| `app.py` | FastAPI routes over `core`, startup checks (exits on a bad bank or rejected key) |
| `streamlit_app.py` | Streamlit front end over `core`: Practice and History pages |
| `.streamlit/secrets.toml.example` | Template for Streamlit Cloud secrets (`secrets.toml` itself is gitignored) |
| `.streamlit/config.toml` | Hides tracebacks from visitors, caps uploads at 5 MB, turns off usage stats |
| `judge.py` | Gemini calls (follow-up, judge, transcribe, ping), retries, cost, and the `--selftest` CLI |
| `store.py` / `config.py` | SQLite access; every tunable value (timers, round size, upload limit, prices) |
| `prompts/` | `followup.md`, `judge.md` (first line is `PROMPT_VERSION`, currently v2), `transcribe.md` |
| `static/` | Single-page frontend (`index.html`, `app.js`, `style.css`) |
| `data/objections.json` | Objection bank: 40 items, all `reviewed: false`. The only tracked file in `data/` |
| `data/` (gitignored) | `sparring.db`, `audio/`, plus the review reports `bank_review_2026-10-27.md` and `site_gap_analysis_2026-10-27.md` |
| `tests/` | `test_app.py` (FastAPI routes), `test_streamlit.py` (Streamlit `AppTest`), `e2e_stub.py` (browser), `selftest.json`; `fixtures/` holds a gitignored recording |
| `requirements.txt`, `.env.example` | Python packages; env template (copy to `.env`, gitignored) |
| `logs/` (gitignored) | `app.log`, rotating; ids, timings and cost only |
| `CLAUDE.md` | Agent context |
| `research/carnismdebunked/` | Scraper and local copy of carnismdebunked.com (see Research material) |
| `debate-sparring-mvp-prd.md`, `TIDY_*.md` (gitignored) | The PRD the app was built from, and tidy logs; kept locally only |

## Setup

```
pip install -r requirements.txt
copy .env.example .env      # then add GEMINI_API_KEY
python app.py               # http://127.0.0.1:8000
streamlit run streamlit_app.py   # or the Streamlit version, http://localhost:8501
```

At startup either front end validates the objection bank, then checks the key with a one-token call. `app.py` exits with "API key rejected" if the key is refused; Streamlit shows the error on the page.

### Streamlit Community Cloud

1. The repo is on GitHub: `joshwhittick/debate-sparring`.
2. On share.streamlit.io, create an app from the repo with main file `streamlit_app.py`. Not `app.py`: that starts FastAPI, which never sees the Cloud secrets and exits with "GEMINI_API_KEY is not set".
3. Paste the keys from `.streamlit/secrets.toml.example` into the app's Secrets, with your key, a random `APP_PASSWORD` of 20+ characters and a `DAILY_SPEND_LIMIT_USD`. Root-level secrets are copied into the environment, so `config.py` reads them like `.env` values.
4. Set a budget alert or quota cap on the Gemini key in Google Cloud as a second limit.

The Streamlit app runs in **public mode** on Streamlit Cloud, on any host other than localhost, or with `PUBLIC_DEPLOY=1`. It fails closed:

- It refuses to run without `APP_PASSWORD`. A wrong password costs a 1 s delay.
- Typed answers are capped at 2,000 characters, and each session gets 30 Gemini-spending actions.
- Once today's (UTC) spend on that day's answers reaches `DAILY_SPEND_LIMIT_USD`, both front ends stop making new calls.
- History and calibration are hidden, because every visitor shares one database.
- Recordings are deleted once an answer is judged.
- Errors show a generic message, never a traceback.

On Cloud the SQLite database and logs live on the container's disk and are lost when the app restarts or redeploys. History and calibration are only durable locally.

| `.env` | Meaning |
|---|---|
| `GEMINI_API_KEY` | Required |
| `GEMINI_MODEL` | Default `gemini-3.1-flash-lite`, the cheapest model that passes the self-test ($0.25 / $1.50 per 1M tokens). `gemini-3.5-flash-lite` also passes. `gemini-2.5-flash-lite` is closed to new users |
| `GEMINI_TIER` | `free` (default) or `paid`. Speak mode is enabled when this is `paid` |
| `VOICE_ON_FREE_TIER` | `1` enables Speak mode on the free tier anyway. Your recordings may then be used and human-reviewed by Google |
| `GEMINI_API_SURFACE` | Optional. Set to `generate_content` if `interactions.create` misbehaves |
| `APP_PASSWORD` | Streamlit only. Required in public mode (the app won't run without it); optional on localhost |
| `PUBLIC_DEPLOY` | Streamlit only. Unset: public mode off localhost. `1` forces it on locally, `0` off (ignored on Streamlit Cloud) |
| `DAILY_SPEND_LIMIT_USD` | Default `1.0`. No new Gemini calls once today's (UTC) spend reaches it |

Timers, round size, the upload and answer-length limits, the session call limit, output token caps and prices are in `config.py`.

## Data and privacy

- Typed answers and voice recordings are sent to the Gemini API.
- **On the free tier, Google may use and human-review content. Deleting locally doesn't delete Google's copy.** For that reason, Speak mode stays disabled unless `GEMINI_TIER=paid` (a billed project) or you opt in with `VOICE_ON_FREE_TIER=1`.
- Recordings are kept under `data/audio/` (`.webm` from the local app, `.wav` from Streamlit). You can delete them per answer from the feedback screen; the transcripts and scores are kept. In Streamlit public mode they are deleted as soon as the answer is judged.
- The start screen tells visitors that answers go to the Gemini API and may be used by Google on the free tier.
- `logs/app.log` holds ids, timings, token counts and cost only. It never holds answer text or transcripts.

## Tests

| Command | What it checks | Cost |
|---|---|---|
| `python tests/test_app.py` | Every route, with Gemini stubbed | Free |
| `python tests/test_streamlit.py` | The Streamlit app via `AppTest`: round, skip, feedback, calibration, summary, history, double-submit recovery, and public mode (password gate, hidden pages, session and daily limits, answer cap); Gemini stubbed | Free |
| `python tests/e2e_stub.py` | A full browser round with Gemini stubbed (needs Playwright) | Free |
| `python judge.py --selftest [--model X] [--repeat 3]` | Live judge sanity: strong beats weak, empty scores low, wrong answer flagged, injection flagged | About 18 judge calls |
| `python judge.py --selftest --audio [PATH]` | Adds the voice transcription case (default `tests/fixtures/sample.webm`) | One more call |

## Calibration

Turn on Calibration mode on the start screen. On each answer you enter your own 5 scores, and tick "deliberate error" if you seeded one, before the AI's feedback is shown. History reports agreement within ±1 and the seeded-error hit rate, counted for the current `PROMPT_VERSION` only (first line of `prompts/judge.md`). Bump the version whenever you change the prompt. Pass bar: at least 80% within ±1 over 20 answers, and all 5 seeded errors flagged.

## Cost

Measured cost per round of 5: **not yet measured** (fill in after the first live round; History shows the total spend).

## Research material

`research/carnismdebunked/` holds a local copy of [carnismdebunked.com](https://www.carnismdebunked.com/): 12 content pages, about 25,800 words. It's reference material for reviewing and extending `data/objections.json`. Don't copy its wording into the bank.

```
python research/carnismdebunked/scrape_carnismdebunked.py fetch   # sitemap -> raw/*.html; resumable, 3 s between requests
python research/carnismdebunked/scrape_carnismdebunked.py parse   # raw/ -> pages/*.md (offline)
```

- The most useful pages are `general-ethical.md`, `health-nutrition.md`, `environmental.md` and `fallacies.md`. Each keeps 96-99% of the page's text.
- `resources.md` and `your-country.md` lose about half their text, which was link grids and buttons.
- Only the script is tracked in git. `raw/`, `pages/` and `logs/` are gitignored because the content is third-party.
- The scrape ran on 2026-10-27 over a direct connection, at Josh's instruction.
- `data/site_gap_analysis_2026-10-27.md` (gitignored) compares the four key pages with the bank. They raise 69 objections and the bank misses 24. The report drafts 14 new items and 8 key-point additions, none of them applied yet.

## Status

- 2026-10-27: MVP built. Voice uses two calls (transcribe, then judge). The default model changed to `gemini-3.1-flash-lite`, which passes the full self-test including `--audio` on a real recording (that run cost $0.017).
- 2026-10-27: Streamlit front end added. Shared logic moved from `app.py` into `core.py`; the FastAPI routes and responses are unchanged (`test_app.py` and `e2e_stub.py` pass).
- 2026-10-27: hardened for a public deploy: fail-closed public mode, answer and output caps, session and daily spend limits, generic errors, wrapper-tag escaping for unclosed tags. The PRD and tidy logs left git.
- 2026-10-27: repo made public and deployed to Streamlit Community Cloud at https://vegan-debate.streamlit.app/ (main file `streamlit_app.py`).
- TODO:
  - Work through `data/bank_review_2026-10-27.md` and set `reviewed: true` per item. The bank stays a Claude draft until then.
  - Decide which of the 14 items from the gap analysis to add.
  - Rewrite the `tests/selftest.json` answers yourself.
  - Run 20 calibration answers under prompt v2.
  - Measure the cost per round.

## Credit

The idea comes from a remark Jack Symes made on stream ([Jack v Halo - Part II, around 3:15:00](https://www.youtube.com/watch?v=kkw6ozdxli0&t=11700s)). This app is not affiliated with or endorsed by Jack Symes.
