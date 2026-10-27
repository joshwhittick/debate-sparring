# Sparring Partner

A local practice tool for answering common objections to veganism under time pressure. Each round has 5 objections. You answer each one by typing or speaking, the AI pushes back once, you reply, and Gemini scores both turns on accuracy, logic, addressing the crux, concision and concession discipline. You also get tips and a model answer.

**Debate practice, not medical or dietary advice.**

## Contents

| Path | What it does |
|---|---|
| `app.py` | FastAPI routes, objection-bank loading and validation, startup checks |
| `judge.py` | Gemini calls (follow-up, judge, transcribe, ping), retries, cost, and the `--selftest` CLI |
| `store.py` / `config.py` | SQLite access; every tunable value (timers, round size, upload limit, prices) |
| `prompts/` | `followup.md`, `judge.md` (first line is `PROMPT_VERSION`, currently v2), `transcribe.md` |
| `static/` | Single-page frontend (`index.html`, `app.js`, `style.css`) |
| `data/objections.json` | Objection bank: 40 items, all `reviewed: false`. The only tracked file in `data/` |
| `data/` (gitignored) | `sparring.db`, `audio/`, plus the review reports `bank_review_2026-10-27.md` and `site_gap_analysis_2026-10-27.md` |
| `tests/` | `test_app.py`, `e2e_stub.py`, `selftest.json`; `fixtures/` holds a gitignored recording |
| `research/carnismdebunked/` | Scraper and local copy of carnismdebunked.com (see Research material) |
| `debate-sparring-mvp-prd.md` | The PRD the app was built from |
| `TIDY_2026-10-27.md` | Tidy log |

## Setup

```
pip install -r requirements.txt
copy .env.example .env      # then add GEMINI_API_KEY
python app.py               # http://127.0.0.1:8000
```

At startup the app validates the objection bank, then checks the key with a one-token call. It exits with "API key rejected" if the key is refused.

| `.env` | Meaning |
|---|---|
| `GEMINI_API_KEY` | Required |
| `GEMINI_MODEL` | Default `gemini-3.1-flash-lite`, the cheapest model that passes the self-test ($0.25 / $1.50 per 1M tokens). `gemini-3.5-flash-lite` also passes. `gemini-2.5-flash-lite` is closed to new users |
| `GEMINI_TIER` | `free` (default) or `paid`. Speak mode is enabled when this is `paid` |
| `VOICE_ON_FREE_TIER` | `1` enables Speak mode on the free tier anyway. Your recordings may then be used and human-reviewed by Google |
| `GEMINI_API_SURFACE` | Optional. Set to `generate_content` if `interactions.create` misbehaves |

Timers, round size, the upload limit and prices are in `config.py`.

## Data and privacy

- Typed answers and voice recordings are sent to the Gemini API.
- **On the free tier, Google may use and human-review content. Deleting locally doesn't delete Google's copy.** For that reason, Speak mode stays disabled unless `GEMINI_TIER=paid` (a billed project) or you opt in with `VOICE_ON_FREE_TIER=1`.
- Recordings are kept under `data/audio/`. You can delete them per answer from the feedback screen; the transcripts and scores are kept.
- `logs/app.log` holds ids, timings, token counts and cost only. It never holds answer text or transcripts.

## Tests

| Command | What it checks | Cost |
|---|---|---|
| `python tests/test_app.py` | Every route, with Gemini stubbed | Free |
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
- TODO:
  - Work through `data/bank_review_2026-10-27.md` and set `reviewed: true` per item. The bank stays a Claude draft until then.
  - Decide which of the 14 items from the gap analysis to add.
  - Rewrite the `tests/selftest.json` answers yourself.
  - Run 20 calibration answers under prompt v2.
  - Measure the cost per round.

## Credit

The idea comes from a remark Jack Symes made on stream ([Jack v Halo - Part II, around 3:15:00](https://www.youtube.com/watch?v=kkw6ozdxli0&t=11700s)). This app is not affiliated with or endorsed by Jack Symes.
