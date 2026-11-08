# PROTOTYPE, throwaway: assemble Jev's labels into the design table and derive the five relations in
# code; write runs/<GSE>/reading.yaml in the SCHEMA.md shape (plus the gold README's additions: title,
# series per sample, relations scoped by assay/target/role, background_control basis).
import itertools
import json
import re
import sys

import yaml

from ask import TARGETED, lists_v1
from common import JEV_MODEL, load, run_dir

UNIT = {"min": 1 / 60, "h": 1, "hr": 1, "hrs": 1, "hour": 1, "hours": 1, "d": 24, "day": 24,
        "days": 24, "wk": 168, "week": 168, "weeks": 168, "mo": 730, "month": 730, "months": 730,
        "y": 8760, "yr": 8760, "year": 8760, "years": 8760}
NUM = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*-?\s*([a-zA-Z]*)\s*(?:old)?\s*$")


def parse_num(v):
    """Code parses numbers; Jev never does. '48h' -> 48.0 (hours), '3 months' -> 2190, else None."""
    m = NUM.match(v or "")
    if not m:
        return None
    x, u = float(m.group(1)), m.group(2).lower()
    if u and u not in UNIT:
        return None
    return x * UNIT.get(u, 1)


SEX = {"male": "PATO:0000384", "m": "PATO:0000384", "female": "PATO:0000383", "f": "PATO:0000383"}


def pmax(ans):
    return round(ans["probabilities"][ans["choice"]], 3)


def evidence(s, key, val):
    if val is None:
        return {"quote": None, "source": "unverified"}
    for k, v in s["characteristics"]:
        if v == val or (val.lower() in v.lower() and len(val) > 2):
            return {"quote": f"{k}: {v}" if k else v, "source": "GSM characteristics"}
    if val.lower() in (s["title"] or "").lower():
        return {"quote": s["title"], "source": "GSM title"}
    return {"quote": None, "source": "unverified"}


def derive(gse, out_name="reading.yaml"):
    L = load(gse, "lists.json")
    ds = L["deepseek"]
    study, A = load(gse, "study.json"), load(gse, "answers.json")
    keys, names = A["keys"], list(A["keys"])
    roles = {k: A["study"][f"role_{i}"]["choice"] for i, k in enumerate(names)}
    factors = [k for k in names if roles[k] == "designed_factor"]
    fdefs = []
    for k in factors:
        i = names.index(k)
        nums = [parse_num(v) for v in keys[k]]
        fdefs.append({"name": k, "levels": keys[k], "baseline": A["study"][f"baseline_{i}"]["choice"],
                      "ordered": all(n is not None for n in nums),
                      "probability": {"role": pmax(A["study"][f"role_{i}"]),
                                      "baseline": pmax(A["study"][f"baseline_{i}"])}})
    _, donors_l, targets_l, attrs_l = lists_v1(L)
    multi = A["multi_attrs"]
    parent = {s["gsm"]: s["gsm"] for s in study["samples"]}

    def find(x):
        while parent[x] != x:
            x = parent[x]
        return x

    for pair, p in A["biosample_pairs"].items():
        if p > 0.5:
            a, b = pair.split("|")
            parent[find(a)] = find(b)
    bio_ids, donors, rows = {}, {}, []
    for s in study["samples"]:
        g, a = s["gsm"], A["samples"][s["gsm"]]
        chars = {k: v for k, v in s["characteristics"]}
        fv, ev, prob = {}, {}, {}
        for k in factors:
            ans = a[f"level_{names.index(k)}"]
            fv[k] = None if ans["choice"] == "not_stated" else ans["choice"]
            prob[k] = pmax(ans)
            ev[k] = evidence(s, k, fv[k])
        donor = None
        if "donor" in a:
            prob["donor"] = pmax(a["donor"])  # also when the pick is not_stated
        if "donor" in a and a["donor"]["choice"] != "not_stated":
            donor = donors.setdefault(a["donor"]["choice"], f"D{len(donors) + 1:02d}")
            ev["donor"] = evidence(s, "donor", a["donor"]["choice"])
            ev["donor"]["label"] = a["donor"]["choice"]
        bio = bio_ids.setdefault(find(g), f"B{len(bio_ids) + 1:02d}")
        role = a["role"]["choice"]
        assay = a["assay"]["choice"]
        prob.update({"assay": pmax(a["assay"]), "role": pmax(a["role"])})
        target = None
        if "target" in a:
            prob["target"] = pmax(a["target"])
            if assay in TARGETED and role == "signal" and a["target"]["choice"] != "none":
                target = a["target"]["choice"]
        ctype = None
        if role == "control":
            ctype = a["control_type"]["choice"]
            prob["control_type"] = pmax(a["control_type"])
        attrs = {"species": {"text": s["organism"], "id": f"NCBITaxon:{s['taxid']}" if s["taxid"] else None}}
        if "sample_type" in A["study"]:
            attrs["sample_type"] = {"text": A["study"]["sample_type"]["choice"], "source": "Jev study question"}
            prob["attr:sample_type"] = pmax(A["study"]["sample_type"])
        for k, v in chars.items():
            if k and k not in keys and k not in attrs:
                attrs[k] = {"text": v, "source": "GSM characteristics"}
        for k, vals in attrs_l.items():  # the writer's attributes: constant ones as written, varying ones asked
            if k.lower() in {x.lower() for x in attrs}:
                continue
            if k in multi:
                ans = a[f"attr_{multi.index(k)}"]
                if ans["choice"] != "not_stated":
                    attrs[k] = {"text": ans["choice"], "source": "writer list, Jev pick"}
                    prob[f"attr:{k}"] = pmax(ans)
            else:
                attrs[k] = {"text": vals[0], "source": "writer list"}
        for k, v in attrs.items():  # ontology ids, wherever the attribute came from
            for q, o in A["onto"].items():
                if o["attr"] == k and o["text"] == v["text"]:
                    pick = A["study"][q]["choice"]
                    v["id"] = None if pick == "none" else pick
                    prob[f"attr_id:{k}"] = pmax(A["study"][q])
            if k.lower() == "sex" and v["text"].strip().lower() in SEX:
                v["id"] = SEX[v["text"].strip().lower()]
        if donor is None and A["study"].get("one_individual_per_sample", {}).get("noul", 0) > 0.5:
            donor = f"D{len(rows) + 1:02d}"  # each sample its own individual (Jev study question)
            prob["donor_study"] = round(A["study"]["one_individual_per_sample"]["noul"], 3)
        unsure = [{"label": q.replace("level_", "factor ").replace("attr_", "attribute "),
                   "note": f"Jev picked {v['reask']['first']} then {v['reask']['second']} with options reversed"}
                  for q, v in a.items() if isinstance(v, dict) and v.get("reask")
                  and v["reask"]["first"] != v["reask"]["second"]]
        rows.append({
            "gsm": g, "title": s["title"], "unsure": unsure, "series": [x for x in s["series"] if x in study["series"]],
            "factors": fv, "biosample": bio, "donor": donor, "assay": assay, "target": target, "role": role,
            "control_type": ctype, "attributes": attrs, "evidence": ev, "probability": prob,
        })
    rel = relations(rows, fdefs)
    reading = {
        "study": {"series": study["series"], "superseries": study["superseries"], "bioproject": study["bioproject"],
                  "pmid": study["pmid"],
                  "notes": f"model reading: lists by DeepSeek ({ds.get('_meta', {}).get('model')}), labels by {JEV_MODEL}; "
                           f"donors listed {len(donors_l)}, targets listed {len(targets_l)}"},
        "factors": fdefs, "samples": rows, "relations": rel,
    }
    (run_dir(gse) / out_name).write_text(
        yaml.safe_dump(json.loads(json.dumps(reading)), sort_keys=False, allow_unicode=True, width=120))
    return reading


