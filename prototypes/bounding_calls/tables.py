# PROTOTYPE, throwaway: the markdown tables RESULTS.md quotes, from results.json, jev_negated.json
# and the thinking variant's placement (read from _logs/, ids only are printed).
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
rows = json.loads((HERE / "results.json").read_text())
neg = {(p["study"], p["id"]): p["p_neg"] for p in json.loads((HERE / "jev_negated.json").read_text())}


def thinking_in(gid):
    f = HERE / "_logs" / gid / "writer-thinking.json"
    if not f.exists():
        return None
    a = json.loads(f.read_text()).get("answer") or {}
    return {str(e.get("id")) for k in ("series", "papers") for e in a.get(k) or []}


def mark(b):
    return "in" if b else "out"


print("| Study | Record | Standard set | Writer (thinking off) | Writer (thinking on) | Jev p(yes) | Jev negated p(yes) | Code: own paper |")
print("|---|---|---|---|---|---|---|---|")
for r in rows:
    if r["structure"]["one_one"]:
        continue
    th = thinking_in(r["id"])
    for rid in sorted(set(r["standard"]["in"]) | set(r["standard"]["out"])):
        py = r["jev_p_yes"].get(rid)
        if rid == r["structure"]["superseries"]:
            py_s = "code: SuperSeries"
        elif py is None:
            py_s = "code: named by it"
        else:
            py_s = f"{py:.2f}"
        pn = neg.get((r["id"], rid))
        sign = r["structure"]["recite_sign"].get(rid)
        w = mark(rid in r["placement"]["in"])
        if rid in r["placement"]["in"] and rid in r["placement"]["out"]:
            w = "both (refused)"
        s = mark(rid in r["standard"]["in"])
        flag = " **≠**" if w != s else ""
        print(f"| {r['id']} | {rid} | {s} | {w}{flag} | {mark(rid in th) if th is not None else '-'} | "
              f"{py_s} | {'-' if pn is None else f'{pn:.2f}'} | "
              f"{'-' if sign is None else ('yes' if sign else 'no')} |")

print()
print("1+1 groups, paper p(yes):", sorted(Counter(
    f"{min(int(p * 10), 9) / 10:.1f}" for r in rows if r["structure"]["one_one"]
    for p in r["paper_p"].values() if p is not None).items()))
print("lowest 1+1 paper p:", sorted((p, r["id"]) for r in rows if r["structure"]["one_one"]
                                     for p in r["paper_p"].values() if p is not None)[:4])
