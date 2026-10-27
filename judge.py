"""Gemini judge for the vegan-debate sparring app (PRD milestones M3 and M7).

Public API (functional; the only classes are the two exceptions):
    PROMPT_VERSION, TEMPERATURE
    get_client(), ping(model=None)
    make_followup(objection, opener_text, model=None)            -> (str, usage)
    judge_answer(objection, opener_text, followup_text, reply_text,
                 delivery=None, model=None)                       -> (feedback, usage)
    transcribe(audio_bytes, mime, model=None)                     -> ({"transcript", "delivery"}, usage)
    combine_usage(*usages)                                        -> usage

usage = {"model", "input_tokens", "output_tokens" (candidates + thinking), "cost_usd"}.

API surface: client.interactions.create with response_format (PRD decision). Set
GEMINI_API_SURFACE=generate_content in .env to fall back to client.models.generate_content
with response_json_schema if the live interactions behaviour differs from the docs.

Logging: metadata only (model, latency, token counts, cost, attempt). Never answer text,
prompts or transcripts.

CLI: python judge.py --selftest [--model X] [--repeat N] [--audio [PATH]]
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import random
import re
import statistics
import sys
import time
from functools import lru_cache
from pathlib import Path

import httpx
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

import config

try:  # the interactions endpoint raises its own APIError hierarchy (status_code, not code)
    from google.genai._gaos.lib.compat_errors import APIError as _InteractionsAPIError
except Exception:  # pragma: no cover - SDK layout changed
    _InteractionsAPIError = None

load_dotenv(config.BASE_DIR / ".env")

log = logging.getLogger("sparring.judge")


class JudgeError(Exception):
    """Any failure after retries: API error, blocked/empty response, schema-invalid output.

    `usage` (when set) holds the tokens and cost spent before giving up."""

    usage = None


class KeyRejected(Exception):
    """The API key was rejected (HTTP 401/403, or 400 API_KEY_INVALID)."""


# ---------------------------------------------------------------------------
# Constants and module-level knobs
# ---------------------------------------------------------------------------

TEMPERATURE = None  # selftest sets 0. Deprecated for 3.5+ models; dropped automatically on a 400.
BACKOFF_BASE_S = 1.0
API_SURFACE = os.getenv("GEMINI_API_SURFACE", "interactions").strip().lower()

DIMENSIONS = ("accuracy", "logic", "crux", "concession_discipline", "concision")
FLAG_TYPES = ("fallacy", "concession", "missed_point", "unsupported_claim", "injection_attempt")
TURNS = ("opener", "reply")
FAIL_STATUSES = {"failed", "cancelled", "incomplete", "budget_exceeded"}
TEMPLATE_MARKER = "=== INPUT TEMPLATE ==="

# Lowest thinking level first. A 400 that mentions "thinking" steps down the ladder;
# None means "omit the setting".
_THINK_LADDER = ("minimal", "low", None)
_think_pos: dict = {}
_no_temperature: set = set()
_warned_models: set = set()

# Tags whose literal text in user content is neutralised so it cannot close or open our wrappers.
_WRAPPER_TAGS = (
    "opener", "reply", "followup", "objection", "objection_category", "objection_as_put_to_them",
    "strongest_form_of_objection", "key_points_a_strong_answer_addresses", "reference_sources",
    "empty_turns",
)
_TAG_RE = re.compile(r"<\s*(/?)\s*(" + "|".join(_WRAPPER_TAGS) + r")\b[^>]*>", re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
_HEALTH_RE = re.compile(r"\b(child|children|kid|kids|infant|baby|babies|pregnan\w*|supplement\w*|b12)\b", re.I)


def _read_first_line_version(path: Path) -> str:
    first = path.read_text(encoding="utf-8").splitlines()[0].strip()
    match = re.fullmatch(r"PROMPT_VERSION:\s*(\S+)", first)
    if not match:
        raise RuntimeError(f"{path.name} must start with 'PROMPT_VERSION: vN' (found {first[:40]!r})")
    return match.group(1)


PROMPT_VERSION: str = _read_first_line_version(config.PROMPTS_DIR / "judge.md")


# ---------------------------------------------------------------------------
# Small helpers: prompts, escaping, usage
# ---------------------------------------------------------------------------

def _read_prompt(name: str) -> tuple[str, str]:
    """Return (system_text, input_template) from prompts/<name>."""
    lines = (config.PROMPTS_DIR / name).read_text(encoding="utf-8").splitlines()
    if lines and lines[0].startswith("PROMPT_VERSION:"):
        lines = lines[1:]
    system, _, template = "\n".join(lines).partition(TEMPLATE_MARKER)
    return system.strip(), template.strip()


def _fill(template: str, values: dict) -> str:
    """Replace {placeholders} in one pass (values are never re-scanned, so user text
    containing '{reply}' cannot inject anything). Unknown placeholders are left as is."""
    return _PLACEHOLDER_RE.sub(lambda m: str(values.get(m.group(1), m.group(0))), template)


def _escape(text: str | None) -> str:
    """Neutralise literal wrapper tags (opening and closing) inside user-supplied text."""
    return _TAG_RE.sub(lambda m: f"&lt;{m.group(1)}{m.group(2)}&gt;", text or "")


def _blank(text: str | None) -> bool:
    return not (text or "").strip()


def _words(text: str) -> int:
    return len(text.split())


def _prompt_limit(limit: int) -> int:
    """Word limit quoted to the model: 15% below the hard limit so near-misses do not cost a re-ask."""
    return max(10, int(limit * 0.85))


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _zero_usage(model: str) -> dict:
    return {"model": model, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}


def _cost(model: str, prompt_tokens: int, output_tokens: int) -> float:
    price = config.PRICES.get(model)
    if not price:
        if model not in _warned_models:
            _warned_models.add(model)
            log.warning("no price for model=%s in config.PRICES; cost recorded as 0", model)
        return 0.0
    return (prompt_tokens * price["input"] + output_tokens * price["output"]) / 1e6


def combine_usage(*usages) -> dict:
    """Sum usage dicts (None entries ignored). `model` is the shared model, or 'a+b' if mixed."""
    items = [u for u in usages if u]
    models = []
    for u in items:
        if u.get("model") and u["model"] not in models:
            models.append(u["model"])
    return {
        "model": "+".join(models),
        "input_tokens": sum(_int(u.get("input_tokens")) for u in items),
        "output_tokens": sum(_int(u.get("output_tokens")) for u in items),
        "cost_usd": round(sum(float(u.get("cost_usd") or 0.0) for u in items), 8),
    }


def _loads(text: str):
    """Parse JSON, tolerating a ```json fence. Returns None on failure."""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rsplit("```", 1)[0]
    try:
        return json.loads(body)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Client, errors, raw call layer
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_client() -> genai.Client:
    """Cached client; GEMINI_API_KEY from the environment (.env loaded at import)."""
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise JudgeError("GEMINI_API_KEY is not set")
    return genai.Client(api_key=key, http_options=types.HttpOptions(timeout=config.CALL_TIMEOUT_MS))


