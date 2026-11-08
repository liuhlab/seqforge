# PROTOTYPE, throwaway: turn the open lists into closed questions and ask Jev.
# A fixed sequence: one study-level request (each candidate key's role, each key's baseline asked
# speculatively, ontology picks), then one request per sample (its level for every key, assay, role,
# control type). Biosample: code blocks samples that agree on every key; Jev answers yes/no only
# inside a block.
import itertools
import json
import sys
import urllib.parse
import urllib.request

from common import jev, jev_many, load, run_dir, save
from draft import study_text

ASSAYS = ["RNA-seq", "ChIP-seq", "ATAC-seq", "CUT&RUN", "CUT&Tag", "scRNA-seq", "CRISPR screen",
          "CLIP", "Ribo-seq", "other"]
ONTOLOGY_FOR = {"cell type": "cl", "tissue": "uberon", "cell line": "clo"}


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


def ols_candidates(gse, text, onto):
    f = run_dir(gse) / "raw" / f"ols_{onto}_{abs(hash(text)) % 10**8}.json"
    if not f.exists():
        q = urllib.parse.urlencode({"q": text, "ontology": onto, "rows": 8, "type": "class"})
        with urllib.request.urlopen(f"https://www.ebi.ac.uk/ols4/api/search?{q}", timeout=60) as r:
            f.write_bytes(r.read())
    docs = json.loads(f.read_text())["response"]["docs"]
    out = {}
    for d in docs:
        if d.get("obo_id") and d["obo_id"] not in out:
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


def study_questions(study, keys, gse):
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
    for k, v in sorted({(k, v) for s in study["samples"] for k, v in s["characteristics"] if k in ONTOLOGY_FOR}):
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
    return qs, onto


def ask(gse):
    study = load(gse, "study.json")
    lists = load(gse, "lists.json")
    keys = keys_from_lists(lists)
    knames = list(keys)
    sq, onto = study_questions(study, keys, gse)
    sa, smeta = jev(gse, study_text(study), sq, tag="study")
    jobs = []
    for s in study["samples"]:
        qs = {f"level_{i}": level_q(k, keys[k]) for i, k in enumerate(knames)}
        qs.update({"assay": ASSAY_Q, "role": ROLE_Q, "control_type": CONTROL_TYPE_Q})
        jobs.append(({"sample": record(s), "lists": keys}, qs))
    res = jev_many(gse, jobs, tag="sample")
    per_sample = {s["gsm"]: a for s, (a, m) in zip(study["samples"], res)}
    # biosample: code blocks samples that agree on every key; Jev asked only within a block.
    sig = {}
    for gsm, a in per_sample.items():
        sig.setdefault(tuple(a[f"level_{i}"]["choice"] for i in range(len(knames))), []).append(gsm)
    pairs = [p for g in sig.values() if len(g) > 1 for p in itertools.combinations(g, 2)]
    bios = {}
    if pairs:
        by = {s["gsm"]: s for s in study["samples"]}
        pj = [({"sample_a": record(by[a]), "sample_b": record(by[b])},
               {"same": {"type": "noul", "instructions": "Are `sample_a` and `sample_b` two libraries made from the "
                         "same biological material (one prep), rather than separate biological replicates?"}})
              for a, b in pairs]
        for (a, b), (ans, m) in zip(pairs, jev_many(gse, pj, tag="biosample_pair")):
            bios[f"{a}|{b}"] = ans["same"]["noul"]
    out = {"keys": keys, "study": sa, "onto": onto, "samples": per_sample, "biosample_pairs": bios,
           "n_requests": 1 + len(jobs) + len(pairs)}
    save(gse, "answers.json" if lists["used"] == "code" or not lists["code"]["candidate_keys"] else "answers.deepseek-lists.json", out)
    return out


if __name__ == "__main__":
    r = ask(sys.argv[1])
    print("requests", r["n_requests"])
    for i, k in enumerate(r["keys"]):
        print(k, r["study"][f"role_{i}"]["choice"], round(max(r["study"][f"role_{i}"]["probabilities"].values()), 3),
              "baseline", r["study"][f"baseline_{i}"]["choice"], r["study"][f"baseline_{i}"]["probabilities"])
    for q in r["onto"]:
        print(q, r["onto"][q]["text"], r["study"][q]["choice"], r["study"][q]["probabilities"])
