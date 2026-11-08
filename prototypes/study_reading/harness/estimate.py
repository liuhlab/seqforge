# PROTOTYPE, throwaway: estimate questions, dollars and minutes for a study of N samples, from the
# tokens and latencies measured on the dev study (runs/<GSE>/probes.json and calls.jsonl).
import json
import sys

from common import DS_PRICE_IN_MISS, DS_PRICE_OUT, JEV_PRICE_IN, load

GSE = sys.argv[1] if len(sys.argv) > 1 else "GSE201250"
P = load(GSE, "probes.json")["P2_P3_size_context"]
n_dev = 15
# measured: one request per sample with the sample's own record (+lists)
atomic = P["atomic_chars"]["cost"]          # 1 question per request
packed = P["packed_chars"]["cost"]          # 4 questions per request
per_req_1q = atomic["tokens_in"] / atomic["requests"]
per_q_extra = (packed["tokens_in"] / packed["requests"] - per_req_1q) / 3
state_tokens = per_req_1q - per_q_extra
lat = packed["mean_request_s"]
calls = [json.loads(x) for x in open(f"../runs/{GSE}/calls.jsonl")]
writer = next(c for c in calls if c["provider"] == "deepseek")
study_req = next(c for c in calls if c["tag"] == "study")
pair_req = P.get("pair_tokens", 760)  # P5 pairwise: ~760 tokens per two-record request (measured 79.5k / 105)

out = {"measured_on": GSE, "state_tokens_per_sample_request": round(state_tokens),
       "tokens_per_extra_question": round(per_q_extra), "mean_request_s": lat}
for N in (100, 1000):
    K = 4                          # candidate keys (3 factors + donor), as on the dev study
    q_sample = K + 3               # levels + assay + role + control_type (speculative)
    sample_tok = N * (state_tokens + per_q_extra * q_sample)
    study_tok = study_req["tokens_in"]          # study request; at N>~100 send a value table, not every sample
    pairs = N * 1                  # biosample yes/no only inside code blocks (replicate triples -> ~N pairs)
    pair_tok = pairs * pair_req
    recheck = 0.1 * N * q_sample   # re-ask, options reversed, every Choice under p 0.9 (dev: ~5% of labels)
    recheck_tok = recheck * (state_tokens / q_sample + per_q_extra)
    jev_tok = sample_tok + study_tok + pair_tok + recheck_tok
    questions = N * q_sample + 2 * K + 5 + pairs + recheck
    requests = N + 1 + pairs + 0.1 * N
    ds_in = writer["tokens_in"] / n_dev * N   # writer prompt grows ~linearly with sample lines
    ds_out = writer["tokens_out"] * 2          # lists, not per-sample labels; doubled for safety
    ds_usd = (ds_in * DS_PRICE_IN_MISS + ds_out * DS_PRICE_OUT) / 1e6
    out[f"N={N}"] = {
        "jev_questions": round(questions), "jev_requests": round(requests), "jev_tokens": round(jev_tok),
        "jev_usd": round(jev_tok * JEV_PRICE_IN / 1e6, 4), "deepseek_usd_if_needed": round(ds_usd, 4),
        "minutes_8_parallel": round(requests * lat / 8 / 60 + (writer["seconds"] * N / n_dev) / 60, 2),
        "pairwise_all_pairs_instead": {"questions": N * (N - 1) // 2,
                                       "usd": round(N * (N - 1) / 2 * pair_req * JEV_PRICE_IN / 1e6, 2)},
        "whole_study_state_fits_64k": N * (P["batch_chars"]["cost"]["tokens_in"] / n_dev) < 32000,
    }
print(json.dumps(out, indent=1))
json.dump(out, open(f"../runs/{GSE}/estimate.json", "w"), indent=1)