def _error_status(exc: BaseException) -> int | None:
    for attr in ("code", "status_code"):  # genai.errors.APIError.code / interactions APIError.status_code
        value = getattr(exc, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _is_api_error(exc: BaseException) -> bool:
    kinds = [genai_errors.APIError, httpx.HTTPError]
    if _InteractionsAPIError is not None:
        kinds.append(_InteractionsAPIError)
    return isinstance(exc, tuple(kinds))


def _is_transient(exc: BaseException) -> bool:
    """429 / 5xx / client-side timeout or connection failure: worth a backoff and retry."""
    status = _error_status(exc)
    if status is not None:
        return status == 429 or status >= 500
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError)) or type(exc).__name__ in (
        "APITimeoutError", "APIConnectionError")


def _is_key_rejected(exc: BaseException) -> bool:
    status = _error_status(exc)
    if status in (401, 403):
        return True
    if status == 400:  # Gemini reports a bad key as 400 API_KEY_INVALID
        msg = _safe_str(exc).lower()
        return "api key not valid" in msg or "api_key_invalid" in msg or "api key expired" in msg
    return False


def _safe_str(exc: BaseException) -> str:
    try:
        return str(exc)
    except Exception:  # SDK error objects can fail to stringify
        return ""


def _adapt_to_400(model: str, exc: BaseException) -> bool:
    """If a 400 complains about thinking or temperature settings, relax them. True if changed."""
    if _error_status(exc) != 400:
        return False
    msg = _safe_str(exc).lower()
    pos = _think_pos.get(model, 0)
    if "thinking" in msg and pos < len(_THINK_LADDER) - 1:
        _think_pos[model] = pos + 1
        log.warning("model=%s rejected thinking_level=%s; stepping down", model, _THINK_LADDER[pos])
        return True
    if "temperature" in msg and TEMPERATURE is not None and model not in _no_temperature:
        _no_temperature.add(model)
        log.warning("model=%s rejected temperature; dropping it", model)
        return True
    return False


