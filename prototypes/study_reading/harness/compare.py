# PROTOTYPE, throwaway: score a model reading against the hand reading (gold), field by field.
#   pixi run python compare.py GSE164798 [reading.v1.yaml]      -> runs/<GSE>/compare.<name>.json + a printed summary
#   pixi run python compare.py all [reading.v1.yaml]            -> all three studies + calibration over all of them
# Levels are matched by meaning: same spelling after normalising, else the gold level the model level co-occurs with
# most (one to one); the mapping is printed so a reader can check it. Gold labels under `unsure` are scored apart.
import itertools
import json
import re
import sys

import yaml

from common import RUNS

GOLD = RUNS.parent / "gold"
STUDIES = ["GSE164798", "GSE190102", "GSE206603"]
STUDY_LEVEL = {"attr:sample_type"}
SYN = {"m": "male", "f": "female", "wildtype": "wt", "diseasestate": "disease", "celltype": "celltype", "chipantibody": "target"}


def n(s):
    s = re.sub(r"[^a-z0-9]", "", str(s).lower()) if s is not None else None
    return SYN.get(s, s)


def same_text(a, b):
    """Equal after normalising, or one contains the other when the shorter has 3+ characters ('M' is not in 'female')."""
    a, b = n(a), n(b)
    if a == b:
        return True
    s, l = sorted((a, b), key=len)
    return len(s) >= 3 and s in l


def map_levels(pairs):
    """pairs: [(model value, gold value)] over samples -> {model value: gold value}, one to one."""
    out, used = {}, set()
    for m, g in pairs:  # same spelling first
        if m is not None and g is not None and n(m) == n(g):
            out[m] = g
            used.add(g)
    cnt = {}
    for m, g in pairs:
        if m is not None and g is not None and m not in out and g not in used:
            cnt[(m, g)] = cnt.get((m, g), 0) + 1
    for (m, g), _ in sorted(cnt.items(), key=lambda x: -x[1]):
        if m not in out and g not in used:
            out[m] = g
            used.add(g)
    return out


def neigh(rows, key):
    """gsm -> frozenset of other gsms with the same value of key (None = no claim: singleton)."""
    by = {}
    for r in rows:
        if r.get(key) is not None:
            by.setdefault(r[key], set()).add(r["gsm"])
    return {r["gsm"]: frozenset(by.get(r.get(key), {r["gsm"]}) - {r["gsm"]}) for r in rows}


def pr(model, gold):
    m, g = set(model), set(gold)
    tp = len(m & g)
    return {"model": len(m), "gold": len(g), "match": tp,
            "precision": round(tp / len(m), 3) if m else None, "recall": round(tp / len(g), 3) if g else None}


def unsure_of(s):
    return {u["label"] for u in (s.get("unsure") or [])}


