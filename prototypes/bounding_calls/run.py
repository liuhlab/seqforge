# PROTOTYPE, throwaway: the model calls that draw a study's bounds from its linked group.
#
# Sequence per study (see RESULTS.md):
#   0. code   reads the linked group from the toy store, and flags structure (shape, re-cite sign)
#   1. writer DeepSeek Flash writes the lists, with one quoted passage per claim (skippable: 1+1)
#   2. code   greps every quote back into the named record field or abstract
#   3. Jev    one request: a yes/no per linked Series and per paper, neutral to the writer
#   4. code   builds the bounds file (format 1), gives each claim p from Jev, runs the format check
#
# Raw replies (they hold abstract text) go to _logs/, which git ignores. Run from the worktree root:
#   python prototypes/bounding_calls/run.py [--only GSE1,GSE2] [--thinking]
import argparse
import copy
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402
from labllm import Gateway, Model  # noqa: E402

from seqforge.standard_set.draft import RULES  # noqa: E402
from seqforge.standard_set.material import (  # noqa: E402
    drafter_text,
    open_sources,
    pick_entries,
    read_group,
)
from seqforge.study.bounds import check_bounds  # noqa: E402

SET = ROOT / "evals" / "standard-set"
LOGS = HERE / "_logs"
OUT = HERE / "out"
JEV = Model("typesafe", "jev-1.14.0")
WRITER_NAME = "deepseek-flash"
_lock = threading.Lock()

FIELD_GEO = {
    "Title": "Series_title",
    "Type": "Series_type",
    "Summary": "Series_summary",
    "Overall design": "Series_overall_design",
    "Citation": "Series_citation",
    "Relation": "Series_relation",
}

WRITER_SYSTEM = (
    "You draw the bounds of one study from GEO records and paper abstracts.\n\n"
    + RULES
    + """
Back every entry of "series", "papers" and "left_out" with a quote when the material has one:
words copied exactly, one continuous passage of at most 40 words, from a Series record field or a
paper's title or abstract, that shows why the record is in or out. Give "source" (the GSE or
pmid:<number> the words are from) and "field" (for a Series: Title, Summary, Overall design,
Citation, Relation or Samples; for a paper: Title or Abstract). Do not change, join or shorten
the words. A title only says what a record is about: prefer a passage that says the record is or
is not this study's, such as a stated re-use or reanalysis, a data statement, or a sentence that
names the other dataset. If no passage shows it, set "quote" to null. Always give "how": at most
15 words on how you decided.

Answer with one JSON object of this shape:
{"series": [{"id": "GSE...", "quote": {"source": "...", "field": "...", "text": "..."}, "how": "..."}],
 "papers": [{"id": "pmid:...", "quote": null, "how": "..."}],
 "left_out": [{"id": "GSE... or pmid:...", "quote": {...}, "how": "..."}],
 "reason": "..."}
"""
)


# ---------------------------------------------------------------- code: the group and its structure
def load_groups(ids):
    entries = pick_entries(SET)
    store, papers = open_sources()
    groups = {}
    for gid in ids or list(entries):
        groups[gid] = read_group(store, papers, entries[gid])
    return groups


def structure(group):
    """Code's reading of the links: shape, and per non-picked Series the re-cite sign."""
    picked = group["id"]
    sup = group["superseries"]
    series = [s for s in group["series"]]
    held = {s["id"]: s for s in series if s["held"]}
    core = {picked} if picked != sup else set(held[picked]["superseries_of"]) & set(held)
    core_papers = {p for g in core for p in held.get(g, {}).get("pmids", [])}
    recite = {}
    for s in series:
        if s["id"] in core or s["id"] == sup or not s["held"]:
            continue
        own = [p for p in s["pmids"] if p not in core_papers]
        recite[s["id"]] = {"own_papers": own, "submitted": (s["fields"]["Submitted"] or [None])[0]}
    n_series = len([s for s in series if s["id"] != sup])
    return {
        "picked": picked,
        "superseries": sup,
        "core": sorted(core),
        "n_series": n_series,
        "n_papers": len(group["papers"]),
        "one_one": n_series == 1 and len(group["papers"]) <= 1 and sup is None,
        "recite_sign": {g: bool(v["own_papers"]) for g, v in recite.items()},
    }


