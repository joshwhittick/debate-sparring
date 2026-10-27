# Sparring Partner

A local practice tool for answering common objections to veganism under time pressure. Each round has 5 objections. You answer each one by typing or speaking, the AI pushes back once, you reply, and Gemini scores both turns on accuracy, logic, addressing the crux, concision and concession discipline. You also get tips and a model answer.

**Debate practice, not medical or dietary advice.**

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
| `GEMINI_MODEL` | Code default `gemini-3.8-flash`. `gemini-3.5-flash-lite` passed the self-test and is fastest (recommended). `gemini-2.5-flash-lite` is closed to new users |
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
| `python judge.py --selftest --audio` | Adds the voice transcription case (`tests/fixtures/sample.webm`) | One more call |

## Calibration

Turn on Calibration mode on the start screen. On each answer you enter your own 5 scores, and tick "deliberate error" if you seeded one, before the AI's feedback is shown. History reports agreement within ±1 and the seeded-error hit rate, counted for the current `PROMPT_VERSION` only (first line of `prompts/judge.md`). Bump the version whenever you change the prompt. Pass bar: at least 80% within ±1 over 20 answers, and all 5 seeded errors flagged.

## Cost

Measured cost per round of 5: **not yet measured** (fill in after the first live round; History shows the total spend).

## Credit

The idea comes from a remark Jack Symes made on stream ([Jack v Halo - Part II, around 3:15:00](https://www.youtube.com/watch?v=kkw6ozdxli0&t=11700s)). This app is not affiliated with or endorsed by Jack Symes.
