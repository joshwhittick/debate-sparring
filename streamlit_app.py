"""Debate sparring app, Streamlit front end (local or Streamlit Community Cloud). Logic lives in `core`.

Run: streamlit run streamlit_app.py
On Streamlit Cloud, put GEMINI_API_KEY and APP_PASSWORD in the app's secrets.

Public mode (Streamlit Cloud, any host other than localhost, or PUBLIC_DEPLOY=1) fails closed: it refuses to run
without APP_PASSWORD, hides History and calibration (the database is shared by every visitor), caps Gemini calls
per session and deletes recordings once judged.
"""
import hmac
import os
import time

import streamlit as st

# Copy root-level Streamlit secrets into the environment before config reads it (Cloud has no .env).
try:
    for _key, _value in st.secrets.items():
        if isinstance(_value, (str, int, float, bool)):
            os.environ.setdefault(_key, str(_value))
except Exception:  # no secrets.toml: local run, .env applies
    pass

import config  # noqa: E402
import core  # noqa: E402
import judge  # noqa: E402
import store  # noqa: E402

st.set_page_config(page_title="Sparring Partner", page_icon="🌱")
ss = st.session_state
DISCLAIMER = "Debate practice, not medical or dietary advice."
DATA_NOTICE = ("Your answers are sent to Google's Gemini API. On the free tier Google may use them to improve its "
               "products, and people may review them. Don't include personal information.")
SPENDING = {"start_answer", "retry_followup", "submit_reply", "rejudge"}  # core calls that reach Gemini
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
DIM_LABELS = {"accuracy": "Accuracy", "logic": "Logic", "crux": "Crux", "concision": "Concision",
              "concession_discipline": "Concession discipline"}


# --- startup ---

class BootError(Exception):
    pass


@st.cache_resource
def boot():
    """Runs once per server process. Raises BootError (not cached, so the next page load retries) on failure."""
    try:
        core.init()
    except core.BankInvalid:
        raise BootError("Objection bank failed validation - see logs/app.log")
    if not config.GEMINI_API_KEY:
        raise BootError("GEMINI_API_KEY is not set - add it to .env (local) or the app's secrets (Cloud)")
    try:
        judge.ping()
    except judge.KeyRejected:
        raise BootError("API key rejected")
    except Exception as e:
        core.log.error("startup ping failed: %s", type(e).__name__)
        raise BootError("Gemini is unavailable right now - try again shortly")
    core.log.info("streamlit startup: model=%s tier=%s prompt_version=%s pending=%d", config.GEMINI_MODEL,
                  config.GEMINI_TIER, judge.PROMPT_VERSION, store.count_pending())


def is_public():
    """True unless the page is served to localhost. Fails closed: an unknown host counts as public."""
    if str(config.BASE_DIR).startswith("/mount/"):  # /mount/src is Streamlit Cloud
        return True
    if config.PUBLIC_DEPLOY in ("0", "1"):
        return config.PUBLIC_DEPLOY == "1"
    host = (st.context.headers.get("Host") or "").lower()
    return host.rsplit(":", 1)[0] not in LOCAL_HOSTS if host else True


def app_password():
    """Read live from st.secrets, so a password edited on Cloud applies without a reboot; .env otherwise."""
    try:
        return str(st.secrets.get("APP_PASSWORD", "")).strip() or config.APP_PASSWORD
    except Exception:  # no secrets.toml: local run
        return config.APP_PASSWORD


def password_ok():
    if ss.get("authed"):
        return True
    expected = app_password()
    if not expected:
        if is_public():
            st.error("APP_PASSWORD is not set. A public deploy needs one in the app's secrets.")
            return False
        return True
    pw = st.text_input("Password", type="password").strip()
    if pw and hmac.compare_digest(pw.encode(), expected.encode()):
        ss.authed = True
        st.rerun()
    if pw:
        time.sleep(1)  # slows guessing
        st.error("Wrong password")
    return False


def call(fn, *args, **kwargs):
    """Runs a core call; shows the error and returns None on failure. Counts Gemini calls in public mode."""
    if fn.__name__ in SPENDING and ss.public:
        if ss.get("calls", 0) >= config.SESSION_CALL_LIMIT:
            st.error("Session limit reached. Reload the page to start a new session.")
            return None
        ss.calls = ss.get("calls", 0) + 1
    try:
        return fn(*args, **kwargs)
    except core.CoreError as e:
        if e.code == "followup_failed":
            ss.answer_id, ss.phase = e.extra["answer_id"], "followup_failed"
            st.rerun()
        if e.code == "duplicate":  # a double click already saved this turn
            recover_duplicate()
        st.error(f"{e.code.replace('_', ' ').capitalize()}{' - ' + e.extra['detail'] if 'detail' in e.extra else ''}")
        return None
    except Exception as e:  # never show a traceback to a visitor
        core.log.error("%s failed: %s", fn.__name__, type(e).__name__)
        st.error("Something went wrong. Try again.")
        return None


