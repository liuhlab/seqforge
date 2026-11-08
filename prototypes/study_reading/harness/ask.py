# PROTOTYPE, throwaway: turn the open lists into closed questions and ask Jev.
# A fixed sequence: one study-level request (each candidate key's role, each key's baseline asked
# speculatively, ontology picks), then one request per sample (its level for every key, assay, role,
# control type). Biosample: code blocks samples that agree on every key; Jev answers yes/no only
# inside a block.
import itertools
import json
import os
import sys
import urllib.parse
import urllib.request

from common import jev, jev_many, load, run_dir, save
from draft import study_text

ASSAYS = ["RNA-seq", "ChIP-seq", "ATAC-seq", "CUT&RUN", "CUT&Tag", "scRNA-seq", "CRISPR screen",
          "CLIP", "Ribo-seq", "other"]
ONTOLOGY_FOR = {"cell type": "cl", "tissue": "uberon", "cell line": "clo", "disease state": "mondo", "disease": "mondo"}
# v2 (written after reading the gold): BioSample-style sample type, asked once per study
SAMPLE_TYPES = ["tissue", "cell line", "primary cells", "in vitro differentiated cells", "organoid", "whole organism",
                "cell culture"]


def keys_from_lists(lists):
    """Candidate keys with their levels, from whichever draft is used."""
    if lists["used"] == "code":
        return {k["name"]: [lv["value"] for lv in k["levels"]] for k in lists["code"]["candidate_keys"]}
    ds = lists["deepseek"]
    keys = {f["name"]: [lv["value"] for lv in f["levels"]] for f in ds.get("factors", [])}
    if ds.get("donors"):
        keys["donor"] = [d["id"] for d in ds["donors"]]
    return keys


def record(s, with_chars=True):
    r = {"gsm": s["gsm"], "title": s["title"], "source": s["source"], "organism": s["organism"],
         "library_strategy": s["library_strategy"], "molecule": s["molecule"],
         "treatment_protocol": s["protocols"]["treatment"]}
    if with_chars:
        r["characteristics"] = {k or "note": v for k, v in s["characteristics"]}
    return r


def _ols(gse, text, onto):
    import hashlib
    f = run_dir(gse) / "raw" / f"ols_{onto}_{hashlib.sha1(text.encode()).hexdigest()[:10]}.json"
    if not f.exists():
        q = urllib.parse.urlencode({"q": text, "ontology": onto, "rows": 8, "type": "class"})
        with urllib.request.urlopen(f"https://www.ebi.ac.uk/ols4/api/search?{q}", timeout=60) as r:
            f.write_bytes(r.read())
    return json.loads(f.read_text())["response"]["docs"]


def ols_candidates(gse, text, onto):
    """v2: query the text and each word's singular form ('neurons' -> 'neuron'), merge, keep order."""
    qs = [text]
    sing = " ".join(w[:-1] if w.endswith("s") and len(w) > 3 else w for w in text.split())
    if sing != text:
        qs.append(sing)
    last = text.split()[-1] if text.split() else text
    if last != text:
        qs.append(last[:-1] if last.endswith("s") and len(last) > 3 else last)
    out = {}
    for q in qs:
        for d in _ols(gse, q, onto):
            if d.get("obo_id") and d["obo_id"] not in out and len(out) < 12:
                out[d["obo_id"]] = d.get("label")
    return out


def level_q(key, levels, ref="`sample`"):
    crit = {v: f"{ref} has {key} = {v}" for v in levels}
    crit["not_stated"] = f"{ref} does not say which {key} it has"
    return {"type": "choice", "instructions": f"Which {key} does {ref} have?", "criteria": crit}


ROLE_Q = {
    "type": "choice",
    "instructions": "Is `sample` a signal library or a background control library?",
    "criteria": {
        "signal": "It measures the biology being studied. Untreated, mock, wild-type, vehicle or "
                  "control-shRNA samples are biological baselines and count as signal.",
        "control": "It exists only as a technical background reference for another library, such as "
                   "a ChIP input, an IgG pull-down, or a size-matched input in CLIP.",
    },
}
CONTROL_TYPE_Q = {
    "type": "choice",
    "instructions": "If `sample` is a background control library, what kind of control is it?",
    "criteria": {"input": "chromatin or RNA input taken before enrichment", "IgG": "non-specific antibody pull-down",
                 "non-targeting": "non-targeting guide or scrambled construct", "size-matched input": "CLIP size-matched input",
                 "other": "another kind of control"},
}
ASSAY_Q = {"type": "choice", "instructions": "Which assay produced the library in `sample`?",
           "criteria": {a: None for a in ASSAYS}}


