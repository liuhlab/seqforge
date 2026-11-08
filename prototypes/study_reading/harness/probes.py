# PROTOTYPE, throwaway: probe Jev's limits on the dev study. The comparisons were fixed before the
# first run; truth is code-parsed from the GSM characteristics (hand-checked against the GEO page).
#
# P1 option order: every sample-level Choice (level per key, assay, role) in two contexts (record with
#    and without characteristics) plus the study-level role/baseline Choices, asked in 4 orders:
#    original, reversed, shuffle seed 1, shuffle seed 2. Report flip rate vs original, first-option
#    pick rate, accuracy per order.
# P2+P3 size x context for level labels (3 factors + donor key, 15 samples, 60 labels):
#    atomic   = one question per request, state = own record (+lists)        [with / without chars]
#    packed   = one request per sample, its 4 questions                       [with / without chars]
#    joint    = one question per sample over the 8 level combinations of the 3 factors [with / without]
#    batch    = one request, state = whole study, 60 questions               [with / without chars]
#    whole1   = state = whole study (no chars), one question per request (does batching change answers?)
# P4 numbers: span pick (time point among regex spans), threshold (>24 h?), unit change (days),
#    pairwise later-than, counting (donors, samples, HIV1 samples), dose span pick and comparison.
# P5 grouping, donor and biosample: pairwise yes/no over all 105 pairs vs one Choice per sample.
import itertools
import json
import random
import re
import sys
import time

from ask import ASSAY_Q, ROLE_Q, keys_from_lists, level_q, record, study_questions
from common import jev, jev_many, load, save
from draft import study_text

GSE = sys.argv[1] if len(sys.argv) > 1 else "GSE201250"
study, lists = load(GSE, "study.json"), load(GSE, "lists.json")
keys = keys_from_lists(lists)
names = list(keys)
S = study["samples"]
truth = {s["gsm"]: {k: v for k, v in s["characteristics"]} for s in S}
FACT = [k for k in names if k != "donor number"]
R = {}


def reorder(q, mode):
    if q["type"] != "choice":
        return q
    items = list(q["criteria"].items())
    if mode == "reversed":
        items = items[::-1]
    elif mode.startswith("shuffle"):
        random.Random(int(mode[-1])).shuffle(items)
    return {**q, "criteria": dict(items)}


def run(jobs, tag):
    t0 = time.time()
    res = jev_many(GSE, jobs, tag)
    wall = time.time() - t0
    tin = sum(m["tokens_in"] for a, m in res)
    secs = [m["seconds"] for a, m in res]
    return [a for a, m in res], {"requests": len(jobs), "questions": sum(len(q) for s, q in jobs),
                                 "tokens_in": tin, "dollars": tin * 0.042 / 1e6,
                                 "mean_request_s": round(sum(secs) / len(secs), 3),
                                 "wall_s_8_parallel": round(wall, 2)}


def acc(rows):
    n = len(rows)
    return {"n": n, "accuracy": round(sum(r["ok"] for r in rows) / n, 3),
            "mean_p_truth": round(sum(r["p_truth"] for r in rows) / n, 3)}