# ---------------------------------------------------------------- code: quote grep-back
_TRANS = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-",
                        "—": "-", " ": " "})


def _norm(text):
    """Normalized text and, for each of its characters, the index in the original."""
    out, idx, prev_space = [], [], False
    for i, ch in enumerate(text.translate(_TRANS)):
        if ch.isspace():
            if prev_space or not out:
                continue
            out.append(" ")
            idx.append(i)
            prev_space = True
        else:
            out.append(ch.lower())
            idx.append(i)
            prev_space = False
    return "".join(out), idx


def sources(group):
    """Every place a quote may come from: (source id, field) -> original text."""
    src = {}
    for s in group["series"]:
        if not s["held"]:
            continue
        for label, values in s["fields"].items():
            src[(s["id"], label)] = "\n".join(values)
        lines = []
        for smp in s["samples"]:
            lines.append(" | ".join([smp["title"], smp["source"], "; ".join(smp["characteristics"])]))
        src[(s["id"], "Samples")] = "\n".join(lines)
    for p in group["papers"]:
        src[(p["id"], "Title")] = p["title"] or ""
        src[(p["id"], "Abstract")] = p["abstract"] or ""
    return src


def grep_back(quote, src):
    """Where a quote is found: in its named field, elsewhere, or nowhere; with char offsets."""
    if not quote or not quote.get("text"):
        return {"status": "none"}
    q, _ = _norm(quote["text"].strip().strip('"').strip())
    q = q.rstrip(".").strip()
    if len(q) < 8:
        return {"status": "too_short"}
    named = (str(quote.get("source", "")).strip(), str(quote.get("field", "")).strip())
    order = [named] + [k for k in src if k != named]
    for key in order:
        if key not in src:
            continue
        n, idx = _norm(src[key])
        at = n.find(q)
        if at >= 0:
            start, end = idx[at], idx[at + len(q) - 1] + 1
            status = "named_field" if key == named else (
                "other_field_same_source" if key[0] == named[0] else "other_source")
            return {"status": status, "source": key[0], "field": key[1], "start": start, "end": end}
    return {"status": "not_found"}


# ---------------------------------------------------------------- model calls
def _save(gid, name, obj):
    d = LOGS / gid
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(obj, indent=1, ensure_ascii=False))


def _load(gid, name):
    f = LOGS / gid / name
    return json.loads(f.read_text()) if f.exists() else None


def call_writer(meter, gid, text, thinking):
    name = f"writer{'-thinking' if thinking else ''}.json"
    if (got := _load(gid, name)) is not None:
        return got
    model = Model("deepseek", WRITER_NAME, thinking=thinking,
                  max_output_tokens=12000 if thinking else 3000)
    t = time.time()
    r = meter.chat(model, system=WRITER_SYSTEM, user=text, schema={"type": "object"},
                   tag=f"{gid}/writer")
    rec = {"seconds": round(time.time() - t, 2), "cost_usd": r.cost_usd, "first_cost_usd": r.first_cost_usd, "usage": r.usage,
           "cached": r.cached, "attempts": r.attempts, "text": r.text}
    try:
        rec["answer"] = json.loads(r.text)
    except ValueError as e:
        rec["answer"] = None
        rec["parse_error"] = str(e)
    _save(gid, name, rec)
    return rec


def jev_questions(group, st):
    picked = st["picked"]
    target = (f"the Series under SuperSeries {picked}" if st["superseries"] == picked
              else f"GEO Series {picked}")
    qs = {}
    for s in group["series"]:
        if s["id"] == st["superseries"] or s["id"] in st["core"] and st["superseries"] is None:
            continue
        qs[f"s_{s['id']}"] = {
            "type": "noul",
            "instructions": (
                f"Was GEO Series {s['id']} deposited as part of the same piece of research as "
                f"{target}: data made for it, rather than an older dataset that this research "
                "only re-uses or cites, a later dataset that re-uses its samples, or an unrelated "
                "study?"),
        }
    for p in group["papers"]:
        qs[f"p_{p['pmid']}"] = {
            "type": "noul",
            "instructions": (
                f"Is paper {p['id']} the paper that first reports the data of {target} (a "
                "preprint and its journal version both count), rather than a later paper that "
                "re-uses that data, a paper that reports another dataset of this group, or a "
                "paper linked by mistake?"),
        }
    return qs