def _thinking_level(model: str):
    return _THINK_LADDER[_think_pos.get(model, 0)]


def _as_input(parts: list):
    """parts = [("text", str) | ("audio", bytes, mime)] -> interactions input (str or content blocks)."""
    if all(p[0] == "text" for p in parts):
        return "\n\n".join(p[1] for p in parts)
    blocks = []
    for p in parts:
        if p[0] == "text":
            blocks.append({"type": "text", "text": p[1]})
        else:
            blocks.append({"type": "audio", "data": base64.b64encode(p[1]).decode("ascii"), "mime_type": p[2]})
    return blocks


def _send_interactions(model, system, parts, schema, max_out) -> dict:
    gen_cfg: dict = {}
    level = _thinking_level(model)
    if level:
        gen_cfg["thinking_level"] = level
    if TEMPERATURE is not None and model not in _no_temperature:
        gen_cfg["temperature"] = TEMPERATURE
    if max_out:
        gen_cfg["max_output_tokens"] = max_out
    kwargs = {"model": model, "input": _as_input(parts), "system_instruction": system, "store": False}
    if gen_cfg:
        kwargs["generation_config"] = gen_cfg
    if schema:
        kwargs["response_format"] = {"type": "text", "mime_type": "application/json", "schema": schema}
    interaction = get_client().interactions.create(**kwargs)

    usage = getattr(interaction, "usage", None)
    prompt = _int(getattr(usage, "total_input_tokens", None))
    out = _int(getattr(usage, "total_output_tokens", None))
    thought = _int(getattr(usage, "total_thought_tokens", None))
    tool = _int(getattr(usage, "total_tool_use_tokens", None))
    total = getattr(usage, "total_tokens", None)
    cand = out
    if thought and isinstance(total, int) and prompt + out + tool == total:
        cand = max(out - thought, 0)  # total_output_tokens already included the thinking tokens
    status = getattr(interaction, "status", None)
    return {
        "text": getattr(interaction, "output_text", None) or "",
        "blocked": f"status={status}" if status in FAIL_STATUSES else None,
        "prompt": prompt, "cand": cand, "thought": thought,
    }


def _send_generate_content(model, system, parts, schema, max_out) -> dict:
    cfg: dict = {"system_instruction": system}
    level = _thinking_level(model)
    if level:
        cfg["thinking_config"] = types.ThinkingConfig(thinking_level=types.ThinkingLevel[level.upper()])
    if TEMPERATURE is not None and model not in _no_temperature:
        cfg["temperature"] = TEMPERATURE
    if max_out:
        cfg["max_output_tokens"] = max_out
    if schema:
        cfg["response_mime_type"] = "application/json"
        cfg["response_json_schema"] = schema
    contents = [
        types.Part.from_text(text=p[1]) if p[0] == "text" else types.Part.from_bytes(data=p[1], mime_type=p[2])
        for p in parts
    ]
    response = get_client().models.generate_content(
        model=model, contents=contents, config=types.GenerateContentConfig(**cfg))

    blocked = None
    feedback = getattr(response, "prompt_feedback", None)
    if feedback is not None and getattr(feedback, "block_reason", None):
        blocked = f"prompt_blocked={getattr(feedback.block_reason, 'name', feedback.block_reason)}"
    candidates = getattr(response, "candidates", None) or []
    if candidates and not blocked:
        reason = getattr(candidates[0], "finish_reason", None)
        name = getattr(reason, "name", str(reason)) if reason else None
        if name not in (None, "STOP", "FINISH_REASON_UNSPECIFIED"):
            blocked = f"finish_reason={name}"
    meta = getattr(response, "usage_metadata", None)
    try:
        text = response.text or ""
    except Exception:
        text = ""
    return {
        "text": text, "blocked": blocked,
        "prompt": _int(getattr(meta, "prompt_token_count", None)),
        "cand": _int(getattr(meta, "candidates_token_count", None)),
        "thought": _int(getattr(meta, "thoughts_token_count", None)),
    }


def _send(model, system, parts, schema, max_out) -> dict:
    if API_SURFACE == "generate_content":
        return _send_generate_content(model, system, parts, schema, max_out)
    return _send_interactions(model, system, parts, schema, max_out)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _log_call(fn, model, attempt, latency_ms, prompt, cand, thought, cost, outcome) -> None:
    log.info(
        "gemini_call fn=%s model=%s attempt=%d latency_ms=%d prompt_tokens=%d candidate_tokens=%d "
        "thinking_tokens=%d cost_usd=%.6f outcome=%s",
        fn, model, attempt, latency_ms, prompt, cand, thought, cost, outcome)


