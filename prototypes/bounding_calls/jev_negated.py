# PROTOTYPE, throwaway: ask Jev each bounding question in the negated direction, to see whether
# p(yes) on the plain question and 1 - p(yes) on the negated one agree.
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run  # noqa: E402
from labllm import Gateway  # noqa: E402


def negated(group, st):
    picked = st["picked"]
    target = (f"the Series under SuperSeries {picked}" if st["superseries"] == picked
              else f"GEO Series {picked}")
    qs = {}
    for s in group["series"]:
        if s["id"] == st["superseries"] or s["id"] in st["core"] and st["superseries"] is None:
            continue
        qs[f"s_{s['id']}"] = {"type": "noul", "instructions": (
            f"Is GEO Series {s['id']} an older dataset that the research of {target} only re-uses "
            "or cites, a later dataset that re-uses its samples, or an unrelated study, rather "
            "than data deposited for the same research?")}
    for p in group["papers"]:
        qs[f"p_{p['pmid']}"] = {"type": "noul", "instructions": (
            f"Is paper {p['id']} a later paper that re-uses the data of {target}, a paper that "
            "reports another dataset of this group, or a paper linked by mistake, rather than "
            "the paper that first reports that data (a preprint and its journal version both "
            "count as the paper)?")}
    return qs


def main():
    groups = run.load_groups([])
    gw = Gateway(repo="seqforge")
    meter = gw.meter("prototype bounding calls, negated Jev (#617)", ceiling_usd=0.2)
    out = {}

    def do(g):
        st = run.structure(groups[g])
        qs = negated(groups[g], st)
        f = run.LOGS / g / "jev-negated.json"
        if f.exists():
            out[g] = json.loads(f.read_text())
            return
        if not qs:
            out[g] = {"answers": {}}
            return
        t = time.time()
        r = meter.jev(run.JEV, description=run.drafter_text(groups[g]), questions=qs,
                      tag=f"{g}/jev-negated")
        rec = {"seconds": round(time.time() - t, 2), "first_cost_usd": r.first_cost_usd,
               "answers": r.answers}
        f.write_text(json.dumps(rec))
        out[g] = rec

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(do, list(groups)))
    rows = json.loads((run.HERE / "results.json").read_text())
    pairs = []
    for r in rows:
        neg = out[r["id"]]["answers"]
        for rid, py in r["jev_p_yes"].items():
            if py is None:
                continue
            key = f"s_{rid}" if rid.startswith("GSE") else f"p_{rid.split(':')[1]}"
            pn = neg.get(key, {}).get("noul")
            pairs.append({"study": r["id"], "id": rid, "p_yes": py, "p_neg": pn,
                          "agree": (py > 0.5) == (pn < 0.5),
                          "standard_in": rid in r["standard"]["in"]})
    (run.HERE / "jev_negated.json").write_text(json.dumps(pairs, indent=1))
    print("pairs", len(pairs), "placement agrees", sum(p["agree"] for p in pairs))
    for p in pairs:
        if not p["agree"] or abs(p["p_yes"] - (1 - p["p_neg"])) > 0.3:
            print(p)
    gap = sorted(abs(p["p_yes"] - (1 - p["p_neg"])) for p in pairs)
    print("abs gap p_yes vs 1-p_neg: median %.3f p90 %.3f max %.3f" % (
        gap[len(gap) // 2], gap[int(len(gap) * 0.9)], gap[-1]))
    neg_wrong = [p for p in pairs if (p["p_neg"] < 0.5) != p["standard_in"]]
    print("negated-only placement differs from standard:", neg_wrong)
    print("spent first-cost %.5f" % sum(o.get("first_cost_usd", 0) for o in out.values()))


if __name__ == "__main__":
    main()