# ---------------------------------------------------------------- P1 option order
def p1():
    orders = ["original", "reversed", "shuffle1", "shuffle2"]
    out = {}
    per = {o: {} for o in orders}
    meta = {}
    for o in orders:
        jobs, ids = [], []
        for ctx in ("chars", "nochar"):
            for s in S:
                qs = {f"level_{i}": level_q(k, keys[k]) for i, k in enumerate(names)}
                qs.update({"assay": ASSAY_Q, "role": ROLE_Q})
                qs = {k: reorder(q, o) for k, q in qs.items()}
                jobs.append(({"sample": record(s, ctx == "chars"), "lists": keys}, qs))
                ids.append((ctx, s["gsm"]))
        sq, _ = study_questions(study, keys, GSE)
        sq = {k: reorder(q, o) for k, q in sq.items() if not k.startswith("onto")}
        jobs.append((study_text(study), sq))
        ids.append(("study", "-"))
        ans, meta[o] = run(jobs, f"p1_order_{o}")
        for (ctx, g), a, (st, qs) in zip(ids, ans, jobs):
            for qn, x in a.items():
                first = next(iter(qs[qn]["criteria"]))
                if ctx == "study":
                    i = int(qn.split("_")[1])
                    t = {"role": "donor" if names[i] == "donor number" else "designed_factor",
                         "baseline": {"donor number": None, "genotype": "control", "infection": "Mock",
                                      "time": "0h"}[names[i]]}[qn.split("_")[0]]
                elif qn.startswith("level"):
                    t = truth[g][names[int(qn.split("_")[1])]]
                else:
                    t = {"assay": "RNA-seq", "role": "signal"}[qn]
                if t is None:
                    continue
                per[o][(ctx, g, qn)] = {"choice": x["choice"], "first": x["choice"] == first,
                                        "ok": x["choice"] == t, "p_truth": x["probabilities"].get(t, 0)}
    base = per["original"]
    for o in orders:
        rows = list(per[o].values())
        flips = [k for k in per[o] if per[o][k]["choice"] != base[k]["choice"]]
        by_ctx = {}
        for (ctx, g, qn), r in per[o].items():
            by_ctx.setdefault(ctx, []).append(r)
        out[o] = {**acc(rows), "flips_vs_original": len(flips),
                  "first_option_rate": round(sum(r["first"] for r in rows) / len(rows), 3),
                  "by_context": {c: acc(v) for c, v in by_ctx.items()},
                  "flipped": [list(k) + [base[k]["choice"], per[o][k]["choice"]] for k in flips][:20],
                  "cost": meta[o]}
    # questions whose answer changed under ANY reorder
    unstable = [k for k in base if len({per[o][k]["choice"] for o in orders}) > 1]
    out["any_flip"] = {"questions": len(base), "unstable": len(unstable),
                       "unstable_ids": [list(k) for k in unstable][:30]}
    R["P1_option_order"] = out


# ---------------------------------------------------------------- P2+P3 size x context
def score_levels(answers_by_label):
    rows = []
    for (g, k), x in answers_by_label.items():
        t = truth[g][k]
        rows.append({"ok": x["choice"] == t, "p_truth": x["probabilities"].get(t, 0), "k": k})
    by = {}
    for r in rows:
        by.setdefault(r["k"], []).append(r)
    return {**acc(rows), "by_key": {k: acc(v)["accuracy"] for k, v in by.items()}}


def p23():
    out = {}
    for ctx in ("chars", "nochar"):
        wc = ctx == "chars"
        # atomic
        jobs, lab = [], []
        for s in S:
            for i, k in enumerate(names):
                jobs.append(({"sample": record(s, wc), "lists": keys}, {"q": level_q(k, keys[k])}))
                lab.append((s["gsm"], k))
        ans, m = run(jobs, f"p2_atomic_{ctx}")
        out[f"atomic_{ctx}"] = {**score_levels({l: a["q"] for l, a in zip(lab, ans)}), "cost": m}
        # packed per sample
        jobs = [({"sample": record(s, wc), "lists": keys},
                 {f"level_{i}": level_q(k, keys[k]) for i, k in enumerate(names)}) for s in S]
        ans, m = run(jobs, f"p2_packed_{ctx}")
        out[f"packed_{ctx}"] = {**score_levels({(s["gsm"], k): a[f"level_{i}"] for s, a in zip(S, ans)
                                                for i, k in enumerate(names)}), "cost": m}
        # joint over the factor combinations
        combos = list(itertools.product(*[keys[k] for k in FACT]))
        cname = [" | ".join(f"{k}={v}" for k, v in zip(FACT, c)) for c in combos]
        jq = {"type": "choice", "instructions": "Which combination of conditions does `sample` have?",
              "criteria": {c: None for c in cname}}
        jobs = [({"sample": record(s, wc), "lists": {k: keys[k] for k in FACT}}, {"joint": jq}) for s in S]
        ans, m = run(jobs, f"p2_joint_{ctx}")
        rows = []
        for s, a in zip(S, ans):
            t = " | ".join(f"{k}={truth[s['gsm']][k]}" for k in FACT)
            rows.append({"ok": a["joint"]["choice"] == t, "p_truth": a["joint"]["probabilities"].get(t, 0)})
        out[f"joint_{ctx}"] = {**acc(rows), "note": "one label = all 3 factors at once; donor not included",
                               "cost": m}
        # batch: whole study state, all labels in one request
        whole = whole_state(wc)
        qs, lab = {}, []
        for s in S:
            for i, k in enumerate(names):
                qs[f"q{len(lab)}"] = level_q(k, keys[k], ref=f"sample {s['gsm']}")
                lab.append((s["gsm"], k))
        ans, m = run([(whole, qs)], f"p2_batch_{ctx}")
        out[f"batch_{ctx}"] = {**score_levels({l: ans[0][f"q{j}"] for j, l in enumerate(lab)}), "cost": m}
        if ctx == "nochar":
            jobs = [(whole, {"q": qs[f"q{j}"]}) for j in range(len(lab))]
            ans1, m = run(jobs, "p2_whole1_nochar")
            out["whole1_nochar"] = {**score_levels({l: a["q"] for l, a in zip(lab, ans1)}), "cost": m}
            same = sum(ans1[j]["q"]["choice"] == ans[0][f"q{j}"]["choice"] for j in range(len(lab)))
            dp = max(abs(ans1[j]["q"]["probabilities"][c] - ans[0][f"q{j}"]["probabilities"][c])
                     for j in range(len(lab)) for c in ans1[j]["q"]["probabilities"])
            out["batch_vs_single_same_state"] = {"same_choice": same, "of": len(lab), "max_abs_dp": round(dp, 3)}
    R["P2_P3_size_context"] = out


