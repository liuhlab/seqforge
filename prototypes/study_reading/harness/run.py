# PROTOTYPE, throwaway: read one GEO study end to end.
#   pixi run python run.py GSE164798 [--label v1]
# fetch -> DeepSeek open lists -> quote check -> Jev closed questions -> derive -> reading.yaml + cost.json
# (the code-first list path of the dev study is kept as draft(gse, "auto"); its readings are reading.v0-*.yaml)
import json
import sys
import time

import ask
import common
import derive
import draft
import fetch
from common import run_dir

gse = sys.argv[1]
label = sys.argv[sys.argv.index("--label") + 1] if "--label" in sys.argv else "v1"
common.RUN_LABEL = label
wall = {}
t = time.time(); s = fetch.fetch(gse); wall["fetch"] = round(time.time() - t, 2)
t = time.time(); l = draft.draft(gse, "only"); wall["deepseek_lists"] = round(time.time() - t, 2)
t = time.time(); a = ask.ask(gse); wall["jev_questions"] = round(time.time() - t, 2)
t = time.time(); r = derive.derive(gse); wall["derive"] = round(time.time() - t, 3)


def tot(p):
    xs = [x for x in common.LEDGER if x["provider"] == p]
    return {"calls": len(xs), "live_calls": sum(not x["cached"] for x in xs),
            "questions": sum(x.get("n_questions", 0) for x in xs),
            "tokens_in": sum(x["tokens_in"] for x in xs), "tokens_out": sum(x["tokens_out"] for x in xs),
            "dollars": round(sum(x["dollars"] for x in xs), 5),
            "seconds_sum_of_calls": round(sum(x["seconds"] for x in xs), 2),
            "by_tag": {tg: sum(1 for x in xs if x["tag"] == tg) for tg in sorted({x["tag"] for x in xs})}}


cost = {"gse": gse, "label": label, "samples": len(s["samples"]), "deepseek": tot("deepseek"), "jev": tot("typesafe"),
        "wall_seconds_this_process": wall,
        "note": "seconds_sum_of_calls is each call's own latency from the first (live) run, cached or not; "
                "Jev requests ran 8 at a time, so its wall time is shorter than the sum"}
prev = run_dir(gse) / f"cost.{label}.json"
if not prev.exists() or cost["jev"]["live_calls"] or cost["deepseek"]["live_calls"]:
    prev.write_text(json.dumps(cost, indent=1))
print(json.dumps({"gse": gse, "samples": len(s["samples"]),
                  "quote_check": len(l["deepseek_quote_check"]["failed"]),
                  "factors": [f["name"] for f in r["factors"]], "donors": len(a["donors"]), "targets": a["targets"],
                  "relations": {k: len(v) for k, v in r["relations"].items()},
                  "deepseek": cost["deepseek"]["dollars"], "jev": cost["jev"]["dollars"], "wall": wall}, indent=None))
