# PROTOTYPE, throwaway: does the glossary's donor quote move Jev's answer? Re-ask one question with only the
# donor part of the per-sample glossary changed (quotes vs ids only), everything else as in that version's run.
import json

from ask import ASSAY_Q, CONTROL_TYPE_Q, ROLE_Q, glossary, level_q, record  # noqa: F401
from common import jev, load, run_dir


def state_for(gse, lists_file, gsm, donor_quotes):
    study = load(gse, "study.json")
    ds = json.loads((run_dir(gse) / lists_file).read_text())["deepseek"]
    g = glossary(study, ds)
    if donor_quotes:
        g["donors"] = {str(d["id"]): d.get("quote") for d in ds["donors"]}
    else:
        g["donors"] = [str(d["id"]) for d in ds["donors"]]
    s = next(x for x in study["samples"] if x["gsm"] == gsm)
    return {"sample": record(s), "study_glossary": g}, ds


out = {}
# 1. v1's role error: GSM8514843 (title "155688 WT 12M H3K27me3"); the v1 quote for donor 155688 was "155688 WT 12M TI"
for dq in (True, False):
    st, ds = state_for("GSE190102", "lists.v1.json", "GSM8514843", dq)
    a, m = jev("GSE190102", st, {"role": ROLE_Q}, tag="probe_glossary")
    out[f"v1 lists, GSM8514843 role, donor quotes={dq}"] = a["role"]["probabilities"]
# 2. v2's donor error: GSM5825066 (title "17 WT 6M H3K27ac"), asked with and without donor quotes
for dq in (True, False):
    st, ds = state_for("GSE190102", "lists.json", "GSM5825066", dq)
    donors = [str(d["id"]) for d in ds["donors"]]
    a, m = jev("GSE190102", st, {"donor": level_q("donor (individual, animal or cell line)", donors)}, tag="probe_glossary")
    p = a["donor"]["probabilities"]
    out[f"v2 lists, GSM5825066 donor, donor quotes={dq}"] = {k: v for k, v in sorted(p.items(), key=lambda x: -x[1])[:3]}
print(json.dumps(out, indent=1))
json.dump(out, open(run_dir("GSE190102") / "probe_glossary.json", "w"), indent=1)