def whole_state(with_chars):
    st = {"series": {a: {"title": t["title"], "summary": t["summary"], "overall_design": t["overall_design"]}
                     for a, t in study["series_text"].items()},
          "samples": [record(s, with_chars) for s in S], "lists": keys}
    for r in st["samples"]:
        r.pop("treatment_protocol", None)
    st["treatment_protocol"] = S[0]["protocols"]["treatment"]
    return st


# ---------------------------------------------------------------- P4 numbers
SPAN = re.compile(r"\b\d+(?:\.\d+)?\s*(?:h|hr|hours?|days?|d|mM|%|x 10\^6 cells/ml)\b|\b\d+:\d+\b")


def p4():
    out, jobs, tags = {}, [], []
    tval = {s["gsm"]: float(truth[s["gsm"]]["time"].rstrip("h")) for s in S}
    for s in S:
        rec = record(s, False)
        text = json.dumps(rec)
        spans = sorted(set(m.group(0) for m in SPAN.finditer(text)))
        jobs.append(({"sample": rec}, {
            "span": {"type": "choice", "instructions": "Which text span gives the time point at which `sample` "
                     "was collected, counted from the HIV-1 challenge or mock?", "criteria": {x: None for x in spans}},
            "gt24": {"type": "noul", "instructions": "Was `sample` collected more than 24 hours after the HIV-1 "
                     "challenge or mock treatment began?"},
            "days": {"type": "choice", "instructions": "How many days after the HIV-1 challenge or mock treatment "
                     "began was `sample` collected?", "criteria": {"0 days": None, "1 day": None, "2 days": None,
                                                                   "3 days": None}},
        }))
        tags.append(s["gsm"])
    ans, m = run(jobs, "p4_numbers_sample")
    sp, gt, dy = [], [], []
    for g, a in zip(tags, ans):
        t = truth[g]["time"]
        sp.append({"ok": a["span"]["choice"].replace(" ", "") == t, "p_truth": 0})
        gt.append({"ok": (a["gt24"]["noul"] > 0.5) == (tval[g] > 24), "p_truth": a["gt24"]["noul"] if tval[g] > 24
                   else 1 - a["gt24"]["noul"]})
        d = f"{int(tval[g] // 24)} day" + ("s" if tval[g] // 24 != 1 else "")
        dy.append({"ok": a["days"]["choice"] == d, "p_truth": a["days"]["probabilities"].get(d, 0)})
    out["span_pick_time"] = {**acc(sp), "note": "pick the time span among regex-found spans (protocol says 48 hours "
                             "for all; title says 0h for three)", "spans_offered_example": list(jobs[0][1]["span"]["criteria"])}
    out["threshold_gt_24h"] = acc(gt)
    out["unit_change_days"] = acc(dy)
    out["cost_sample_level"] = m
    # pairwise later-than
    pj, pt = [], []
    for a, b in itertools.permutations(S, 2):
        if tval[a["gsm"]] != tval[b["gsm"]]:
            pj.append(({"sample_a": record(a, False), "sample_b": record(b, False)},
                       {"later": {"type": "noul", "instructions": "Was `sample_a` collected at a later time point "
                                  "than `sample_b`?"}}))
            pt.append(tval[a["gsm"]] > tval[b["gsm"]])
    ans, m = run(pj, "p4_later_pairs")
    rows = [{"ok": (x["later"]["noul"] > 0.5) == t, "p_truth": x["later"]["noul"] if t else 1 - x["later"]["noul"]}
            for x, t in zip(ans, pt)]
    out["pairwise_later_than"] = {**acc(rows), "cost": m}
    # counting and dose, study level, original and reversed option order
    whole = whole_state(True)
    proto = S[0]["protocols"]["growth"]
    cq = {
        "n_donors": ({"type": "choice", "instructions": "How many different blood donors provided cells for this study?",
                      "criteria": {str(i): None for i in range(1, 7)}}, "3"),
        "n_samples": ({"type": "choice", "instructions": "How many samples are listed in `samples`?",
                       "criteria": {str(i): None for i in range(10, 21)}}, "15"),
        "n_hiv": ({"type": "choice", "instructions": "How many samples in `samples` were challenged with HIV-1?",
                   "criteria": {str(i): None for i in range(0, 11)}}, "6"),
    }
    dq = {
        "hepes": ({"type": "choice", "instructions": "What is the HEPES concentration in the culture medium?",
                   "criteria": {x: None for x in sorted(set(SPAN.findall(proto)))}}, "25 mM"),
        "more": ({"type": "choice", "instructions": "Which is present at the higher concentration in the culture "
                  "medium: GlutaMAX or HEPES?", "criteria": {"GlutaMAX": None, "HEPES": None}}, "HEPES"),
    }
    for order in ("original", "reversed"):
        a1, m1 = run([(whole, {k: reorder(q, order) for k, (q, t) in cq.items()}),
                      ({"growth_protocol": proto}, {k: reorder(q, order) for k, (q, t) in dq.items()})],
                     f"p4_count_dose_{order}")
        res = {}
        for k, (q, t) in {**cq, **dq}.items():
            x = (a1[0] if k in cq else a1[1])[k]
            res[k] = {"truth": t, "choice": x["choice"], "p_truth": x["probabilities"].get(t, 0),
                      "p_choice": x["probabilities"][x["choice"]]}
        out[f"count_and_dose_{order}"] = res
    R["P4_numbers"] = out


# ---------------------------------------------------------------- P5 grouping
def transitivity_violations(n_yes):
    v = 0
    for a, b, c in itertools.combinations([s["gsm"] for s in S], 3):
        e = [n_yes.get((a, b)), n_yes.get((b, c)), n_yes.get((a, c))]
        if sum(e) == 2:
            v += 1
    return v


def p5():
    out = {}
    donor = {s["gsm"]: truth[s["gsm"]]["donor number"] for s in S}
    pairs = list(itertools.combinations(S, 2))
    for kind, instr, tfun in [
        ("donor", "Did `sample_a` and `sample_b` come from the same blood donor?",
         lambda a, b: donor[a] == donor[b]),
        ("biosample", "Are `sample_a` and `sample_b` two libraries made from the same biological material (one cell "
                      "prep harvested once), rather than separate samples?", lambda a, b: False),
    ]:
        for ctx in ("chars", "nochar"):
            jobs = [({"sample_a": record(a, ctx == "chars"), "sample_b": record(b, ctx == "chars")},
                     {"q": {"type": "noul", "instructions": instr}}) for a, b in pairs]
            ans, m = run(jobs, f"p5_pair_{kind}_{ctx}")
            yes, rows = {}, []
            for (a, b), x in zip(pairs, ans):
                t = tfun(a["gsm"], b["gsm"])
                p = x["q"]["noul"]
                yes[(a["gsm"], b["gsm"])] = p > 0.5
                rows.append({"ok": (p > 0.5) == t, "p_truth": p if t else 1 - p})
            out[f"pairwise_{kind}_{ctx}"] = {**acc(rows), "transitivity_violations": transitivity_violations(yes),
                                             "false_yes": sum(1 for r, (a, b) in zip(rows, pairs) if not r["ok"] and
                                                              not tfun(a["gsm"], b["gsm"])), "cost": m}
    # choice per sample: donor among the listed donor ids; biosample partner among the other samples
    for ctx in ("chars", "nochar"):
        jobs = []
        for s in S:
            others = {o["gsm"]: o["title"] for o in S if o["gsm"] != s["gsm"]}
            others["none"] = "no other sample was made from the same material"
            jobs.append(({"sample": record(s, ctx == "chars"), "donors": keys.get("donor number")}, {
                "donor": level_q("donor", keys["donor number"]),
                "partner": {"type": "choice", "instructions": "Which other sample was made from the same biological "
                            "material (one cell prep harvested once) as `sample`?", "criteria": others}}))
            jobs[-1][0]["other_samples"] = {o["gsm"]: o["title"] for o in S if o["gsm"] != s["gsm"]}
        ans, m = run(jobs, f"p5_choice_{ctx}")
        dr = [{"ok": a["donor"]["choice"] == donor[s["gsm"]], "p_truth": a["donor"]["probabilities"].get(donor[s["gsm"]], 0)}
              for s, a in zip(S, ans)]
        br = [{"ok": a["partner"]["choice"] == "none", "p_truth": a["partner"]["probabilities"].get("none", 0)}
              for s, a in zip(S, ans)]
        out[f"choice_{ctx}"] = {"donor": acc(dr), "biosample_partner": acc(br),
                                "partner_picks": [a["partner"]["choice"] for a in ans], "cost": m}
    R["P5_grouping"] = out


# ---------------------------------------------------------------- P4b mixed-unit numbers (added after P4
# came back all-correct: the dev study's two time points are too easy to show the limit)
MIXED = ["30 min", "90 min", "0.5h", "2h", "1 day", "36h", "2d", "8 weeks", "P7", "E14.5"]
HOURS = {"30 min": 0.5, "90 min": 1.5, "0.5h": 0.5, "2h": 2, "1 day": 24, "36h": 36, "2d": 48, "8 weeks": 1344}
AGE_DAYS = {"P7": 7, "E14.5": -4.5, "8 weeks": 56}


def p4b():
    out = {}
    tl = [x for x in HOURS]
    jobs, truth_ = [], []
    for a, b in itertools.permutations(tl, 2):
        if HOURS[a] == HOURS[b]:
            continue
        jobs.append(({"time_point_a": a, "time_point_b": b},
                     {"q": {"type": "noul", "instructions": "Is `time_point_a` a later time after treatment than `time_point_b`?"}}))
        truth_.append(HOURS[a] > HOURS[b])
    ans, m = run(jobs, "p4b_time_pairs")
    rows = [{"ok": (x["q"]["noul"] > 0.5) == t, "p_truth": x["q"]["noul"] if t else 1 - x["q"]["noul"]} for x, t in zip(ans, truth_)]
    wrong = [(j[0]["time_point_a"], j[0]["time_point_b"], round(x["q"]["noul"], 2)) for j, x, r in zip(jobs, ans, rows) if not r["ok"]]
    out["mixed_unit_later_than"] = {**acc(rows), "wrong": wrong, "cost": m, "code_parse_accuracy": 1.0}
    jobs, truth_ = [], []
    for a, b in itertools.permutations(AGE_DAYS, 2):
        jobs.append(({"age_a": a, "age_b": b, "note": "mouse ages; P = postnatal day, E = embryonic day"},
                     {"q": {"type": "noul", "instructions": "Is the mouse at `age_a` older than the mouse at `age_b`?"}}))
        truth_.append(AGE_DAYS[a] > AGE_DAYS[b])
    ans, m = run(jobs, "p4b_age_pairs")
    rows = [{"ok": (x["q"]["noul"] > 0.5) == t, "p_truth": x["q"]["noul"] if t else 1 - x["q"]["noul"]} for x, t in zip(ans, truth_)]
    out["age_older_than"] = {**acc(rows), "wrong": [(j[0]["age_a"], j[0]["age_b"], round(x["q"]["noul"], 2)) for j, x, r in zip(jobs, ans, rows) if not r["ok"]]}
    # ordering as one Choice: which is the latest
    q = {"type": "choice", "instructions": "Which of these time points after treatment is the latest?",
         "criteria": {x: None for x in ["36h", "1 day", "2d", "90 min", "2h"]}}
    for order in ("original", "reversed"):
        a1, _ = run([({"note": "time points after treatment"}, {"q": reorder(q, order)})], f"p4b_latest_{order}")
        out[f"latest_of_5_{order}"] = {"truth": "2d", "choice": a1[0]["q"]["choice"], "probabilities": a1[0]["q"]["probabilities"]}
    R["P4b_mixed_numbers"] = out


if __name__ == "__main__":
    which = sys.argv[2:] or ["p1", "p23", "p4", "p4b", "p5"]
    for w in which:
        t0 = time.time()
        globals()[w]()
        print(w, "done", round(time.time() - t0, 1), "s", flush=True)
    old = {}
    try:
        old = load(GSE, "probes.json")
    except FileNotFoundError:
        pass
    old.update(R)
    save(GSE, "probes.json", old)