def recover_duplicate():
    """After a double click, reruns on the screen for the turn that was saved (returns only if it can't)."""
    rnd = ss.round
    if ss.phase == "opener":
        obj_id = rnd["objections"][rnd["idx"]]["id"]
        row = next((a for a in store.round_answers(rnd["id"]) if a["objection_id"] == obj_id), None)
        if row:
            ss.answer_id, ss.turn_started = row["id"], time.time()
            ss.phase = "reply" if row["followup_text"] else "followup_failed"
            st.rerun()
    elif ss.phase == "reply" and ss.answer_id:
        ss.result = core.judged_out(store.get_answer(ss.answer_id))
        ss.phase = "feedback"
        st.rerun()


# --- round state ---

def start_round(category, study, shuffle, calibration, reviewer):
    out = call(core.new_round, category, study, shuffle)
    if out:
        ss.round = {"id": out["round_id"], "objections": out["objections"], "idx": 0, "study": study,
                    "calibration": calibration, "reviewer": reviewer}
        begin_objection()


def begin_objection():
    rnd = ss.round
    if rnd["idx"] >= len(rnd["objections"]):
        ss.phase = "summary"
        return
    ss.phase, ss.answer_id, ss.result, ss.turn_started = "opener", None, None, time.time()


def next_objection():
    ss.round["idx"] += 1
    begin_objection()


def end_round():
    ss.phase = "summary"


# --- widgets ---

@st.fragment(run_every=1)
def countdown(limit):
    left = limit - (time.time() - ss.turn_started)
    if left > 0:
        st.progress(left / limit, text=f"{int(left)} s left")
    elif left > -config.GRACE_S:
        st.warning("Time's up - finish your sentence")
    else:
        st.error("Time's up - submit now")


def turn_input(turn, key):
    """Mode picker, optional timer and input. Returns (mode, text, audio_bytes, mime) once submitted, else None."""
    modes = ["type", "speak"] if core.speak_enabled() else ["type"]
    mode = st.radio("Answer by", modes, horizontal=True, key=f"mode_{key}", format_func=str.capitalize)
    if not ss.round["study"]:
        countdown(config.TIMERS["opener" if turn == "opener" else "followup"][mode])
    if mode == "type":
        text = st.text_area("Your answer", key=f"text_{key}", height=160, max_chars=config.MAX_ANSWER_CHARS)
        if st.button("Submit", key=f"submit_{key}", type="primary", disabled=not text.strip()):
            return mode, text, None, None
        return None
    audio = st.audio_input("Record your answer", key=f"audio_{key}")
    if st.button("Submit", key=f"submit_{key}", type="primary", disabled=audio is None):
        return mode, None, audio.getvalue(), audio.type or "audio/wav"
    return None


def show_flags(flags):
    if not flags:
        st.write("No flags raised.")
        return
    for turn in ("opener", "reply"):
        mine = [f for f in flags if f.get("turn") == turn]
        if mine:
            st.markdown(f"**{turn.capitalize()}**")
            for f in mine:
                st.markdown(f"- `{f['type']}` {f.get('detail') or ''}")
    other = [f for f in flags if f.get("turn") not in ("opener", "reply")]
    for f in other:
        st.markdown(f"- `{f['type']}` {f.get('detail') or ''}")


def show_feedback(answer):
    fb = answer["feedback"]
    cols = st.columns(len(store.DIMENSIONS) + 1)
    cols[0].metric("Overall", f"{fb['overall']}/5")
    for col, d in zip(cols[1:], store.DIMENSIONS):
        col.metric(DIM_LABELS[d], fb["scores"][d])
    st.subheader("Flags")
    show_flags(fb.get("flags"))
    if fb.get("tips"):
        st.subheader("Tips")
        for tip in fb["tips"]:
            st.markdown(f"- {tip}")
    with st.expander("Model answer"):
        st.write(fb.get("model_answer") or "")
    notes = fb.get("notes") or {}
    for label, key in (("Tone", "tone"), ("Persuasiveness", "persuasiveness"), ("Delivery", "delivery")):
        if notes.get(key):
            st.caption(f"**{label}:** {notes[key]}")
    show_recordings(answer)
    st.caption(DISCLAIMER)