def call_jev(meter, gid, text, qs):
    if (got := _load(gid, "jev.json")) is not None:
        return got
    if not qs:
        rec = {"seconds": 0.0, "cost_usd": 0.0, "answers": {}, "n_questions": 0}
        _save(gid, "jev.json", rec)
        return rec
    t = time.time()
    r = meter.jev(JEV, description=text, questions=qs, tag=f"{gid}/jev")
    rec = {"seconds": round(time.time() - t, 2), "cost_usd": r.cost_usd, "first_cost_usd": r.first_cost_usd, "usage": r.usage,
           "cached": r.cached, "attempts": r.attempts, "answers": r.answers,
           "n_questions": len(qs), "questions": qs}
    _save(gid, "jev.json", rec)
    return rec


def writer_entries(writer):
    ans = (writer or {}).get("answer") or {}
    return [(lst, e) for lst in ("series", "papers", "left_out") for e in ans.get(lst) or []
            if isinstance(e, dict) and e.get("id")]


def entail_request(gid, st, writer, src):
    """Step 3b: one Jev request asking, for each quote that grepped back, whether it backs its claim."""
    target = (f"the Series under SuperSeries {st['picked']}" if st["superseries"] == st["picked"]
              else f"GEO Series {st['picked']}")
    lines = [f"Checking whether quoted passages back claims about which GEO Series and papers "
             f"belong to one study: the research that deposited {target}."]
    qs = {}
    for i, (lst, e) in enumerate(writer_entries(writer)):
        g = grep_back(e.get("quote"), src)
        if g["status"] not in ("named_field", "other_field_same_source", "other_source"):
            continue
        text = src[(g["source"], g["field"])][g["start"]:g["end"]]
        rid = str(e["id"]).strip()
        if lst == "series":
            claim = f"{rid} holds data made by the same research as {target}"
        elif lst == "papers":
            claim = f"{rid} is the paper that reports the data of {target}"
        elif rid.startswith("GSE"):
            claim = (f"{rid} is a separate dataset from {target}: older data it re-uses, a later "
                     "dataset that re-uses it, or an unrelated study")
        else:
            claim = (f"{rid} is not the paper that reports the data of {target}: it is a later "
                     "re-use, the paper of another dataset, or a wrong link")
        qs[f"e_{i}"] = {
            "type": "noul",
            "instructions": (
                f"Passage from {g['source']}, {g['field']}: \"{text}\"\n"
                f"Claim: {claim}.\nDoes this passage give evidence for the claim? Yes if it "
                "states or clearly implies it; no if it only names or describes the record."),
        }
    return "\n".join(lines), qs


def call_entail(meter, gid, desc, qs):
    if (got := _load(gid, "entail.json")) is not None:
        return got
    if not qs:
        rec = {"seconds": 0.0, "cost_usd": 0.0, "answers": {}, "n_questions": 0}
    else:
        t = time.time()
        r = meter.jev(JEV, description=desc, questions=qs, tag=f"{gid}/entail")
        rec = {"seconds": round(time.time() - t, 2), "cost_usd": r.cost_usd, "first_cost_usd": r.first_cost_usd, "usage": r.usage,
               "answers": r.answers, "n_questions": len(qs), "cached": r.cached}
    _save(gid, "entail.json", rec)
    return rec


# ---------------------------------------------------------------- code: the bounds file
def placement_default(group, st):
    """Code's own drawing, used when the writer is skipped (and as a baseline)."""
    series = st["core"]
    out = [st["superseries"]] if st["superseries"] else []
    out += [s["id"] for s in group["series"] if s["id"] not in series and s["id"] not in out]
    papers = [p["id"] for p in group["papers"]]
    return {"series": series, "papers": papers, "left_out": out}


def p_yes(jev, rid):
    key = f"s_{rid}" if rid.startswith("GSE") else f"p_{rid.split(':')[1]}"
    a = (jev.get("answers") or {}).get(key)
    return None if a is None else float(a.get("noul"))


