# PROTOTYPE, throwaway: cost and time per study and harness version, from runs/<GSE>/cost.<label>.json (every call
# one reading used, cached or live, with the meta of its first live run) and calls.jsonl (live wall time per label).
import json
import sys

from common import RUNS

STUDIES = ["GSE164798", "GSE190102", "GSE206603"]


def live_wall(gse, label, provider):
    xs = [json.loads(x) for x in open(RUNS / gse / "calls.jsonl") if x.strip()]
    xs = [x for x in xs if x.get("run") == label and x["provider"] == provider]
    if not xs:
        return None
    return round(max(x["ts"] for x in xs) - min(x["ts"] - x["seconds"] for x in xs), 2)


def table(labels):
    out = {}
    for g in STUDIES:
        for lb in labels:
            f = RUNS / g / f"cost.{lb}.json"
            if not f.exists():
                continue
            c = json.loads(f.read_text())
            out[f"{g} {lb}"] = {
                "samples": c["samples"],
                "deepseek": {k: c["deepseek"][k] for k in ("calls", "tokens_in", "tokens_out", "dollars",
                                                           "seconds_sum_of_calls")},
                "jev": {k: c["jev"][k] for k in ("calls", "questions", "tokens_in", "dollars", "seconds_sum_of_calls",
                                                 "by_tag")},
                "jev_live_wall_s": live_wall(g, lb, "typesafe"),
            }
    return out


if __name__ == "__main__":
    t = table(sys.argv[1:] or ["v1", "v2", "v3"])
    print("| study, version | samples | DeepSeek calls | DS tokens in/out | DS $ | DS s | Jev requests | Jev questions | Jev tokens in | Jev $ | Jev s (sum) | Jev wall s (8 parallel) |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for k, v in t.items():
        d, j = v["deepseek"], v["jev"]
        print(f"| {k} | {v['samples']} | {d['calls']} | {d['tokens_in']:,}/{d['tokens_out']:,} | {d['dollars']:.4f} | "
              f"{d['seconds_sum_of_calls']} | {j['calls']} | {j['questions']} | {j['tokens_in']:,} | {j['dollars']:.4f} | "
              f"{j['seconds_sum_of_calls']} | {v['jev_live_wall_s']} |")
    json.dump(t, open(RUNS / "cost_summary.json", "w"), indent=1)


def estimate(label="v2"):
    """100 and 1,000 samples, from the v2 runs' measured means (per request kind) and stated assumptions."""
    from common import DS_PRICE_IN_MISS, DS_PRICE_OUT, JEV_PRICE_IN
    reqs, ds, wall = {}, [], []
    for g in STUDIES:
        xs = [json.loads(x) for x in open(RUNS / g / "calls.jsonl") if x.strip()]
        xs = [x for x in xs if x.get("run") == label]
        n = json.loads((RUNS / g / f"cost.{label}.json").read_text())["samples"]
        for x in xs:
            if x["provider"] == "typesafe":
                reqs.setdefault(x["tag"], []).append((x["tokens_in"], n))
        ds += [(x["tokens_in"], x["tokens_out"], x["seconds"], n) for x in xs if x["provider"] == "deepseek"][-1:]
        w = live_wall(g, label, "typesafe")
        k = sum(1 for x in xs if x["provider"] == "typesafe")
        wall.append(w / k)
    tok_sample = sum(t for t, _ in reqs["sample"]) / len(reqs["sample"])
    tok_pair = sum(t for t, _ in reqs["biosample_pair"]) / len(reqs["biosample_pair"])
    pairs_per_sample = len(reqs["biosample_pair"]) / sum(json.loads((RUNS / g / f"cost.{label}.json").read_text())["samples"] for g in STUDIES)
    per_line = sum((t - 2000) / n for t, n in reqs["study"]) / len(reqs["study"])  # study request ~2k + per sample line
    ds_line = sum((ti - 4500) / n for ti, to, s, n in ds) / len(ds)  # writer: ~4.5k (prompt, summaries, paper) + per line
    eff = sum(wall) / len(wall)  # measured seconds per request at 8 in parallel
    out = {"measured_means": {"jev_tokens_per_sample_request": round(tok_sample), "jev_tokens_per_pair_request": round(tok_pair),
                              "pairs_per_sample": round(pairs_per_sample, 2), "study_request_tokens_per_sample_line": round(per_line),
                              "deepseek_tokens_per_sample_line": round(ds_line), "jev_wall_s_per_request_8_parallel": round(eff, 3)}}
    for N in (100, 1000):
        study_tok = min(2000 + per_line * N, 30000)  # past ~280 samples send a table of distinct values instead
        jev_tok = N * tok_sample + N * pairs_per_sample * tok_pair + study_tok
        req = 1 + N + N * pairs_per_sample
        chunks = max(1, -(-N // 150))  # writer: one call per 150 sample lines, plus one merge call when chunked
        ds_in = chunks * (4500 + ds_line * min(N, 150)) + (3000 if chunks > 1 else 0)
        ds_out = 700 * chunks + (2000 if chunks > 1 else 0)
        out[f"N={N}"] = {"jev_requests": round(req), "jev_tokens": round(jev_tok), "jev_usd": round(jev_tok * JEV_PRICE_IN / 1e6, 3),
                         "deepseek_calls": chunks + (1 if chunks > 1 else 0), "deepseek_tokens_in": round(ds_in),
                         "deepseek_usd": round((ds_in * DS_PRICE_IN_MISS + ds_out * DS_PRICE_OUT) / 1e6, 3),
                         "jev_minutes_8_parallel": round(req * eff / 60, 1),
                         "deepseek_seconds_parallel_chunks": round(4 + (4 if chunks > 1 else 0))}
    return out


if __name__ == "__main__":
    e = estimate()
    print(json.dumps(e, indent=1))
    json.dump(e, open(RUNS / "estimate_three_studies.json", "w"), indent=1)