def show_recordings(answer):
    if not (answer["has_opener_audio"] or answer["has_reply_audio"]):
        return
    with st.expander("Your recordings"):
        for turn in ("opener", "reply"):
            if answer[f"has_{turn}_audio"]:
                try:
                    path, mime = core.audio_file(answer["id"], turn)
                except core.CoreError:
                    continue
                st.caption(turn.capitalize())
                st.audio(path.read_bytes(), format=mime)
        if st.button("Delete recordings", key=f"del_audio_{answer['id']}"):
            call(core.delete_audio, answer["id"])
            ss.result = core.judged_out(store.get_answer(answer["id"]))
            st.rerun()


# --- screens ---

def start_screen():
    st.write("Five objections per round. Answer each one, take one pushback, reply, then get scored feedback.")
    st.caption(DISCLAIMER)
    with st.form("start"):
        category = st.selectbox("Category", [""] + config.CATEGORIES,
                                format_func=lambda c: c.capitalize() if c else "All")
        study = st.checkbox("Study mode (untimed)")
        shuffle = st.checkbox("Shuffle (ignore how often each objection has come up)")
        calibration, reviewer = False, None
        if not ss.public:  # calibration stats are shared, so only offered locally
            calibration = st.checkbox("Calibration mode (score yourself before seeing feedback)")
            reviewer = st.selectbox("Reviewer", ["josh", "second"])
        if st.form_submit_button("Start round", type="primary"):
            start_round(category, study, shuffle, calibration, reviewer)
            st.rerun()
    if not core.speak_enabled():
        st.caption("Speak mode is off: it needs GEMINI_TIER=paid, or VOICE_ON_FREE_TIER=1.")
    st.caption(DATA_NOTICE)


def objection_header():
    rnd = ss.round
    obj = rnd["objections"][rnd["idx"]]
    st.caption(f"Objection {rnd['idx'] + 1} of {len(rnd['objections'])} · {obj['category'].capitalize()}"
               f"{' · Study mode' if rnd['study'] else ''}")
    st.markdown(f"### {obj['text']}")
    return obj


def opener_screen():
    rnd = ss.round
    obj = objection_header()
    key = f"{rnd['id']}_{obj['id']}_opener"
    submitted = turn_input("opener", key)
    if submitted:
        mode, text, data, mime = submitted
        with st.spinner("Getting the pushback..."):
            out = call(core.start_answer, rnd["id"], obj["id"], mode, text, data, mime or "audio/webm")
        if out:
            ss.answer_id, ss.phase, ss.turn_started = out["answer_id"], "reply", time.time()
            st.rerun()
    left, right = st.columns(2)
    if left.button("Skip this objection"):
        try:
            new = core.skip(rnd["id"], obj["id"])
        except core.CoreError as e:
            st.error(e.code.replace("_", " ").capitalize())
        else:
            if new:
                rnd["objections"][rnd["idx"]] = new
            else:  # bank exhausted: the round shrinks
                rnd["objections"].pop(rnd["idx"])
            begin_objection()
            st.rerun()
    if right.button("End round"):
        end_round()
        st.rerun()


def followup_failed_screen():
    objection_header()
    st.error("Couldn't get the pushback. Your opener is saved.")
    if st.button("Retry", type="primary"):
        if call(core.retry_followup, ss.answer_id):
            ss.phase, ss.turn_started = "reply", time.time()
            st.rerun()
    if st.button("Skip to next objection"):
        next_objection()
        st.rerun()


def reply_screen():
    objection_header()
    answer = core.get_answer(ss.answer_id)
    with st.expander("Your opener", expanded=False):
        st.write(answer["opener_text"] or "(nothing transcribed)")
    st.info(f"**Pushback:** {answer['followup_text']}")
    submitted = turn_input("reply", f"{ss.answer_id}_reply")
    if submitted:
        mode, text, data, mime = submitted
        with st.spinner("Judging..."):
            out = call(core.submit_reply, ss.answer_id, mode, text, data, mime or "audio/webm")
        if out:
            if ss.public and out["status"] == "judged":  # the shared disk keeps no recordings
                core.delete_audio(ss.answer_id)
                out = core.judged_out(store.get_answer(ss.answer_id))
            ss.result = out
            ss.phase = "calibrate" if ss.round["calibration"] and out["status"] == "judged" else "feedback"
            st.rerun()


