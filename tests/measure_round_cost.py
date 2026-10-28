"""Live cost measurement: one full round of 5 typed objections through `core`, as a user would play it.

For each objection: core.start_answer (opener, follow-up call), core.submit_reply (reply, judge call).
The tokens and cost printed are the ones core and store recorded on each answer row (summed per
objection and for the round); a wrapper around judge._send also counts every Gemini request,
including retries, as a cross-check.

Safe by design: the database, audio and log directories are pointed at a temp directory before
core.init(), so data/sparring.db and logs/app.log are never touched. data/objections.json is only
read; the in-memory bank is narrowed to the five objections below so the round is reproducible.
The answers are written for those five items (about 80-120 words each). Answer text is never printed.

Run: python tests/measure_round_cost.py   (live Gemini calls, about 10 calls, roughly 1 cent)
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="sparring_cost_"))
config.DATA_DIR = TMP
config.AUDIO_DIR = TMP / "audio"
config.DB_PATH = TMP / "cost.db"
config.LOG_DIR = TMP / "logs"

import core  # noqa: E402
import judge  # noqa: E402
import store  # noqa: E402

ROUND = ["nature-01", "health-02", "ethics-03", "environment-02", "personal-08"]

ANSWERS = {
    "nature-01": {
        "opener": (
            "Lions and wolves eat meat because they have no alternative, and they cannot step back and ask "
            "whether it is right. They are not moral agents, so we don't judge them either way. We are "
            "different: we can reflect on what our food choices cost others, and in most of the world we can "
            "do well without animal products. I'd happily concede that eating meat is natural and that our "
            "ancestors hunted. But natural is not the same as justified. Plenty of natural behaviour, such "
            "as infanticide in some species, is something we would never defend in people. So the question "
            "is not what is natural but what we can reasonably choose."),
        "reply": (
            "I agree that our ancestors hunted, and I'm not claiming they did anything wrong given what they "
            "knew and what they had available. Survival was at stake and there were no alternatives. The "
            "situation has changed. Most of us have supermarkets, fortified foods and good nutritional "
            "advice, so killing animals is no longer necessary for our health. When something stops being "
            "necessary, the only reason left is taste and habit, and that is a much weaker justification for "
            "causing suffering and death. That change is why it can be wrong now when it wasn't then."),
    },
    "health-02": {
        "opener": (
            "Yes, B12 is genuinely required on a vegan diet, and I'd say so plainly: vegans need a "
            "supplement or fortified foods, and it isn't optional. But that doesn't show the diet is "
            "unhealthy. B12 is made by microorganisms, not by animals or plants. Most farmed livestock are "
            "given B12 through their feed or supplements, so a lot of meat eaters get their B12 second hand "
            "from a supplement too. Plenty of other people also rely on fortified foods such as cereals and "
            "milk. Relying on fortification is common, and the major dietetic bodies say a well-planned "
            "vegan diet can suit every stage of life, as long as B12 is covered."),
        "reply": (
            "I'm not saying the risk is imaginary. Deficiency can do serious harm, which is exactly why "
            "vegans should treat B12 as non-negotiable and take a reliable supplement or eat fortified "
            "foods every day. The same is true for anyone eating a restricted or poorly planned diet, vegan "
            "or not. What matters is whether the diet is planned properly, with a regular source of B12 and "
            "a blood test now and then. The major dietetic organisations support well-planned vegan diets "
            "for all ages, including pregnancy and childhood, with that guidance. So it needs care and "
            "professional advice, not an assumption that it can't work."),
    },
    "ethics-03": {
        "opener": (
            "I agree it is different, and I'd rather see animals live well than in a factory farm. A good "
            "life with a quick death is clearly better than months of suffering, and I won't pretend "
            "otherwise. But I think two problems remain. First, the animal still dies when it didn't need "
            "to, and it would have gone on living. Taking that life is a harm even if the death is painless. "
            "Second, there is not nearly enough high-welfare supply for everyone, labels don't always match "
            "reality, and breeding, transport and slaughter still raise welfare problems. So it is better, "
            "but it doesn't settle the question of killing."),
        "reply": (
            "Fair enough, I'm not claiming your farm is the same as a factory. Welfare really does differ "
            "enormously between systems, and buying from a good farm is a better choice than buying the "
            "cheapest supermarket meat. My point is narrower. If we agree it would be wrong to kill a "
            "healthy dog or cat that was enjoying its life just because we fancied eating it, then a good "
            "life doesn't by itself justify the killing. The animal has a stake in staying alive. And on a "
            "practical level, most people who say they buy high-welfare meat eat far more than that supply "
            "could cover."),
    },
    "environment-02": {
        "opener": (
            "You're right that soy expansion is linked to deforestation in parts of South America, and I "
            "won't dismiss that. But look at where the soy actually goes. Most of the world's soy, roughly "
            "three quarters, is fed to animals, mainly chickens, pigs and farmed fish, and only a small "
            "share becomes tofu and soy milk. So eating the soy directly takes far less land than eating "
            "the animals that were fed it. Cattle ranching is also a bigger driver of Amazon deforestation "
            "than soy, and soy for human food can come from deforestation-free supply chains, including "
            "European or North American growers. Going vegan cuts the pressure; it doesn't add to it."),
        "reply": (
            "I'm happy to accept that tofu and soy milk aren't impact free. No food is. What matters is the "
            "comparison. A person eating a plant-based diet needs far less cropland than someone eating "
            "meat, because feeding crops to animals wastes most of the calories on the way. If you are "
            "worried about the Amazon, the most effective step is to cut the demand for feed and beef "
            "together. And you can choose soy that is certified deforestation-free or grown in Europe, "
            "which many brands now do. So I'd say the argument counts against animal feed far more than "
            "against vegans."),
    },
    "personal-08": {
        "opener": (
            "It's true that no single purchase can be traced to one animal, and I'd admit that the market "
            "is diffuse and uncertain. But it isn't zero. Supply chains respond to demand in large steps, "
            "so each purchase has a small chance of tipping a decision, such as an extra order or a bigger "
            "batch, and a large effect when it does. Averaged out, that expected effect is close to the "
            "harm of the purchase itself. And even if you think a single choice never matters, there is "
            "still integrity and complicity. If I think something is wrong, I don't want to pay for it "
            "just because I'm one of millions."),
        "reply": (
            "I'll grant that causal impact is a real puzzle in philosophy and that nobody can measure the "
            "effect of one person's diet. But think about how we treat other cases. One voter or one "
            "litter-dropper makes almost no difference either, yet we still think voting and not littering "
            "are the right thing to do. Part of the answer is expected value, and part is that you shouldn't "
            "take part in something you think is wrong, even when your share is tiny. Movements are made of "
            "individuals deciding that way, so changing my own choices is also how change spreads."),
    },
}

CALLS = []  # one entry per Gemini request (retries included), recorded by the wrapper below
_real_send = judge._send


def _counting_send(model, system, parts, schema, max_out):
    raw = _real_send(model, system, parts, schema, max_out)
    CALLS.append({"prompt": raw["prompt"], "cand": raw["cand"], "thought": raw["thought"]})
    return raw


def check_answers() -> list[str]:
    """Word counts must sit in the 80-120 band so the round is realistic."""
    problems = []
    for oid, turns in ANSWERS.items():
        for turn, text in turns.items():
            n = len(text.split())
            if not 80 <= n <= 120:
                problems.append(f"{oid} {turn}: {n} words")
    return problems


def play_round() -> list[dict]:
    core.init()
    missing = [oid for oid in ROUND if oid not in core.BANK]
    if missing:
        raise SystemExit(f"objections missing from the bank: {missing}")
    for oid in list(core.BANK):  # in memory only; the bank file is not written
        if oid not in ROUND:
            del core.BANK[oid]
    rnd = core.new_round(None)
    rows = []
    for item in rnd["objections"]:
        oid = item["id"]
        started = core.start_answer(rnd["round_id"], oid, "type", text=ANSWERS[oid]["opener"])
        result = core.submit_reply(started["answer_id"], "type", text=ANSWERS[oid]["reply"])
        rows.append(store.get_answer(started["answer_id"]) | {"judge_status": result["status"]})
    return rows


def main() -> int:
    bad = check_answers()
    if bad:
        print("answer length problems:", "; ".join(bad))
        return 1
    judge._send = _counting_send
    try:
        rows = play_round()
    finally:
        judge._send = _real_send
    print(f"model={config.GEMINI_MODEL} prompt_version={judge.PROMPT_VERSION} db={config.DB_PATH.name} (temp)")
    print(f"{'objection':<16} {'status':<8} {'overall':<8} {'in':>7} {'out':>7} {'cost_usd':>10}")
    for r in rows:
        print(f"{r['objection_id']:<16} {r['status']:<8} {str(r['overall']):<8} {r['input_tokens']:>7} "
              f"{r['output_tokens']:>7} {r['cost_usd']:>10.6f}")
    tot_in = sum(r["input_tokens"] for r in rows)
    tot_out = sum(r["output_tokens"] for r in rows)
    tot_cost = sum(r["cost_usd"] for r in rows)
    wrapped_in = sum(c["prompt"] for c in CALLS)
    wrapped_out = sum(c["cand"] + c["thought"] for c in CALLS)
    print(f"\nround total: in={tot_in} out={tot_out} cost=${tot_cost:.6f}  "
          f"per objection: in={tot_in / len(rows):.0f} out={tot_out / len(rows):.0f} cost=${tot_cost / len(rows):.6f}")
    print(f"gemini requests (retries included): {len(CALLS)}  wrapper tokens in={wrapped_in} out={wrapped_out}")
    print(f"store spend_since today: ${store.total_spend():.6f}")
    ok = all(r["status"] == "judged" for r in rows) and (tot_in, tot_out) == (wrapped_in, wrapped_out)
    shutil.rmtree(TMP, ignore_errors=True)
    print("RESULT: PASS" if ok else "RESULT: CHECK (a row was not judged or recorded tokens differ from the wrapper)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