def build(gid, group, st, writer, jev, src, entail=None):
    """The bounds file, from the writer's answer (or code's default), quotes checked, p from Jev."""
    ans = (writer or {}).get("answer") if writer else None
    entries = {"series": [], "papers": [], "left_out": []}
    if ans:
        for lst in entries:
            for e in ans.get(lst) or []:
                if isinstance(e, dict) and e.get("id"):
                    entries[lst].append(e)
        reason = ans.get("reason") or ""
    else:
        d = placement_default(group, st)
        for lst in entries:
            for rid in d[lst]:
                how = ("the study is named by this Series" if rid in st["core"]
                       else "a SuperSeries only repeats its sub-Series" if rid == st["superseries"]
                       else "the only paper the group links" if rid.startswith("pmid:")
                       else "not the Series the study is named by")
                entries[lst].append({"id": rid, "quote": None, "how": how})
        reason = "One Series and the one paper its record links."
    passages, checks, claims = {}, [], {"series": [], "left_out": []}
    i = -1
    for lst in ("series", "papers", "left_out"):
        for e in entries[lst]:
            i += 1
            rid = str(e["id"]).strip()
            g = grep_back(e.get("quote"), src)
            ea = ((entail or {}).get("answers") or {}).get(f"e_{i}") if ans else None
            g["entail_p"] = None if ea is None else float(ea.get("noul"))
            checks.append({"id": rid, "list": lst, **g})
            if lst == "papers":
                continue
            claim = {"id": rid}
            how = (e.get("how") or "").strip() or "no reason given"
            if g["status"] in ("named_field", "other_field_same_source", "other_source"):
                n = len(passages) + 1
                full = src[(g["source"], g["field"])][g["start"]:g["end"]]
                if g["source"].startswith("pmid:"):
                    passages[n] = {"paper": g["source"], "section": g["field"], "quote": full,
                                   "_offsets": [g["start"], g["end"]]}
                else:
                    passages[n] = {"record": g["source"],
                                   "field": FIELD_GEO.get(g["field"], g["field"]), "quote": full}
                # Quoted only when the words grep back AND Jev says they back the claim.
                if g["entail_p"] is not None and g["entail_p"] >= 0.5:
                    claim["quoted"] = [n]
                else:
                    claim["inferred"] = how
                    claim["see"] = [n]
            else:
                claim["inferred"] = how
            py = p_yes(jev, rid)
            if rid in st["core"] and st["superseries"] is None:
                claim["p"] = None  # code's call: the study is named by this Series
            elif py is None:
                claim["p"] = None
            else:
                claim["p"] = round(py if lst == "series" else 1 - py, 4)
            claims[lst].append(claim)
    bounds = {
        "format": 1,
        "study": gid,
        "models": {"bounds": f"deepseek/{WRITER_NAME}" if ans else None,
                   "probabilities": "typesafe/jev-1.14.0"},
        "series": claims["series"],
        "papers": [str(e["id"]).strip() for e in entries["papers"]],
        "left_out": claims["left_out"],
        "reason": " ".join(reason.split()) or "no reason given",
        "passages": {n: {k: v for k, v in p.items() if not k.startswith("_")}
                     for n, p in passages.items()},
    }
    # Paper-claim probabilities live outside the format (papers carry no claim there).
    paper_p = {str(e["id"]).strip(): p_yes(jev, str(e["id"]).strip()) for e in entries["papers"]}
    problems = check_bounds(bounds)
    public = copy.deepcopy(bounds)
    for n, p in passages.items():
        if "paper" in p:  # never commit abstract text: offsets instead
            a, b = p["_offsets"]
            public["passages"][n]["quote"] = f"[{p['section'].lower()} chars {a}-{b}]"
    return bounds, public, problems, checks, paper_p


def placement(bounds):
    return ({c["id"] for c in bounds["series"]} | set(bounds["papers"]),
            {c["id"] for c in bounds["left_out"]})


