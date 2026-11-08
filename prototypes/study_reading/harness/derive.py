# PROTOTYPE, throwaway: assemble Jev's labels into the design table and derive the five relations in
# code; write runs/<GSE>/reading.yaml in the SCHEMA.md shape.
import itertools
import json
import re
import sys

import yaml

from common import JEV_MODEL, load, run_dir

UNIT = {"min": 1 / 60, "m": 1 / 60, "h": 1, "hr": 1, "hrs": 1, "hour": 1, "hours": 1, "d": 24, "day": 24,
        "days": 24, "wk": 168, "week": 168, "weeks": 168}
NUM = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")


def parse_num(v):
    """Code parses numbers; Jev never does. '48h' -> 48.0 (hours), '10' -> 10.0, else None."""
    m = NUM.match(v or "")
    if not m:
        return None
    x, u = float(m.group(1)), m.group(2).lower()
    if u and u not in UNIT:
        return None
    return x * UNIT.get(u, 1)


def pmax(ans):
    return round(ans["probabilities"][ans["choice"]], 3)


def derive(gse):
    L = load(gse, "lists.json")
    alt = L["used"] == "deepseek" and bool(L["code"]["candidate_keys"])
    study, A = load(gse, "study.json"), load(gse, "answers.deepseek-lists.json" if alt else "answers.json")
    keys, names = A["keys"], list(A["keys"])
    roles = {k: A["study"][f"role_{i}"]["choice"] for i, k in enumerate(names)}
    factors = [k for k in names if roles[k] == "designed_factor"]
    donor_key = next((k for k in names if roles[k] == "donor"), None)
    if donor_key is None and "donor" in names:  # DeepSeek lists name the donor list "donor"
        donor_key = "donor"
    fdefs = []
    for k in factors:
        i = names.index(k)
        nums = [parse_num(v) for v in keys[k]]
        fdefs.append({"name": k, "levels": keys[k], "baseline": A["study"][f"baseline_{i}"]["choice"],
                      "ordered": all(n is not None for n in nums)})
    donors, rows = {}, []
    # biosample: union pairs Jev called the same prep
    parent = {s["gsm"]: s["gsm"] for s in study["samples"]}

    def find(x):
        while parent[x] != x:
            x = parent[x]
        return x

    for pair, p in A["biosample_pairs"].items():
        if p > 0.5:
            a, b = pair.split("|")
            parent[find(a)] = find(b)
    bio_ids = {}
    for s in study["samples"]:
        g, a = s["gsm"], A["samples"][s["gsm"]]
        chars = {k: v for k, v in s["characteristics"]}
        fv, ev, prob = {}, {}, {}
        for k in factors:
            ans = a[f"level_{names.index(k)}"]
            fv[k] = None if ans["choice"] == "not_stated" else ans["choice"]
            prob[k] = pmax(ans)
            if chars.get(k) == fv[k]:
                ev[k] = {"quote": f"{k}: {fv[k]}", "source": "GSM characteristics"}
            elif fv[k] and fv[k].lower() in (s["title"] or "").lower():
                ev[k] = {"quote": fv[k], "source": "GSM title"}
            else:
                ev[k] = {"quote": None, "source": "unverified"}
        donor = None
        if donor_key:
            ans = a[f"level_{names.index(donor_key)}"]
            if ans["choice"] != "not_stated":
                donor = donors.setdefault(ans["choice"], f"D{len(donors) + 1:02d}")
                prob["donor"] = pmax(ans)
        root = find(g)
        bio = bio_ids.setdefault(root, f"B{len(bio_ids) + 1:02d}")
        role = a["role"]["choice"]
        prob.update({"assay": pmax(a["assay"]), "role": pmax(a["role"])})
        attrs = {"species": {"text": s["organism"], "id": f"NCBITaxon:{s['taxid']}" if s["taxid"] else None}}
        for q, o in A["onto"].items():
            if chars.get(o["attr"]) == o["text"]:
                pick = A["study"][q]["choice"]
                attrs[o["attr"]] = {"text": o["text"], "id": None if pick == "none" else pick}
                prob[f"attr:{o['attr']}"] = pmax(A["study"][q])
        for k, v in chars.items():
            if k not in keys and k not in attrs and k:
                attrs[k] = {"text": v}
        rows.append({
            "gsm": g, "factors": fv, "biosample": bio, "donor": donor, "assay": a["assay"]["choice"],
            "target": None, "role": role,
            "control_type": a["control_type"]["choice"] if role == "control" else None,
            "attributes": attrs, "evidence": ev, "probability": prob,
        })
    rel = relations(rows, fdefs)
    reading = {
        "study": {"series": study["series"], "superseries": study["superseries"], "bioproject": study["bioproject"],
                  "pmid": study["pmid"],
                  "notes": f"model reading: lists drafted by {load(gse, 'lists.json')['used']}, labels by {JEV_MODEL}; "
                           f"donor key: {donor_key}"},
        "factors": fdefs, "samples": rows, "relations": rel,
    }
    name = "reading.yaml" if load(gse, "lists.json")["used"] == "code" or not load(gse, "lists.json")["code"]["candidate_keys"] \
        else "reading.deepseek-lists.yaml"
    (run_dir(gse) / name).write_text(yaml.safe_dump(json.loads(json.dumps(reading)), sort_keys=False, allow_unicode=True, width=120))
    return reading


def relations(rows, fdefs):
    F = [f["name"] for f in fdefs]
    base = {f["name"]: f["baseline"] for f in fdefs}
    groups = {}
    for r in rows:
        groups.setdefault(tuple(r["factors"].get(k) for k in F), []).append(r["gsm"])
    contrasts = []
    for (ka, ga), (kb, gb) in itertools.permutations(groups.items(), 2):
        diff = [i for i in range(len(F)) if ka[i] != kb[i]]
        if len(diff) == 1:
            i = diff[0]
            if kb[i] == base[F[i]] and ka[i] != base[F[i]]:
                contrasts.append({"factor": F[i], "test": ga, "baseline": gb,
                                  "fixed": {F[j]: ka[j] for j in range(len(F)) if j != i}})
    series = []
    for f in fdefs:
        if f["ordered"]:
            i = F.index(f["name"])
            by_rest = {}
            for key, g in groups.items():
                by_rest.setdefault(key[:i] + key[i + 1:], set()).add(key[i])
            for rest, lv in by_rest.items():
                if len(lv) >= 2:
                    mem = [x for key, g in groups.items() if key[:i] + key[i + 1:] == rest for x in g]
                    series.append({"factor": f["name"], "members": sorted(mem)})
    bio = {}
    for r in rows:
        bio.setdefault(r["biosample"], []).append(r["gsm"])
    bg = []
    for r in rows:
        if r["role"] != "signal":
            continue
        ctl = [c for c in rows if c["role"] == "control" and c["biosample"] == r["biosample"]] or \
              [c for c in rows if c["role"] == "control" and c["factors"] == r["factors"]]
        bg += [{"signal": r["gsm"], "control": c["gsm"]} for c in ctl]
    return {"contrasts": contrasts, "replicates": [sorted(g) for g in groups.values() if len(g) > 1],
            "series": series, "co_measurement": [sorted(g) for g in bio.values() if len(g) > 1],
            "background_control": bg}


if __name__ == "__main__":
    r = derive(sys.argv[1])
    print(len(r["samples"]), "samples;", {k: len(v) for k, v in r["relations"].items()})
