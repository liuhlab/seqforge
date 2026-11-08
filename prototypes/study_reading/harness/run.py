# PROTOTYPE, throwaway: read one GEO study end to end.
#   pixi run python run.py GSE201250 [--writer auto|always] [--probes]
# fetch -> draft open lists (code, or DeepSeek) -> quote check -> Jev closed questions -> derive -> reading.yaml
import json
import sys

import ask
import derive
import draft
import fetch
from common import run_dir

gse = sys.argv[1]
writer = sys.argv[sys.argv.index("--writer") + 1] if "--writer" in sys.argv else "auto"
n0 = sum(1 for _ in open(run_dir(gse) / "calls.jsonl")) if (run_dir(gse) / "calls.jsonl").exists() else 0
s = fetch.fetch(gse)
l = draft.draft(gse, writer)
a = ask.ask(gse)
r = derive.derive(gse)
if "--probes" in sys.argv:
    import subprocess
    subprocess.run([sys.executable, "probes.py", gse], check=True)
n1 = sum(1 for _ in open(run_dir(gse) / "calls.jsonl"))
print(json.dumps({"gse": gse, "samples": len(s["samples"]), "lists_from": l["used"],
                  "quote_check": {k: len(v["failed"]) for k, v in l.items() if k.endswith("quote_check")},
                  "factors": [f["name"] for f in r["factors"]],
                  "relations": {k: len(v) for k, v in r["relations"].items()},
                  "live_model_calls_this_run": n1 - n0}, indent=1))