def compare(gse, name="reading.yaml"):
    G = yaml.safe_load(open(GOLD / f"{gse}.yaml"))
    M = yaml.safe_load(open(RUNS / gse / name))
    gs = {s["gsm"]: s for s in G["samples"]}
    ms = {s["gsm"]: s for s in M["samples"]}
    common = [g for g in gs if g in ms]
    res = {"gse": gse, "reading": name, "samples": {"gold": len(gs), "model": len(ms), "both": len(common)}}
    # study boundary
    res["study"] = {k: {"gold": G["study"][k], "model": M["study"][k],
                        "equal": (sorted(G["study"][k]) == sorted(M["study"][k])) if isinstance(G["study"][k], list)
                        else G["study"][k] == M["study"][k]}
                    for k in ("series", "superseries", "bioproject", "pmid")}
    # factors: pair each gold factor with the model factor whose values agree best over samples
    fmap, lmap = {}, {}
    for gf in G["factors"]:
        best = None
        for mf in M["factors"]:
            pairs = [(ms[x]["factors"].get(mf["name"]), gs[x]["factors"].get(gf["name"])) for x in common]
            mp = map_levels(pairs)
            agree = sum(1 for a, b in pairs if a is not None and mp.get(a) == b)
            if best is None or agree > best[0]:
                best = (agree, mf, mp)
        if best and best[0] > 0:
            fmap[gf["name"]] = best[1]["name"]
            lmap[gf["name"]] = best[2]
    fres = []
    for gf in G["factors"]:
        mfn = fmap.get(gf["name"])
        mf = next((f for f in M["factors"] if f["name"] == mfn), None)
        mp = lmap.get(gf["name"], {})
        fres.append({
            "gold": gf["name"], "model": mfn, "level_map": mp,
            "levels_equal": mf is not None and sorted(mp.get(v, v) for v in mf["levels"]) == sorted(gf["levels"]),
            "baseline_equal": mf is not None and mp.get(mf["baseline"]) == gf["baseline"],
            "ordered_equal": mf is not None and bool(mf["ordered"]) == bool(gf["ordered"]),
            "probability": (mf or {}).get("probability"),
        })
    extra = [f["name"] for f in M["factors"] if f["name"] not in fmap.values()]
    res["factors"] = {"per_gold_factor": fres, "missing": [f["gold"] for f in fres if f["model"] is None],
                      "extra_model_factors": extra}
    # per-sample labels; calib collects (field, probability, correct) for every Jev-backed label
    calib, field, errors, study_cal = [], {}, [], {}
    # one Jev answer per study, copied to every sample: one calibration point, not one per sample
    for f in fres:
        if f["probability"]:
            calib.append({"gse": gse, "field": "study:factor_role", "p": f["probability"]["role"], "correct": True,
                          "unsure": False})
            calib.append({"gse": gse, "field": "study:baseline", "p": f["probability"]["baseline"],
                          "correct": f["baseline_equal"], "unsure": False})
    for mf in M["factors"]:
        if mf["name"] in extra and mf.get("probability"):
            calib.append({"gse": gse, "field": "study:factor_role", "p": mf["probability"]["role"], "correct": False,
                          "unsure": False})

    def score(fld, x, ok, prob, unsure, got, want):
        d = field.setdefault(fld, {"right": 0, "wrong": 0, "unsure_right": 0, "unsure_wrong": 0})
        k = ("unsure_" if unsure else "") + ("right" if ok else "wrong")
        d[k] += 1
        if fld in STUDY_LEVEL or (fld == "donor" and (ms[x].get("probability") or {}).get("donor_study") is not None):
            p = prob if prob is not None else (ms[x].get("probability") or {}).get("donor_study")
            if p is not None:
                study_cal.setdefault(fld, [p, True])[1] &= ok
        elif prob is not None:
            calib.append({"gse": gse, "field": fld, "p": prob, "correct": ok, "unsure": unsure})
        if not ok:
            errors.append({"gsm": x, "field": fld, "model": got, "gold": want, "p": prob, "unsure": unsure})

    gdon, mdon = neigh(G["samples"], "donor"), neigh(M["samples"], "donor")
    gbio, mbio = neigh(G["samples"], "biosample"), neigh(M["samples"], "biosample")
    for x in common:
        g, m, u = gs[x], ms[x], unsure_of(gs[x])
        mp = m.get("probability") or {}
        for gf in G["factors"]:
            mfn = fmap.get(gf["name"])
            got = m["factors"].get(mfn) if mfn else None
            got_m = lmap.get(gf["name"], {}).get(got, got)
            score(f"factor:{gf['name']}", x, got_m == g["factors"].get(gf["name"]), mp.get(mfn), gf["name"] in u,
                  got, g["factors"].get(gf["name"]))
        score("donor", x, gdon[x] == mdon[x] and m.get("donor") is not None, mp.get("donor"), "donor" in u,
              sorted(mdon[x]) if m.get("donor") else "not stated", sorted(gdon[x]))
        score("biosample", x, gbio[x] == mbio[x], None, "biosample" in u, sorted(mbio[x]), sorted(gbio[x]))
        score("assay", x, n(m["assay"]) == n(g["assay"]), mp.get("assay"), "assay" in u, m["assay"], g["assay"])
        score("target", x, n(m["target"]) == n(g["target"]), mp.get("target"), "target" in u, m["target"], g["target"])
        score("role", x, m["role"] == g["role"], mp.get("role"), "role" in u, m["role"], g["role"])
        score("control_type", x, n(m["control_type"]) == n(g["control_type"]), mp.get("control_type"),
              "control_type" in u, m["control_type"], g["control_type"])
        gfn = {f["name"] for f in G["factors"]}
        mattr = {n(k): v for k, v in (m.get("attributes") or {}).items()}
        for k, v in (g.get("attributes") or {}).items():
            if k in gfn:
                continue
            mv = mattr.get(n(k))
            ok = mv is not None and same_text(v["text"], mv["text"])
            score(f"attr:{k}", x, ok, mp.get(f"attr:{k}"), f"attr:{k}" in u, mv and mv["text"], v["text"])
            if v.get("id") and mv is not None:
                score(f"attr_id:{k}", x, mv.get("id") == v["id"], None, False, mv.get("id"), v["id"])
    for fld, (p, ok) in study_cal.items():
        calib.append({"gse": gse, "field": f"study:{fld}", "p": p, "correct": ok, "unsure": False})
    res["fields"] = field
    # relations
    GR, MR = G["relations"], M["relations"]
    inv = {mfn: gfn for gfn, mfn in fmap.items()}

    def ckey(c, mapped):
        f = inv.get(c["factor"], c["factor"]) if mapped else c["factor"]
        return (f, frozenset(c["test"]), frozenset(c["baseline"]))

    def pairs_of(groups):
        return {frozenset(p) for grp in groups for p in itertools.combinations(sorted(grp), 2)}

    unsure_bio = {x for x in gs if "biosample" in unsure_of(gs[x])}
    cm_g, cm_m = pairs_of(GR["co_measurement"]), pairs_of(MR["co_measurement"])
    res["relations"] = {
        "contrasts": pr({ckey(c, True) for c in MR["contrasts"]}, {ckey(c, False) for c in GR["contrasts"]}),
        "replicates": pr({frozenset(g) for g in MR["replicates"]}, {frozenset(g) for g in GR["replicates"]}),
        "series": pr({frozenset(s["members"]) for s in MR["series"]}, {frozenset(s["members"]) for s in GR["series"]}),
        "co_measurement_groups": pr({frozenset(g) for g in MR["co_measurement"]}, {frozenset(g) for g in GR["co_measurement"]}),
        "co_measurement_pairs": pr(cm_m, cm_g),
        "co_measurement_pairs_settled": pr({p for p in cm_m if not p & unsure_bio}, {p for p in cm_g if not p & unsure_bio}),
        "co_measurement_pairs_claimed_among_unsure": len({p for p in cm_m if p & unsure_bio}),
        "background_control": pr({(b["signal"], b["control"]) for b in MR["background_control"]},
                                 {(b["signal"], b["control"]) for b in GR["background_control"]}),
        "donor_pairs": pr(pairs_of(_groups(M["samples"], "donor")), pairs_of(_groups(G["samples"], "donor"))),
    }
    # biosample yes/no answers from Jev, scored against the gold's same-biosample pairs
    ans_file = {"reading.yaml": "answers.json", "reading.v1.yaml": "answers.v1.json", "reading.v2.yaml": "answers.v2.json"}.get(name)
    if ans_file and (RUNS / gse / ans_file).exists():
        A = json.loads((RUNS / gse / ans_file).read_text())
        for pair, p in A.get("biosample_pairs", {}).items():
            a, b = pair.split("|")
            same = gs[a]["biosample"] == gs[b]["biosample"]
            ok = (p > 0.5) == same
            uns = a in unsure_bio or b in unsure_bio
            calib.append({"gse": gse, "field": "biosample_pair", "p": max(p, 1 - p), "correct": ok, "unsure": uns,
                          "p_yes": p, "gold_same": same})
            if not ok:
                errors.append({"gsm": pair, "field": "biosample_pair", "model": round(p, 3), "gold": same, "p": p,
                               "unsure": uns})
        for i, (k) in enumerate(A["keys"]):
            pass
    res["errors"] = errors
    res["calibration"] = calib
    stem = name[len("reading."):-len(".yaml")] if name != "reading.yaml" else ""
    out = RUNS / gse / (f"compare.{stem}.json" if stem else "compare.json")
    out.write_text(json.dumps(res, indent=1, default=list))
    return res