def study_questions(study, keys, gse, wattrs=None, donors=None):
    qs = {}
    for i, (k, levels) in enumerate(keys.items()):
        qs[f"role_{i}"] = {
            "type": "choice",
            "instructions": f"In this study, what is the sample characteristic \"{k}\" (values: {', '.join(levels)})?",
            "criteria": {
                "designed_factor": "a condition the investigators varied on purpose to compare, such as genotype, "
                                   "knockdown, treatment, infection, time point or dose",
                "donor": "it names the person, animal, strain or cell line the material came from, such as a donor "
                         "or patient ID; it is not what the study compares",
                "other": "a descriptive attribute, batch or replicate label that is neither compared nor a donor",
            },
        }
        qs[f"baseline_{i}"] = {
            "type": "choice",
            "instructions": f"If \"{k}\" is a condition the study compares, which value is the reference level the "
                            f"others are compared against (untreated, wild type, control, mock, vehicle or time zero)?",
            "criteria": {v: None for v in levels},
        }
    onto = {}
    pairs = {(k, v) for s in study["samples"] for k, v in s["characteristics"] if k in ONTOLOGY_FOR}
    pairs |= {(k, v) for k, vs in (wattrs or {}).items() if k in ONTOLOGY_FOR for v in vs}
    for k, v in sorted(pairs):
        if k in keys:
            continue
        cands = ols_candidates(gse, v, ONTOLOGY_FOR[k])
        if cands:
            qn = f"onto_{len(onto)}"
            onto[qn] = {"attr": k, "text": v, "candidates": cands}
            crit = {cid: lab for cid, lab in cands.items()}
            crit["none"] = "none of these terms names it"
            qs[qn] = {"type": "choice", "instructions": f"Which ontology term names the {k} \"{v}\" in this study?",
                      "criteria": crit}
    if wattrs is not None:
        qs["sample_type"] = {"type": "choice", "instructions": "What kind of biological material were the libraries "
                             "in this study made from?", "criteria": {x: None for x in SAMPLE_TYPES}}
        if not donors:
            qs["one_individual_per_sample"] = {
                "type": "noul", "instructions": "Does each sample in this study come from a different individual "
                "animal or person (one biological replicate per sample), rather than several samples sharing one "
                "individual or one cell line?"}
    return qs, onto


TARGETED = {"ChIP-seq", "CUT&RUN", "CUT&Tag"}


def reask(gse, study, jobs, per_sample, below=0.9):
    """v3, the rule the dev study proposed before any gold existed: every Choice picked below p 0.9 is asked
    again with its options reversed, same state; the two probability vectors are averaged. A label whose two
    picks differ is listed as unsure."""
    redo = []
    for s, (state, qs) in zip(study["samples"], jobs):
        a = per_sample[s["gsm"]]
        for qid, q in qs.items():
            if q["type"] == "choice" and a[qid]["probabilities"][a[qid]["choice"]] < below:
                rq = dict(q, criteria=dict(reversed(list(q["criteria"].items()))))
                redo.append((s["gsm"], qid, state, rq))
    res = jev_many(gse, [(st, {qid: rq}) for g, qid, st, rq in redo], tag="reask")
    for (g, qid, st, rq), (ans, m) in zip(redo, res):
        a1, a2 = per_sample[g][qid], ans[qid]
        avg = {k: round((a1["probabilities"][k] + a2["probabilities"].get(k, 0)) / 2, 4) for k in a1["probabilities"]}
        pick = max(avg, key=avg.get)
        per_sample[g][qid] = {"type": "choice", "choice": pick, "probabilities": avg,
                              "reask": {"first": a1["choice"], "second": a2["choice"], "p_first": a1["probabilities"],
                                        "p_second": a2["probabilities"]}}
    return len(redo)


def lists_v1(lists):
    """The writer's open lists, reduced to what the closed questions need."""
    ds = lists["deepseek"]
    keys = {f["name"]: [lv["value"] for lv in f["levels"]] for f in ds.get("factors", [])}
    donors = list(dict.fromkeys(str(d["id"]) for d in ds.get("donors", [])))
    targets = list(dict.fromkeys(t["name"] for t in ds.get("targets", [])))
    attrs = {}
    for x in ds.get("attributes", []):
        if x.get("name") and x.get("value") is not None:
            attrs.setdefault(x["name"], []).append(str(x["value"]))
    attrs = {k: list(dict.fromkeys(v)) for k, v in attrs.items() if k not in keys}
    return keys, donors, targets, attrs


def glossary(study, ds):
    """Study glossary for every per-sample state: each listed value with the writer's quote that defines it."""
    g = {"design": [x for t in study["series_text"].values() for x in t["overall_design"] + t["summary"][:1]
                    if "SubSeries listed below" not in x and "Refer to individual" not in x]}
    g["factor_levels"] = {f["name"]: {lv["value"]: lv.get("quote") for lv in f["levels"]} for f in ds.get("factors", [])}
    if ds.get("donors"):
        g["donors"] = [str(d["id"]) for d in ds["donors"]]  # v2: ids only
    if ds.get("targets"):
        g["targets"] = {t["name"]: t.get("quote") for t in ds["targets"]}
    if ds.get("controls"):
        g["controls"] = {c["kind"]: c.get("quote") for c in ds["controls"]}
    return g