def calibrate_screen():
    objection_header()
    st.subheader("Your scores")
    st.caption("Score the answer yourself before the AI's feedback is shown.")
    with st.form(f"calibrate_{ss.answer_id}"):
        scores = {d: st.slider(DIM_LABELS[d], 1, 5, 3) for d in store.DIMENSIONS}
        seeded = st.checkbox("I seeded a deliberate error")
        if st.form_submit_button("Save and show feedback", type="primary"):
            call(core.calibrate, ss.answer_id, ss.round["reviewer"], scores, seeded)
            ss.phase = "feedback"
            st.rerun()


def feedback_screen():
    objection_header()
    result = ss.result
    if result["status"] != "judged":
        st.error("Judging failed. Both turns are saved.")
        if st.button("Try again", type="primary"):
            with st.spinner("Judging..."):
                out = call(core.rejudge, ss.answer_id)
            if out:
                ss.result = out
                st.rerun()
    else:
        show_feedback(result["answer"])
    last = ss.round["idx"] + 1 >= len(ss.round["objections"])
    if st.button("Round summary" if last else "Next objection", type="primary"):
        next_objection()
        st.rerun()


def summary_screen():
    summary = call(core.round_summary, ss.round["id"])
    if not summary:
        return
    st.subheader("Round summary")
    cols = st.columns(len(store.DIMENSIONS))
    for col, d in zip(cols, store.DIMENSIONS):
        value = summary["dimension_means"][d]
        col.metric(DIM_LABELS[d], "-" if value is None else value)
    if summary["weakest_category"]:
        st.write(f"Weakest category this round: **{summary['weakest_category'].capitalize()}**")
    st.dataframe([{"Objection": o["text"], "Category": o["category"], "Overall": o["overall"],
                   "Status": o["status"]} for o in summary["objections"]], hide_index=True, width="stretch")
    if st.button("New round", type="primary"):
        ss.pop("round", None)
        ss.phase = None
        st.rerun()


def practice_page():
    if not ss.get("round") or not ss.get("phase"):
        start_screen()
        return
    if ss.phase == "summary" and not ss.round["objections"]:
        st.info("No objections left for this category.")
    {"opener": opener_screen, "followup_failed": followup_failed_screen, "reply": reply_screen,
     "calibrate": calibrate_screen, "feedback": feedback_screen, "summary": summary_screen}[ss.phase]()


def history_page():
    h = core.history()
    st.metric("Total spend", f"${h['spend_usd']:.4f}")
    st.subheader("Rounds")
    if h["rounds"]:
        st.dataframe([{"Date": r["started_at"], "Category": (r["category_filter"] or "All").capitalize(),
                       "Mode": "Study" if r["study_mode"] else "Timed", "Answered": r["answered"],
                       "Mean": r["mean_overall"]} for r in h["rounds"]], hide_index=True, width="stretch")
    else:
        st.write("No rounds yet.")
    st.subheader("Category trend (last 10 rounds)")
    st.dataframe([{"Category": t["category"].capitalize(), "Mean overall": t["mean_overall"], "Answers": t["n"]}
                  for t in h["category_trend"]], hide_index=True, width="stretch")
    st.subheader(f"Calibration (prompt {h['prompt_version']})")
    if h["calibration"]:
        st.dataframe([{"Reviewer": c["reviewer"], "Answers": c["answers"], "Within ±1": f"{c['pct']}%",
                       "Seeded flagged": f"{c['seeded_flagged']}/{c['seeded_total']}"} for c in h["calibration"]],
                     hide_index=True, width="stretch")
    else:
        st.write("No calibration answers for this prompt version.")
    if h["pending"]:
        st.subheader("Not judged")
        for p in h["pending"]:
            left, right = st.columns([4, 1])
            left.write(f"{p['objection_text']} ({p['status']})")
            if right.button("Judge", key=f"rejudge_{p['answer_id']}"):
                with st.spinner("Judging..."):
                    call(core.rejudge, p["answer_id"])
                st.rerun()


def main():
    st.title("Sparring Partner")
    ss.public = is_public()
    if not password_ok():
        return
    try:
        boot()
    except BootError as e:
        st.error(str(e))
        return
    if ss.public:  # History shows every visitor's rounds and spend, so it is local only
        practice_page()
        return
    page = st.sidebar.radio("Page", ["Practice", "History"], label_visibility="collapsed")
    st.sidebar.caption(f"Model: {config.GEMINI_MODEL} · tier: {config.GEMINI_TIER}")
    practice_page() if page == "Practice" else history_page()


main()
