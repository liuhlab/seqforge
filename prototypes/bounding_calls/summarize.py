# PROTOTYPE, throwaway: aggregates over results.json (and _logs/ for the thinking variant).
import json
import statistics as stx
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
rows = json.loads((HERE / "results.json").read_text())


def bucket(p):
    if p is None:
        return "null"
    b = min(int(p * 10), 9)
    return f"{b / 10:.1f}-{(b + 1) / 10:.1f}"


def cost(part):
    return (part or {}).get("first_cost_usd") or 0.0


print("studies", len(rows))
one = [r for r in rows if r["structure"]["one_one"]]
print("1+1 groups", len(one), "other", len(rows) - len(one))
print("format problems", sum(bool(r["problems"]) for r in rows),
      [(r["id"], r["problems"]) for r in rows if r["problems"]])

print("\n== differences from the standard set (records placed differently) ==")
for name, key in (("sequence", "differs"), ("jev only", "jev_only_differs")):
    d = [(r["id"], r[key]) for r in rows if r[key]]
    print(name, len(d), d)
alt = [(r["id"], r["writer_on_one_one"]["placement"]) for r in one
       if r["writer_on_one_one"]["placement"] != [r["placement"]["in"], r["placement"]["out"]]]
print("writer on 1+1 differs from code default:", alt)
print("1+1 where standard set differs from code default:",
      [r["id"] for r in one if r["differs"]])

print("\n== quotes ==")
checks = [c for r in rows if r["writer_used"] for c in r["quote_checks"]]
checks += [c for r in one for c in r["writer_on_one_one"]["checks"]]
print("claims with a writer quote considered", len(checks))
print("grep-back status", Counter(c["status"] for c in checks))
print("by field", Counter((c.get("field") or "-") for c in checks))
ok = [c for c in checks if c["status"] in ("named_field", "other_field_same_source", "other_source")]
print("entail p buckets (grepped back)", sorted(Counter(bucket(c["entail_p"]) for c in ok).items()))
print("entail >=0.5 by field", Counter(c["field"] for c in ok if (c["entail_p"] or 0) >= 0.5))
print("entail >=0.5 by list", Counter(c["list"] for c in ok if (c["entail_p"] or 0) >= 0.5),
      "of", Counter(c["list"] for c in ok))
seq_claims = []
for r in rows:
    b = json.loads(json.dumps(r))  # noqa
print("in the sequence's bounds files:")
import yaml  # noqa: E402

marks = Counter()
for r in rows:
    f = HERE / "out" / f"{r['id']}.bounds.yaml"
    if not f.exists():
        continue
    b = yaml.safe_load(f.read_text())
    for c in b["series"] + b["left_out"]:
        marks[[k for k in ("quoted", "inferred", "unknown") if k in c][0]] += 1
print(" claim backing", marks)

print("\n== Jev probabilities ==")
claim_p, paper_p, yes = [], [], []
for r in rows:
    # Every record Jev was asked about, whatever became of the study's file.
    yes += [p for p in r["jev_p_yes"].values() if p is not None]
    f = HERE / "out" / f"{r['id']}.bounds.yaml"
    if not f.exists():  # refused: no bounds file
        continue
    b = yaml.safe_load(f.read_text())
    claim_p += [c["p"] for c in b["series"] + b["left_out"]]
    paper_p += [p for p in r["paper_p"].values()]
print("Series + left_out claims p", sorted(Counter(bucket(p) for p in claim_p).items()))
print("papers kept, p", sorted(Counter(bucket(p) for p in paper_p).items()))
print("all p(yes) per record", sorted(Counter(bucket(p) for p in yes).items()))
low = [(r["id"], rid, p) for r in rows for rid, p in r["jev_p_yes"].items()
       if p is not None and 0.2 <= p <= 0.8]
print("records with p(yes) in 0.2-0.8:", low)

print("\n== re-cite sign (code) ==")
for r in rows:
    for gse, sign in r["structure"]["recite_sign"].items():
        print(r["id"], gse, "own paper" if sign else "no own paper",
              "standard:", "in" if gse in r["standard"]["in"] else "out",
              "seq:", "in" if gse in r["placement"]["in"] else "out",
              "jev p(yes):", r["jev_p_yes"].get(gse))

print("\n== cost and time per study ==")
per, secs, nfresh = [], [], 0
for r in rows:
    w = r["writer"] if r["writer_used"] else None
    c = cost(r["jev"]) + cost(w) + (cost(r["entail"]) if w else 0)
    per.append(c)
    fresh = not (r["jev"].get("cached") or (w or {}).get("cached"))
    if fresh:
        nfresh += 1
        js = r["jev"]["seconds"] or 0
        ws = ((w or {}).get("seconds") or 0) + ((r["entail"] or {}).get("seconds") or 0 if w else 0)
        secs.append((r["id"], js, ws, max(js, ws), js + ws, bool(w)))
print("cost per study: median %.5f max %.5f total %.4f" % (stx.median(per), max(per), sum(per)))
for label, sel in (("all", secs), ("1+1", [s for s in secs if not s[5]]),
                   ("writer", [s for s in secs if s[5]])):
    if sel:
        par = [s[3] for s in sel]
        seq = [s[4] for s in sel]
        print(f"{label}: n={len(sel)} parallel median {stx.median(par):.2f}s max {max(par):.2f}s; "
              f"sequential median {stx.median(seq):.2f}s max {max(seq):.2f}s; sum {sum(seq):.1f}s")
wr = [r["writer"] for r in rows if r["writer"] and not r["writer"].get("cached")]
print("writer calls (incl 1+1): n", len(wr), "s median", stx.median(w["seconds"] for w in wr),
      "max", max(w["seconds"] for w in wr),
      "tokens in median", stx.median(w["usage"]["prompt_tokens"] for w in wr),
      "out median", stx.median(w["usage"]["completion_tokens"] for w in wr),
      "cost median %.5f" % stx.median(w["first_cost_usd"] for w in wr))
jv = [r["jev"] for r in rows if not r["jev"].get("cached") and r["jev"].get("usage")]
print("jev calls: n", len(jv), "s median", stx.median(j["seconds"] for j in jv), "max",
      max(j["seconds"] for j in jv), "tokens in median",
      stx.median(j["usage"]["input_tokens"] for j in jv), "max",
      max(j["usage"]["input_tokens"] for j in jv))
writer_one = sum(cost(r["writer"]) + cost(r["entail"]) for r in one)
print("writer+entail cost on the 1+1 groups (skipped in the sequence): %.4f" % writer_one)

print("\n== thinking variant ==")
th = []
for r in rows:
    f = HERE / "_logs" / r["id"] / "writer-thinking.json"
    if f.exists():
        t = json.loads(f.read_text())
        a = t.get("answer") or {}
        ins = {str(e.get("id")) for k in ("series", "papers") for e in a.get(k) or []}
        th.append((r["id"], t["seconds"], t.get("first_cost_usd"), t["usage"].get("completion_tokens"),
                   (t["usage"].get("completion_tokens_details") or {}).get("reasoning_tokens"),
                   sorted(ins ^ set(r["standard"]["in"])),
                   sorted(ins ^ set(r["placement"]["in"])) if r["writer_used"] else None))
for x in th:
    print(x)
if th:
    print("thinking: n", len(th), "s median", stx.median(x[1] for x in th), "max", max(x[1] for x in th),
          "cost median %.5f total %.4f" % (stx.median(x[2] for x in th), sum(x[2] for x in th)),
          "differs from standard", sum(bool(x[5]) for x in th))
