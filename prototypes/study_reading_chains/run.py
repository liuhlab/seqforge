# PROTOTYPE, throwaway (issue #629): run one chain variant over the standard set.
#
#   python run.py --chain code                     # code lists, Jev answers
#   python run.py --chain flash [--full]           # DeepSeek Flash, thinking off
#   python run.py --chain flash_think [--full]     # DeepSeek Flash, thinking on
#   --only GSE1,GSE2 to pilot on a few studies.
#
# Writes out/<variant>/<ID>.reading.yaml (passes the format check) or <ID>.refused.yaml, and
# out/<variant>/<ID>.result.json (counts, picks, costs; no paper text beyond short quotes).
# Raw model replies go to _logs/ (git-ignored). Needs LABLLM_GATEWAY (the tunnel).
import argparse
import copy
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import yaml  # noqa: E402
from labllm import Gateway  # noqa: E402

import chain as C  # noqa: E402
import material as M  # noqa: E402
from seqforge.study.claims import load as load_yaml  # noqa: E402
from seqforge.study.reading import check_reading  # noqa: E402

OUT = HERE / "out"
CAP = {"deepseek": 24.0, "jev": 24.0}  # stop before $25 per model, all variants together


def spent_so_far():
    tot = {"deepseek": 0.0, "jev": 0.0}
    for f in OUT.glob("*/*.result.json"):
        r = json.loads(f.read_text())
        for e in r.get("ledger", []):
            tot[e["model"]] += e.get("cost") or 0.0
    return tot


def repaired_check(text):
    """Would the file pass if levels no sample is at were dropped (a mechanical repair)?"""
    data, _ = load_yaml(text)
    if not isinstance(data, dict):
        return None
    d = copy.deepcopy(data)
    rows = {g: {**(d.get("shared") or {}), **own} for g, own in (d.get("samples") or {}).items()}
    for lb, f in (d.get("factors") or {}).items():
        used = {r.get(lb, {}).get("level") for r in rows.values()}
        f["levels"] = [x for x in f["levels"] if x in used]
        if f.get("baseline") not in f["levels"]:
            f["baseline"] = None
    return check_reading(d)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", required=True, choices=["code", "flash", "flash_think"])
    ap.add_argument("--full", action="store_true", help="the writer reads full text")
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    variant = args.chain + ("_full" if args.full else "")
    ids = [x for x in args.only.split(",") if x] or M.study_ids()
    if args.full:
        ids = [g for g in ids if M.has_full(M.load(g))]
    before = spent_so_far()
    print(f"spent so far: {before}", flush=True)
    for k in before:
        if before[k] >= CAP[k]:
            sys.exit(f"{k} spend {before[k]:.2f} reached the cap")
    gw = Gateway(repo="seqforge")
    meters = {"jev": gw.meter(f"prototype #629 {variant} jev", ceiling_usd=CAP["jev"] - before["jev"]),
              "deepseek": gw.meter(f"prototype #629 {variant} deepseek",
                                   ceiling_usd=CAP["deepseek"] - before["deepseek"])}
    (OUT / variant).mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    # Writer calls first, 8 in flight (they are the slow ones); cached in _logs.
    if args.chain != "code":
        def prefetch(g):
            calls = C.Calls(meters, g, variant)
            m = M.load(g)
            C.writer_lists(m, calls, thinking=args.chain == "flash_think", full=args.full)
            e = calls.ledger[-1]
            print(f"writer {g} {e['seconds']:.1f}s ${e['cost']:.4f} out {e.get('tokens_out')}",
                  flush=True)

        with ThreadPoolExecutor(8) as ex:
            list(ex.map(prefetch, ids))
        print(f"writers done in {time.time() - t0:.0f}s; deepseek spent ${meters['deepseek'].spent_usd:.4f}",
              flush=True)
    def one(g):
        with _plock:
            pass
        do_study(g)

    def do_study(g):
        if True:
            try:
                res, text = C.run_study(g, args.chain, meters, read_full=args.full, pool=pool)
            except Exception as e:  # noqa: BLE001 - prototype: a crash is a refusal with a reason
                import traceback
                res, text = {"study": g, "chain": args.chain, "read_full": args.full,
                             "ending": "refused", "reason": f"crash: {e!r}",
                             "trace": traceback.format_exc()[-2000:],
                             "ledger": getattr(C.REGISTRY.get((variant, g)), "ledger", [])}, None
            res["variant"] = variant
            if text is not None:
                (OUT / variant / f"{g}.reading.yaml").write_text(text)
            else:
                if res.get("problems") and "reason" not in res:
                    res["reason"] = "the format check rejected the answer"
                (OUT / variant / f"{g}.refused.yaml").write_text(yaml.safe_dump(
                    {"study": g, "refused": res.get("reason"), "problems": res.get("problems")},
                    sort_keys=False, width=100))
            C.jsave(OUT / variant / f"{g}.result.json", res)
            cost = sum(e.get("cost") or 0 for e in res.get("ledger", []))
            print(f"{variant} {g} {res['ending']:9s} {res.get('seconds', 0):.1f}s ${cost:.4f} "
                  f"problems={len(res.get('problems') or [])} "
                  f"{'; '.join(res.get('unsettled_reasons') or [])[:120]}", flush=True)

    import threading
    _plock = threading.Lock()
    # Two studies at a time; per-sample requests share a pool of 6: at most 8 requests in flight.
    with ThreadPoolExecutor(6) as pool, ThreadPoolExecutor(2) as studies:
        list(studies.map(one, ids))
    print(f"done {variant}: {len(ids)} studies in {time.time() - t0:.0f}s; jev ${meters['jev'].spent_usd:.4f}"
          f" deepseek ${meters['deepseek'].spent_usd:.4f}; ontology {C.O.STATS}", flush=True)


if __name__ == "__main__":
    main()