def relations(rows, fdefs):
    F = [f["name"] for f in fdefs]
    base = {f["name"]: f["baseline"] for f in fdefs}
    scopes = {(r["assay"], r["target"], r["role"]) for r in rows}
    groups = {}
    for r in rows:
        groups.setdefault(((r["assay"], r["target"], r["role"]), tuple(r["factors"].get(k) for k in F)), []).append(r["gsm"])

    def tag(sc):
        return {"assay": sc[0], "target": sc[1]} if len(scopes) > 1 else {}

    contrasts = []
    for ((sa, ka), ga), ((sb, kb), gb) in itertools.permutations(groups.items(), 2):
        if sa != sb or sa[2] != "signal":
            continue
        diff = [i for i in range(len(F)) if ka[i] != kb[i]]
        if len(diff) == 1:
            i = diff[0]
            if kb[i] == base[F[i]] and ka[i] != base[F[i]] and ka[i] is not None:
                contrasts.append({"factor": F[i], "test": sorted(ga), "baseline": sorted(gb),
                                  "fixed": {F[j]: ka[j] for j in range(len(F)) if j != i}, **tag(sa)})
    series = []
    for f in fdefs:
        if not f["ordered"]:
            continue
        i = F.index(f["name"])
        by_rest = {}
        for (sc, key), g in groups.items():
            if key[i] is None:
                continue
            by_rest.setdefault((sc, key[:i] + key[i + 1:]), []).append((key[i], g))
        for (sc, rest), lv in by_rest.items():
            if len({x[0] for x in lv}) >= 2:
                series.append({"factor": f["name"], "members": sorted(m for _, g in lv for m in g), **tag(sc),
                               **({"role": sc[2]} if sc[2] != "signal" else {})})
    bio = {}
    for r in rows:
        bio.setdefault(r["biosample"], []).append(r["gsm"])
    bg = []
    for r in rows:
        if r["role"] != "signal":
            continue
        ctl = [c for c in rows if c["role"] == "control" and c["biosample"] == r["biosample"]]
        basis = "same biosample"
        if not ctl:
            ctl = [c for c in rows if c["role"] == "control" and c["factors"] == r["factors"] and c["assay"] == r["assay"]]
            basis = "same factor values"
        bg += [{"signal": r["gsm"], "control": c["gsm"], "basis": basis} for c in ctl]
    return {"contrasts": contrasts, "replicates": sorted(sorted(g) for g in groups.values() if len(g) > 1),
            "series": series, "co_measurement": sorted(sorted(g) for g in bio.values() if len(g) > 1),
            "background_control": bg}


if __name__ == "__main__":
    r = derive(sys.argv[1])
    print(len(r["samples"]), "samples;", {k: len(v) for k, v in r["relations"].items()})
