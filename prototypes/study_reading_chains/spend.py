# PROTOTYPE, throwaway (issue #629): what this prototype paid, per model, from every logged call.
# Each reply in _logs/ records cost_usd, what that call paid (zero when the gateway's cache answered).
import json
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
tot, n = defaultdict(float), defaultdict(int)
for f in (HERE / "_logs").rglob("*.json"):
    r = json.loads(f.read_text())
    if "cost_usd" not in r:
        continue
    model = "deepseek" if f.name.startswith("writer_") else "jev"
    tot[model] += r["cost_usd"]
    n[model] += 1
for k in sorted(tot):
    print(f"{k}: ${tot[k]:.4f} over {n[k]} logged calls")