def _run(fn: str, model: str, system: str, parts: list, schema, validate, max_out=None):
    """Call Gemini with a shared budget of config.MAX_CALLS calls.

    validate(text) -> (value, error_or_None). A validation error triggers a re-ask (which
    uses one call of the budget). Returns (value, usage); raises JudgeError when exhausted."""
    budget = config.MAX_CALLS
    tot_prompt = tot_out = 0
    tot_cost = 0.0
    reask = None
    last = "no attempt made"

    def usage_now() -> dict:
        return {"model": model, "input_tokens": tot_prompt, "output_tokens": tot_out,
                "cost_usd": round(tot_cost, 8)}

    def fail(message: str, cause=None):
        err = JudgeError(message)
        err.usage = usage_now()
        if cause is not None:
            raise err from cause
        raise err

    for attempt in range(1, budget + 1):
        send_parts = parts + [("text", reask)] if reask else parts
        t0 = time.perf_counter()
        try:
            raw = _send(model, system, send_parts, schema, max_out)
        except JudgeError as exc:  # e.g. no API key: not retryable, message already clear
            exc.usage = usage_now()
            raise
        except Exception as exc:  # noqa: BLE001 - classified below
            latency = int((time.perf_counter() - t0) * 1000)
            status = _error_status(exc)
            _log_call(fn, model, attempt, latency, 0, 0, 0, 0.0,
                      f"error:{type(exc).__name__}:{status}")
            if _is_key_rejected(exc):
                raise KeyRejected(f"API key rejected (HTTP {status})") from exc
            adapted = _adapt_to_400(model, exc)
            transient = _is_transient(exc)
            if not (adapted or transient):
                fail(f"{fn}: Gemini call failed ({type(exc).__name__}, status={status})", exc)
            last = f"{type(exc).__name__} status={status}"
            if transient and attempt < budget:
                _sleep(BACKOFF_BASE_S * 2 ** (attempt - 1) + random.uniform(0, BACKOFF_BASE_S * 0.25))
            continue

        latency = int((time.perf_counter() - t0) * 1000)
        p, c, th = raw["prompt"], raw["cand"], raw["thought"]
        call_cost = _cost(model, p, c + th)
        tot_prompt += p
        tot_out += c + th
        tot_cost += call_cost

        text = raw["text"]
        if raw["blocked"] or not text.strip():
            last = f"blocked or empty response ({raw['blocked'] or 'no text'})"
            _log_call(fn, model, attempt, latency, p, c, th, call_cost, "blocked_or_empty")
            continue

        value, err = validate(text)
        if err:
            last = f"invalid output ({err})"
            _log_call(fn, model, attempt, latency, p, c, th, call_cost, "invalid")
            log.warning("fn=%s attempt=%d validation_failed reason=%s", fn, attempt, err)
            reask = (f"Your previous reply was rejected: {err}. "
                     "Reply again with a corrected answer that fixes exactly this.")
            continue

        _log_call(fn, model, attempt, latency, p, c, th, call_cost, "ok")
        return value, usage_now()

    fail(f"{fn} failed after {budget} Gemini calls: {last}")


# ---------------------------------------------------------------------------
# Objection / prompt input rendering
# ---------------------------------------------------------------------------

def _objection_values(objection: dict) -> dict:
    points = [str(p) for p in (objection.get("key_points") or [])]
    sources = [str(s)[:300] for s in (objection.get("sources") or [])][:10]
    return {
        "category": _escape(str(objection.get("category", ""))),
        "objection_text": _escape(str(objection.get("text", ""))),
        "strongest_form": _escape(str(objection.get("strongest_form", "") or objection.get("text", ""))),
        "key_points": _escape("\n".join(f"- {p}" for p in points)) or "(none)",
        "sources": _escape("\n".join(f"- {s}" for s in sources)) or "(none)",
    }


def _empty_note(opener_empty: bool, reply_empty: bool) -> str:
    empty = [name for name, flag in (("opener", opener_empty), ("reply", reply_empty)) if flag]
    if not empty:
        return "none"
    return "The " + " and the ".join(empty) + " was empty (the person gave no answer for that turn)."


# ---------------------------------------------------------------------------
# make_followup
# ---------------------------------------------------------------------------

def _validate_followup(text: str):
    cleaned = text.strip().strip('"').strip("'").strip()
    if not cleaned:
        return None, "the follow-up was empty"
    limit = config.FOLLOWUP_MAX_WORDS
    if _words(cleaned) > limit:
        return None, f"the follow-up has {_words(cleaned)} words but the limit is {_prompt_limit(limit)}; shorten it"
    return cleaned, None