def standard(gid):
    b = yaml.safe_load((SET / gid / "bounds.yaml").read_text())
    return {c["id"] for c in b["series"]} | set(b["papers"]), {c["id"] for c in b["left_out"]}


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--thinking", action="store_true", help="writer with thinking on (variant)")
    ap.add_argument("--all-writer", action="store_true", help="run the writer on 1+1 groups too")
    ap.add_argument("--ceiling", type=float, default=4.0)
    args = ap.parse_args()
    ids = [x for x in args.only.split(",") if x]
    t0 = time.time()
    groups = load_groups(ids)
    print(f"read {len(groups)} groups in {time.time() - t0:.1f}s", flush=True)
    sts = {g: structure(groups[g]) for g in groups}
    texts = {g: drafter_text(groups[g]) for g in groups}
    gw = Gateway(repo="seqforge")
    meter = gw.meter("prototype bounding calls (#617)", ceiling_usd=args.ceiling)
    jobs = []
    for g in groups:
        qs = jev_questions(groups[g], sts[g])
        jobs.append(("jev", g, qs))
        if args.all_writer or not sts[g]["one_one"] or args.thinking:
            jobs.append(("writer", g, None))
    res = {}

    def do(job):
        kind, g, qs = job
        if kind == "jev":
            r = call_jev(meter, g, texts[g], qs)
        else:
            r = call_writer(meter, g, texts[g], args.thinking)
        with _lock:
            res[(kind, g)] = r
            print(f"{kind:6s} {g:10s} {r.get('seconds')}s ${r.get('cost_usd', 0):.5f}", flush=True)

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(do, jobs))
    if args.thinking:
        print(f"spent ${meter.spent_usd:.4f}")
        return
    srcs = {g: sources(groups[g]) for g in groups}
    ent = {}

    def do_entail(g):
        desc, qs = entail_request(g, sts[g], res[("writer", g)], srcs[g])
        r = call_entail(meter, g, desc, qs)
        with _lock:
            ent[g] = r
            print(f"entail {g:10s} {r.get('seconds')}s ${r.get('cost_usd', 0):.5f}", flush=True)

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(do_entail, [g for g in groups if ("writer", g) in res]))
    OUT.mkdir(exist_ok=True)
    rows = []
    for g in groups:
        st, src = sts[g], srcs[g]
        jev = res[("jev", g)]
        writer = res.get(("writer", g))
        use_writer = writer if not st["one_one"] else None
        bounds, public, problems, checks, paper_p = build(
            g, groups[g], st, use_writer, jev, src, ent.get(g) if use_writer else None)
        (LOGS / g / "bounds.full.yaml").write_text(yaml.safe_dump(bounds, sort_keys=False))
        if problems:  # a failed format check is never written as a bounds file: refused
            (OUT / f"{g}.refused.yaml").write_text(yaml.safe_dump(
                {"study": g, "refused": "the format check rejected the answer",
                 "problems": problems}, sort_keys=False, width=100))
        else:
            (OUT / f"{g}.bounds.yaml").write_text(
                yaml.safe_dump(public, sort_keys=False, width=100))
        alt = None
        if writer is not None and st["one_one"]:  # what the writer would have drawn on a 1+1
            ab, _, ap_, achecks, _ = build(g, groups[g], st, writer, jev, src, ent.get(g))
            alt = {"placement": [sorted(x) for x in placement(ab)], "problems": ap_,
                   "checks": achecks}
        inside, out = placement(bounds)
        s_in, s_out = standard(g)
        every = {s["id"] for s in groups[g]["series"]} | {p["id"] for p in groups[g]["papers"]}
        jev_in = {r for r in every if r != st["superseries"]
                  and ((r in st["core"] and st["superseries"] is None) or (p_yes(jev, r) or 0) > 0.5)}
        e = ent.get(g) or {}
        rows.append({
            "id": g, "structure": st, "writer_used": use_writer is not None,
            "writer": {k: writer.get(k) for k in ("seconds", "cost_usd", "first_cost_usd", "usage", "cached")} if writer else None,
            "entail": {k: e.get(k) for k in ("seconds", "cost_usd", "first_cost_usd", "usage", "n_questions", "cached")},
            "jev": {k: jev.get(k) for k in ("seconds", "cost_usd", "first_cost_usd", "usage", "n_questions", "cached")},
            "jev_p_yes": {r: p_yes(jev, r) for r in sorted(inside | out)},
            "problems": problems, "quote_checks": checks, "paper_p": paper_p,
            "placement": {"in": sorted(inside), "out": sorted(out)},
            "standard": {"in": sorted(s_in), "out": sorted(s_out)},
            "differs": sorted((inside ^ s_in)),
            "jev_only_in": sorted(jev_in), "jev_only_differs": sorted(jev_in ^ s_in),
            "writer_on_one_one": alt,
        })
    (HERE / "results.json").write_text(json.dumps(rows, indent=1))
    print(f"spent ${meter.spent_usd:.4f}; {len(rows)} studies; {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