def _groups(rows, key):
    by = {}
    for r in rows:
        if r.get(key) is not None:
            by.setdefault(r[key], []).append(r["gsm"])
    return list(by.values())


def bins(calib):
    edges = [(0, 0.7), (0.7, 0.9), (0.9, 0.99), (0.99, 1.01)]
    out = []
    for lo, hi in edges:
        xs = [c for c in calib if lo <= c["p"] < hi and not c["unsure"]]
        out.append({"p": f"[{lo}, {min(hi, 1)})" if hi < 1 else f"[{lo}, 1]", "n": len(xs),
                    "right": sum(c["correct"] for c in xs),
                    "accuracy": round(sum(c["correct"] for c in xs) / len(xs), 3) if xs else None})
    return out


def summary(r):
    print(f"\n## {r['gse']} ({r['reading']})  samples gold {r['samples']['gold']} model {r['samples']['model']}")
    print("study:", {k: v["equal"] for k, v in r["study"].items()})
    for f in r["factors"]["per_gold_factor"]:
        print(f"factor {f['gold']} <- {f['model']}: levels {f['levels_equal']} baseline {f['baseline_equal']} "
              f"ordered {f['ordered_equal']} map {f['level_map']}")
    print("extra model factors:", r["factors"]["extra_model_factors"])
    for k, d in r["fields"].items():
        tot = d["right"] + d["wrong"]
        print(f"  {k:28s} {d['right']}/{tot}" + (f"   unsure: {d['unsure_right']} agree, {d['unsure_wrong']} differ"
                                                  if d["unsure_right"] + d["unsure_wrong"] else ""))
    for k, v in r["relations"].items():
        print(f"  rel {k:40s} {v}")