def make_followup(objection: dict, opener_text: str, model: str | None = None) -> tuple[str, dict]:
    """One in-character pushback (at most config.FOLLOWUP_MAX_WORDS words) on the weakest part of the opener."""
    model = model or config.GEMINI_MODEL
    system_tpl, input_tpl = _read_prompt("followup.md")
    limit = str(_prompt_limit(config.FOLLOWUP_MAX_WORDS))
    values = _objection_values(objection)
    values.update({
        "opener": _escape(opener_text or ""),
        "empty_note": _empty_note(_blank(opener_text), False),
        "max_words": limit,
    })
    system = _fill(system_tpl, {"max_words": limit})
    parts = [("text", _fill(input_tpl, values))]
    return _run("make_followup", model, system, parts, None, _validate_followup)


# ---------------------------------------------------------------------------
# judge_answer
# ---------------------------------------------------------------------------

def _int_schema() -> dict:
    return {"type": "integer", "minimum": 1, "maximum": 5}


FEEDBACK_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "object",
            "properties": {dim: _int_schema() for dim in DIMENSIONS},
            "required": list(DIMENSIONS),
        },
        "overall": _int_schema(),
        "flags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": list(FLAG_TYPES)},
                    "turn": {"type": "string", "enum": list(TURNS)},
                    "detail": {"type": "string"},
                },
                "required": ["type", "turn", "detail"],
            },
        },
        "tips": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 3},
        "model_answer": {"type": "string"},
        "notes": {
            "type": "object",
            "properties": {
                "tone": {"type": "string"},
                "persuasiveness": {"type": "string"},
                "delivery": {"type": ["string", "null"]},
            },
            "required": ["tone", "persuasiveness", "delivery"],
        },
    },
    "required": ["scores", "overall", "flags", "tips", "model_answer", "notes"],
}

TRANSCRIBE_SCHEMA = {
    "type": "object",
    "properties": {
        "transcript": {"type": "string"},
        "delivery": {"type": ["string", "null"]},
    },
    "required": ["transcript", "delivery"],
}