def protocols(study, limit=1500):
    out = {}
    for s in study["samples"]:
        for k, v in s["protocols"].items():
            if v:
                out.setdefault(k, [])
                if v[:limit] not in out[k]:
                    out[k].append(v[:limit])
    return out


def ask(gse):
    study = load(gse, "study.json")
    lists = load(gse, "lists.json")
    ds = lists["deepseek"]
    keys, donors, targets, attrs = lists_v1(lists)
    knames = list(keys)
    gl = glossary(study, ds)
    sq, onto = study_questions(study, keys, gse, attrs, donors)
    sa, smeta = jev(gse, study_text(study), sq, tag="study")
    multi = {k: v for k, v in attrs.items() if len(v) > 1}
    jobs = []
    for s in study["samples"]:
        qs = {f"level_{i}": level_q(k, keys[k]) for i, k in enumerate(knames)}
        qs.update({"assay": ASSAY_Q, "role": ROLE_Q, "control_type": CONTROL_TYPE_Q})
        if donors:
            qs["donor"] = level_q("donor (individual, animal or cell line)", donors)
        if targets:
            crit = {t: f"`sample` is a pull-down with an antibody against {t}" for t in targets}
            crit["none"] = "`sample` has no antibody target: an input or IgG control, or an assay without an antibody"
            qs["target"] = {"type": "choice", "instructions": "Which antibody target does the library in `sample` have?",
                            "criteria": crit}
        for j, (k, vals) in enumerate(multi.items()):
            qs[f"attr_{j}"] = level_q(k, vals)
        jobs.append(({"sample": record(s), "study_glossary": gl}, qs))
    res = jev_many(gse, jobs, tag="sample")
    per_sample = {s["gsm"]: a for s, (a, m) in zip(study["samples"], res)}
    if os.environ.get("REASK"):
        reask(gse, study, jobs, per_sample)
    # biosample: code blocks assay-blind (same donor if listed, same factor levels), plus the writer's own groups;
    # Jev answers yes/no only for those pairs.
    def block(gsm):
        a = per_sample[gsm]
        return (a.get("donor", {}).get("choice"),) + tuple(a[f"level_{i}"]["choice"] for i in range(len(knames)))
    sig = {}
    for gsm in per_sample:
        b = block(gsm)
        if donors and b[0] in (None, "not_stated"):
            continue  # unknown donor: no block
        sig.setdefault(b, []).append(gsm)
    pairs = {p for g in sig.values() if len(g) > 1 for p in itertools.combinations(sorted(g), 2)}
    for grp in ds.get("biosample_sharing", []):
        gs = sorted(x for x in grp.get("gsms", []) if x in per_sample)
        pairs |= set(itertools.combinations(gs, 2))
    pairs = sorted(pairs)
    bios = {}
    if pairs:
        by = {s["gsm"]: s for s in study["samples"]}
        prot = protocols(study)

        def rec(g):
            r = record(by[g])
            if donors:
                r["donor_label"] = per_sample[g]["donor"]["choice"]
            return r
        pj = [({"sample_a": rec(a), "sample_b": rec(b), "study_glossary": gl, "protocols": prot},
               {"same": {"type": "noul", "instructions":
                         "Were `sample_a` and `sample_b` made from the same biological material: one tissue, nuclei or "
                         "cell preparation from one individual, split into several libraries? Answer no if they came "
                         "from separate preparations (separate animals, separate differentiations or cultures, "
                         "biological replicates), or if no text says they share a preparation."}})
              for a, b in pairs]
        for (a, b), (ans, m) in zip(pairs, jev_many(gse, pj, tag="biosample_pair")):
            bios[f"{a}|{b}"] = ans["same"]["noul"]
    out = {"keys": keys, "donors": donors, "targets": targets, "attrs": attrs, "multi_attrs": list(multi),
           "glossary": gl, "study": sa, "onto": onto, "samples": per_sample, "biosample_pairs": bios,
           "n_requests": 1 + len(jobs) + len(pairs)}
    save(gse, "answers.json", out)
    return out


if __name__ == "__main__":
    r = ask(sys.argv[1])
    print("requests", r["n_requests"])
    for i, k in enumerate(r["keys"]):  # noqa
        print(k, r["study"][f"role_{i}"]["choice"], round(max(r["study"][f"role_{i}"]["probabilities"].values()), 3),
              "baseline", r["study"][f"baseline_{i}"]["choice"], r["study"][f"baseline_{i}"]["probabilities"])
    for q in r["onto"]:
        print(q, r["onto"][q]["text"], r["study"][q]["choice"], r["study"][q]["probabilities"])