if __name__ == "__main__" and "--table" not in sys.argv:
    name = sys.argv[2] if len(sys.argv) > 2 else "reading.yaml"
    todo = STUDIES if sys.argv[1] == "all" else [sys.argv[1]]
    allc = []
    for g in todo:
        r = compare(g, name)
        summary(r)
        allc += r["calibration"]
    print("\ncalibration, settled labels only (Jev's probability of its pick vs right/wrong):")
    for b in bins(allc):
        print("  ", b)
    by = {}
    for c in allc:
        by.setdefault(c["field"].split(":")[0], []).append(c)
    for f, xs in sorted(by.items()):
        print(f"  {f:16s}", " | ".join(f"{b['p']} {b['right']}/{b['n']}" for b in bins(xs) if b["n"]))
    wrong = sorted((c for c in allc if not c["correct"] and not c["unsure"]), key=lambda c: c["p"])
    print("wrong settled answers, by probability:", [(c["gse"], c["field"], c["p"]) for c in wrong])


def table(names=("reading.v0-asis-code-lists.yaml", "reading.v0-asis-deepseek-lists.yaml", "reading.v1.yaml",
                 "reading.v2.yaml")):
    """One markdown table per study: rows = fields and relations, columns = harness versions."""
    for g in STUDIES:
        rs = [compare(g, nm) for nm in names]
        cols = [nm.replace("reading.", "").replace(".yaml", "") for nm in names]
        print(f"\n### {g}\n\n| | " + " | ".join(cols) + " |\n|---|" + "---|" * len(cols))
        fac = []
        for r in rs:
            fs = r["factors"]["per_gold_factor"]
            ok = sum(f["model"] is not None and f["levels_equal"] and f["baseline_equal"] and f["ordered_equal"] for f in fs)
            fac.append(f"{ok}/{len(fs)} right, {len(r['factors']['extra_model_factors'])} extra")
        print("| factors (levels, baseline, ordered) | " + " | ".join(fac) + " |")
        keys = []
        for r in rs:
            keys += [k for k in r["fields"] if k not in keys and not k.startswith("attr")]
        for k in keys:
            cells = []
            for r in rs:
                d = r["fields"].get(k)
                if not d:
                    cells.append("-")
                    continue
                c = f"{d['right']}/{d['right'] + d['wrong']}"
                if d["unsure_right"] + d["unsure_wrong"]:
                    c += f" (unsure: {d['unsure_right']} agree, {d['unsure_wrong']} differ)"
                cells.append(c)
            print(f"| {k} | " + " | ".join(cells) + " |")
        cells = []
        for r in rs:
            a = [d for k, d in r["fields"].items() if k.startswith("attr:")]
            cells.append(f"{sum(d['right'] for d in a)}/{sum(d['right'] + d['wrong'] for d in a)}")
        print("| attributes, text (all keys) | " + " | ".join(cells) + " |")
        cells = []
        for r in rs:
            a = [d for k, d in r["fields"].items() if k.startswith("attr_id:")]
            cells.append(f"{sum(d['right'] for d in a)}/{sum(d['right'] + d['wrong'] for d in a)}")
        print("| attributes, ontology id | " + " | ".join(cells) + " |")
        for k in ("contrasts", "replicates", "series", "co_measurement_groups", "background_control", "donor_pairs"):
            cells = []
            for r in rs:
                v = r["relations"][k]
                cells.append(f"P {v['precision']} R {v['recall']} ({v['match']}/{v['model']} of {v['gold']})")
            print(f"| {k} | " + " | ".join(cells) + " |")


if __name__ == "__main__" and "--table" in sys.argv:
    table()
