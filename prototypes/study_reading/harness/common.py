# PROTOTYPE, throwaway: shared plumbing for the study-reading harness.
# Model calls (DeepSeek, Jev) are cached by request hash and logged to calls.jsonl.
import hashlib
import json
import os
import pathlib
import threading
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
RUNS = HERE.parent / "runs"

JEV_MODEL = "jev-1.14.0"
JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_PRICE_IN = 0.042  # $ per million input tokens; output is free
DS_MODEL = "deepseek-chat"
DS_URL = "https://api.deepseek.com/chat/completions"
# deepseek-chat is served at the Flash price; peak prices taken as the conservative case.
DS_PRICE_IN_MISS, DS_PRICE_IN_HIT, DS_PRICE_OUT = 0.30, 0.006, 1.20
SPEND_CAP = 9.0  # stop before the $10 total cap

_lock = threading.Lock()


def run_dir(gse):
    d = RUNS / gse
    (d / "raw").mkdir(parents=True, exist_ok=True)
    (d / "cache" / "jev").mkdir(parents=True, exist_ok=True)
    (d / "cache" / "deepseek").mkdir(parents=True, exist_ok=True)
    return d


def total_spend():
    s = 0.0
    for f in RUNS.glob("*/calls.jsonl"):
        for line in f.read_text().splitlines():
            if line.strip():
                s += json.loads(line).get("dollars", 0.0)
    return s


def _key(obj):
    # no sort_keys: Choice option order is part of the question (the option-order probe depends on it)
    return hashlib.sha256(json.dumps(obj).encode()).hexdigest()[:24]


def _log(gse, rec):
    with _lock:
        with open(run_dir(gse) / "calls.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")


def _post(url, body, headers, timeout=180, tries=5):
    data = json.dumps(body).encode()
    for i in range(tries):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:500]
            if e.code in (429, 500, 502, 503, 529) and i < tries - 1:
                time.sleep(2 ** i)
                continue
            raise RuntimeError(f"HTTP {e.code}: {msg}") from None
        except (urllib.error.URLError, TimeoutError):
            if i < tries - 1:
                time.sleep(2 ** i)
                continue
            raise


def jev(gse, state, questions, tag, long_context=False):
    """One Jev request. Returns (answers, meta). Cached; live calls are logged."""
    body = {"model": JEV_MODEL, "state": state, "questions": questions}
    k = _key(body)
    cf = run_dir(gse) / "cache" / "jev" / f"{k}.json"
    if cf.exists():
        r = json.loads(cf.read_text())
        return r["answers"], {"cached": True, **r["_meta"]}
    if total_spend() > SPEND_CAP:
        raise SystemExit("spend cap reached")
    headers = {
        "Authorization": "Bearer " + os.environ["TYPESAFE_API_KEY"],
        "Content-Type": "application/json",
    }
    if long_context:
        headers["TypeSafe-Danger-Zone-Long-Context"] = "true"
    t0 = time.time()
    r = _post(JEV_URL, body, headers)
    dt = time.time() - t0
    tin, tout = r["usage"]["input_tokens"], r["usage"]["output_tokens"]
    meta = {
        "model": r["model"], "tokens_in": tin, "tokens_out": tout,
        "dollars": tin * JEV_PRICE_IN / 1e6, "seconds": round(dt, 3),
        "n_questions": len(questions),
    }
    r["_meta"] = meta
    cf.write_text(json.dumps(r))
    _log(gse, {"provider": "typesafe", "tag": tag, "key": k, **meta, "ts": time.time()})
    return r["answers"], {"cached": False, **meta}


def deepseek(gse, system, user, tag, max_tokens=8000):
    body = {
        "model": DS_MODEL, "temperature": 0, "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    k = _key(body)
    cf = run_dir(gse) / "cache" / "deepseek" / f"{k}.json"
    if cf.exists():
        r = json.loads(cf.read_text())
        return json.loads(r["choices"][0]["message"]["content"]), {"cached": True, **r["_meta"]}
    if total_spend() > SPEND_CAP:
        raise SystemExit("spend cap reached")
    headers = {
        "Authorization": "Bearer " + os.environ["DEEPSEEK_API_KEY"],
        "Content-Type": "application/json",
    }
    t0 = time.time()
    r = _post(DS_URL, body, headers, timeout=600)
    dt = time.time() - t0
    u = r["usage"]
    hit = u.get("prompt_cache_hit_tokens", 0)
    miss = u.get("prompt_tokens", 0) - hit
    out = u.get("completion_tokens", 0)
    meta = {
        "model": r.get("model"), "tokens_in": u.get("prompt_tokens", 0), "tokens_out": out,
        "dollars": (miss * DS_PRICE_IN_MISS + hit * DS_PRICE_IN_HIT + out * DS_PRICE_OUT) / 1e6,
        "seconds": round(dt, 3),
    }
    r["_meta"] = meta
    cf.write_text(json.dumps(r))
    _log(gse, {"provider": "deepseek", "tag": tag, "key": k, **meta, "ts": time.time()})
    return json.loads(r["choices"][0]["message"]["content"]), {"cached": False, **meta}


def jev_many(gse, jobs, tag, workers=8):
    """jobs: list of (state, questions). Runs requests concurrently; returns list of (answers, meta)."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(workers) as ex:
        futs = [ex.submit(jev, gse, s, q, tag) for s, q in jobs]
        return [f.result() for f in futs]


def est_tokens(obj):
    return len(json.dumps(obj)) // 3


def load(gse, name):
    return json.loads((run_dir(gse) / name).read_text())


def save(gse, name, obj):
    (run_dir(gse) / name).write_text(json.dumps(obj, indent=1, ensure_ascii=False))