def _as_score(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and 1 <= value <= 5:
        return value
    return None


def _check_feedback(obj):
    """Validate and normalise a parsed feedback object -> (feedback, None) or (None, reason)."""
    if not isinstance(obj, dict):
        return None, "the reply was not a JSON object"
    raw_scores = obj.get("scores")
    if not isinstance(raw_scores, dict):
        return None, "scores is missing"
    scores = {}
    for dim in DIMENSIONS:
        value = _as_score(raw_scores.get(dim))
        if value is None:
            return None, f"scores.{dim} must be an integer from 1 to 5"
        scores[dim] = value
    overall = _as_score(obj.get("overall"))
    if overall is None:
        return None, "overall must be an integer from 1 to 5"

    raw_flags = obj.get("flags")
    if not isinstance(raw_flags, list):
        return None, "flags must be a list"
    flags = []
    for flag in raw_flags:
        if not isinstance(flag, dict):
            return None, "each flag must be an object"
        ftype, turn, detail = flag.get("type"), flag.get("turn"), flag.get("detail")
        if ftype not in FLAG_TYPES:
            return None, f"flag type must be one of {', '.join(FLAG_TYPES)}"
        if turn not in TURNS:
            return None, "flag turn must be opener or reply"
        if not isinstance(detail, str) or not detail.strip():
            return None, "each flag needs a non-empty detail"
        flags.append({"type": ftype, "turn": turn, "detail": detail.strip()})

    tips = obj.get("tips")
    if (not isinstance(tips, list) or not 2 <= len(tips) <= 3
            or not all(isinstance(t, str) and t.strip() for t in tips)):
        return None, "tips must be 2 or 3 non-empty strings"
    tips = [t.strip() for t in tips]

    answer = obj.get("model_answer")
    if not isinstance(answer, str) or not answer.strip():
        return None, "model_answer is missing"
    answer = answer.strip()
    if _words(answer) > config.MODEL_ANSWER_MAX_WORDS:
        return None, (f"model_answer has {_words(answer)} words but the limit is "
                      f"{_prompt_limit(config.MODEL_ANSWER_MAX_WORDS)}; shorten it")

    notes = obj.get("notes")
    if not isinstance(notes, dict):
        return None, "notes is missing"
    tone, pers, delivery = notes.get("tone"), notes.get("persuasiveness"), notes.get("delivery")
    if not isinstance(tone, str) or not isinstance(pers, str):
        return None, "notes.tone and notes.persuasiveness must be strings"
    if delivery is not None and not isinstance(delivery, str):
        return None, "notes.delivery must be a string or null"

    return {
        "scores": scores, "overall": overall, "flags": flags, "tips": tips, "model_answer": answer,
        "notes": {"tone": tone.strip(), "persuasiveness": pers.strip(),
                  "delivery": delivery.strip() if delivery and delivery.strip() else None},
    }, None


def _validate_feedback(text: str):
    obj = _loads(text)
    if obj is None:
        return None, "the reply was not valid JSON"
    return _check_feedback(obj)


def _delivery_text(delivery: dict | None) -> str | None:
    if not delivery:
        return None
    parts = []
    for turn in TURNS:
        note = (delivery.get(turn) or "").strip()
        if note:
            parts.append(f"{turn.capitalize()}: {note}")
    return " ".join(parts) or None


def _model_answer_from_key_points(objection: dict) -> str:
    """Short model answer for the empty short-circuit, built from key_points only (no API call)."""
    points = [str(p).strip().rstrip(".") for p in (objection.get("key_points") or []) if str(p).strip()]
    haystack = " ".join([str(objection.get("category", "")), str(objection.get("text", "")),
                         str(objection.get("strongest_form", "")), " ".join(points)])
    note = ""
    if _HEALTH_RE.search(haystack):
        note = (" Well-planned vegan diets need B12 from supplements or fortified foods, and individuals "
                "should consult a registered dietitian or doctor.")
    lead = "A strong reply would take the objection head on and cover these points: "
    limit = config.MODEL_ANSWER_MAX_WORDS
    while True:
        text = lead + "; ".join(points) + "." + note if points else lead.rstrip(": ") + "." + note
        if _words(text) <= limit or len(points) <= 1:
            break
        points.pop()
    if _words(text) > limit:
        text = " ".join(text.split()[:limit])
    return text


def _empty_feedback(objection: dict, opener_empty: bool = True, reply_empty: bool = True) -> dict:
    flags = [{"type": "missed_point", "turn": turn, "detail": "No answer was given for this turn."}
             for turn, empty in (("opener", opener_empty), ("reply", reply_empty)) if empty]
    return {
        "scores": {dim: 1 for dim in DIMENSIONS},
        "overall": 1,
        "flags": flags,
        "tips": [
            "Give a direct answer to the objection, even a sentence or two; a blank turn scores nothing.",
            "Open with the one point the objection turns on, then back it with a reason or an example.",
        ],
        "model_answer": _model_answer_from_key_points(objection),
        "notes": {"tone": "No answer was given, so there is no tone to assess.",
                  "persuasiveness": "Nothing to assess.", "delivery": None},
    }


def judge_answer(objection: dict, opener_text: str, followup_text: str, reply_text: str,
                 delivery: dict | None = None, model: str | None = None) -> tuple[dict, dict]:
    """Score both turns against the rubric. Returns (feedback, usage).

    delivery = {"opener": str|None, "reply": str|None} (from transcribe) is copied into
    notes.delivery by code (not by the model), so it stays faithful and cannot carry injected text."""
    model = model or config.GEMINI_MODEL
    opener_empty, reply_empty = _blank(opener_text), _blank(reply_text)
    if opener_empty and reply_empty:
        return _empty_feedback(objection), _zero_usage(model)

    system_tpl, input_tpl = _read_prompt("judge.md")
    limit = str(_prompt_limit(config.MODEL_ANSWER_MAX_WORDS))
    values = _objection_values(objection)
    values.update({
        "opener": _escape(opener_text or ""),
        "followup": _escape(followup_text or ""),
        "reply": _escape(reply_text or ""),
        "empty_note": _empty_note(opener_empty, reply_empty),
    })
    system = _fill(system_tpl, {"max_words": limit})
    parts = [("text", _fill(input_tpl, values))]
    feedback, usage = _run("judge_answer", model, system, parts, FEEDBACK_SCHEMA, _validate_feedback)

    for turn, empty in (("opener", opener_empty), ("reply", reply_empty)):
        if empty and not any(f["type"] == "missed_point" and f["turn"] == turn for f in feedback["flags"]):
            feedback["flags"].append(
                {"type": "missed_point", "turn": turn, "detail": "No answer was given for this turn."})
    feedback["notes"]["delivery"] = _delivery_text(delivery)
    return feedback, usage


# ---------------------------------------------------------------------------
# transcribe (M7 two-call path)
# ---------------------------------------------------------------------------

def _validate_transcript(text: str):
    obj = _loads(text)
    if not isinstance(obj, dict):
        return None, "the reply was not a JSON object"
    transcript, delivery = obj.get("transcript"), obj.get("delivery")
    if not isinstance(transcript, str):
        return None, "transcript must be a string"
    if delivery is not None and not isinstance(delivery, str):
        return None, "delivery must be a string or null"
    transcript = transcript.strip()
    delivery = delivery.strip() if isinstance(delivery, str) and delivery.strip() else None
    return {"transcript": transcript, "delivery": delivery if transcript else None}, None


def transcribe(audio_bytes: bytes, mime: str, model: str | None = None) -> tuple[dict, dict]:
    """Transcribe inline audio. Returns ({"transcript": str, "delivery": str|None}, usage).
    An empty transcript means silence or unintelligible audio."""
    model = model or config.GEMINI_MODEL
    if not audio_bytes:
        raise JudgeError("transcribe: no audio data")
    mime = (mime or "audio/webm").split(";")[0].strip().lower()
    system, input_tpl = _read_prompt("transcribe.md")
    parts = [("text", _fill(input_tpl, {})), ("audio", audio_bytes, mime)]
    return _run("transcribe", model, system, parts, TRANSCRIBE_SCHEMA, _validate_transcript)


# ---------------------------------------------------------------------------
# ping
# ---------------------------------------------------------------------------

def ping(model: str | None = None) -> None:
    """One tiny call to prove the key works. KeyRejected on 401/403; other errors re-raised."""
    model = model or config.GEMINI_MODEL
    for attempt in (1, 2):
        t0 = time.perf_counter()
        try:
            raw = _send(model, "Reply with the single word OK.", [("text", "ping")], None, 16)
        except Exception as exc:  # noqa: BLE001
            _log_call("ping", model, attempt, int((time.perf_counter() - t0) * 1000), 0, 0, 0, 0.0,
                      f"error:{type(exc).__name__}:{_error_status(exc)}")
            if _is_key_rejected(exc):
                raise KeyRejected(f"API key rejected (HTTP {_error_status(exc)})") from exc
            if attempt == 1 and _adapt_to_400(model, exc):
                continue
            raise
        p, c, th = raw["prompt"], raw["cand"], raw["thought"]
        _log_call("ping", model, attempt, int((time.perf_counter() - t0) * 1000), p, c, th,
                  _cost(model, p, c + th), "ok")
        return None


# ---------------------------------------------------------------------------
# Selftest CLI
# ---------------------------------------------------------------------------

ANSWER_KINDS = ("strong", "weak", "empty", "concession", "wrong", "injection")
AUDIO_MIMES = {".webm": "audio/webm", ".ogg": "audio/ogg", ".opus": "audio/opus", ".mp3": "audio/mp3",
               ".wav": "audio/wav", ".m4a": "audio/m4a", ".flac": "audio/flac", ".aac": "audio/aac"}


def _aggregate(feedbacks: list[dict]) -> dict:
    """Median per metric across repeats; a flag counts as present if a majority of runs raised it."""
    n = len(feedbacks)
    flag_counts: dict = {}
    for fb in feedbacks:
        for ftype in {f["type"] for f in fb["flags"]}:
            flag_counts[ftype] = flag_counts.get(ftype, 0) + 1
    return {
        "overall": statistics.median(fb["overall"] for fb in feedbacks),
        "scores": {d: statistics.median(fb["scores"][d] for fb in feedbacks) for d in DIMENSIONS},
        "flags": {t for t, count in flag_counts.items() if count * 2 > n},
    }


def _fmt(value) -> str:
    return f"{value:g}" if isinstance(value, (int, float)) else str(value)


def _run_selftest(model: str, repeat: int) -> tuple[bool, dict, list[str]]:
    path = config.TESTS_DIR / "selftest.json"
    cases = json.loads(path.read_text(encoding="utf-8"))["objections"]
    usages: list[dict] = []
    rows: list[str] = []
    problems: list[str] = []
    all_ok = True
    header = f"{'objection':<30} {'overall S/W/E/C/X/I':<20} {'acc C/X':<8} checks(1-6)"
    rows.append(header)
    rows.append("-" * len(header))
    for case in cases:
        objection, answers = case["objection"], case["answers"]
        agg: dict = {}
        try:
            for kind in ANSWER_KINDS:
                a = answers[kind]
                runs = []
                for _ in range(repeat):
                    fb, usage = judge_answer(objection, a["opener"], a["followup"], a["reply"], model=model)
                    runs.append(fb)
                    usages.append(usage)
                agg[kind] = _aggregate(runs)
        except JudgeError as exc:
            all_ok = False
            problems.append(f"{objection['id']}: JudgeError: {exc}")
            usages.append(exc.usage or _zero_usage(model))
            rows.append(f"{objection['id']:<30} ERROR (see below)")
            continue
        checks = [
            ("strong.overall >= weak.overall + 1", agg["strong"]["overall"] >= agg["weak"]["overall"] + 1),
            ("empty.overall <= 2", agg["empty"]["overall"] <= 2),
            ("concession.accuracy > wrong.accuracy",
             agg["concession"]["scores"]["accuracy"] > agg["wrong"]["scores"]["accuracy"]),
            ("wrong has unsupported_claim flag", "unsupported_claim" in agg["wrong"]["flags"]),
            ("injection has injection_attempt flag", "injection_attempt" in agg["injection"]["flags"]),
            ("injection.overall <= strong.overall", agg["injection"]["overall"] <= agg["strong"]["overall"]),
        ]
        grid = "/".join(_fmt(agg[k]["overall"]) for k in ANSWER_KINDS)
        acc = f"{_fmt(agg['concession']['scores']['accuracy'])}/{_fmt(agg['wrong']['scores']['accuracy'])}"
        marks = " ".join("ok" if ok else "FAIL" for _, ok in checks)
        rows.append(f"{objection['id']:<30} {grid:<20} {acc:<8} {marks}")
        for name, ok in checks:
            if not ok:
                all_ok = False
                problems.append(f"{objection['id']}: FAILED {name}")
    return all_ok, combine_usage(*usages), rows + [""] + [f"  {p}" for p in problems]


def _run_audio_case(path: Path, model: str) -> tuple[str, dict | None, str]:
    """-> (status 'PASS'|'FAIL'|'SKIP', usage, detail)."""
    if not path.exists():
        return "SKIP", None, f"{path} not found (record a 10 s answer there to enable the audio case)"
    mime = AUDIO_MIMES.get(path.suffix.lower())
    if not mime:
        return "FAIL", None, f"unsupported audio extension {path.suffix!r}"
    try:
        result, usage = transcribe(path.read_bytes(), mime, model=model)
    except JudgeError as exc:
        return "FAIL", exc.usage, f"JudgeError: {exc}"
    if not result["transcript"].strip():
        return "FAIL", usage, "transcript is empty"
    return "PASS", usage, f"{_words(result['transcript'])} words transcribed"


def main(argv: list[str] | None = None) -> int:
    global TEMPERATURE
    parser = argparse.ArgumentParser(description="Gemini judge for the debate sparring app")
    parser.add_argument("--selftest", action="store_true", help="run tests/selftest.json at temperature 0")
    parser.add_argument("--model", default=None, help="override GEMINI_MODEL")
    parser.add_argument("--repeat", type=int, default=1, help="runs per answer; the median is used")
    parser.add_argument("--audio", nargs="?", const="", default=None, metavar="PATH",
                        help="audio case; default tests/fixtures/sample.webm (alone: run only the audio case)")
    args = parser.parse_args(argv)
    if not args.selftest and args.audio is None:
        parser.print_help()
        return 2
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    TEMPERATURE = 0
    model = args.model or config.GEMINI_MODEL
    print(f"model={model} prompt_version={PROMPT_VERSION} surface={API_SURFACE} repeat={args.repeat} temperature=0")

    ok = True
    usages: list[dict] = []
    try:
        get_client()  # fail fast with a clear message if the key is missing
        if args.selftest:
            passed, usage, lines = _run_selftest(model, args.repeat)
            usages.append(usage)
            ok = ok and passed
            print("\n".join(lines))
        audio_path = Path(args.audio) if args.audio else config.TESTS_DIR / "fixtures" / "sample.webm"
        status, usage, detail = _run_audio_case(audio_path, model)
        if usage:
            usages.append(usage)
        print(f"audio case: {status} - {detail}")
        if status == "FAIL":
            ok = False
    except KeyRejected as exc:
        print(f"ERROR: {exc}")
        return 1
    except JudgeError as exc:
        print(f"ERROR: {exc}")
        return 1

    total = combine_usage(*usages)
    print(f"\ntokens in={total['input_tokens']} out={total['output_tokens']} cost=${total['cost_usd']:.4f}")
    print("RESULT: PASS" if ok else "RESULT: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
