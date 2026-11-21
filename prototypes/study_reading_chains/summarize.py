# PROTOTYPE, throwaway (issue #629): the tables of RESULTS.md, from out/<variant>/*.result.json.
#   python summarize.py > _logs/summary.md      (per-study tables go to per_study.md)
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import yaml  # noqa: E402

import material as M  # noqa: E402

OUT = HERE / "out"
SET = M.SET
VARIANTS = ["code", "flash", "flash_full", "flash_think", "flash_think_full"]
NAME = {"code": "code lists", "flash": "Flash, abstracts", "flash_full": "Flash, full text",
        "flash_think": "Flash thinking, abstracts", "flash_think_full": "Flash thinking, full text"}
EXAMPLES = ["GSE100021", "GSE112068", "GSE235532", "GSE117724", "GSE241718"]


def load(v):
    out = {}
    for f in sorted((OUT / v).glob("*.result.json")):
        r = json.loads(f.read_text())
        out[r["study"]] = r
    return out


R = {v: load(v) for v in VARIANTS}
IDS = M.study_ids()
MAT = {g: M.load(g) for g in IDS}


def p(x):
    print(x)


def cost(r, model=None):
    return sum((e.get("cost") or 0) for e in r.get("ledger", []) if model in (None, e["model"]))


def secs(r, model):
    return sum((e.get("seconds") or 0) for e in r.get("ledger", []) if e["model"] == model)


def med(xs):
    return statistics.median(xs) if xs else 0


def table(head, rows):
    p("| " + " | ".join(head) + " |")
    p("|" + "---|" * len(head))
    for r in rows:
        p("| " + " | ".join(str(x) for x in r) + " |")
    p("")


def hist(ps, edges=(0, .1, .2, .3, .4, .5, .6, .7, .8, .9, .95, .99, 1.0001)):
    c = [0] * (len(edges) - 1)
    for x in ps:
        for i in range(len(edges) - 1):
            if edges[i] <= x < edges[i + 1]:
                c[i] += 1
                break
    return c


# ------------------------------------------------------------------ endings, cost, time
p("## Endings, cost and time per chain\n")
rows = []
for v in VARIANTS:
    rs = R[v]
    e = Counter(r["ending"] for r in rs.values())
    c_ds = [cost(r, "deepseek") for r in rs.values()]
    c_j = [cost(r, "jev") for r in rs.values()]
    t_w = [secs(r, "deepseek") for r in rs.values()]
    t_s = [secs(r, "jev") for r in rs.values()]
    rows.append([NAME[v], len(rs), e["settled"], e["unsettled"], e["refused"],
                 f"${sum(c_ds):.3f}", f"${sum(c_j):.3f}",
                 f"${med([a + b for a, b in zip(c_ds, c_j)]):.4f} / ${max([a + b for a, b in zip(c_ds, c_j)] or [0]):.4f}",
                 f"{med(t_w):.1f} / {max(t_w or [0]):.1f}",
                 f"{med(t_s):.1f} / {max(t_s or [0]):.1f}"])
table(["Chain", "Studies", "Settled", "Unsettled", "Refused", "DeepSeek $ (all)", "Jev $ (all)",
       "$ per study, median / max", "Writer s, median / max", "Jev s summed over requests, median / max"],
      rows)

p("Refusal reasons:\n")
for v in VARIANTS:
    for g, r in sorted(R[v].items()):
        if r["ending"] == "refused":
            probs = r.get("problems") or []
            why = r.get("reason") or ""
            p(f"- {NAME[v]}, {g}: {why}; " + "; ".join(f"`{x['field']}`: {x['reason']}" for x in probs[:3])
              + (f" (+{len(probs) - 3} more)" if len(probs) > 3 else ""))
p("")

rows = []
for v in VARIANTS:
    c = Counter()
    for r in R[v].values():
        if r["ending"] != "refused":
            continue
        probs = r.get("problems") or []
        if "not a JSON" in (r.get("reason") or ""):
            c["writer answer not JSON"] += 1
        elif "crash" in (r.get("reason") or ""):
            c["crash"] += 1
        elif probs and all("no sample is at the level" in x["reason"] for x in probs):
            c["check: only levels no sample is at"] += 1
        else:
            c["check: other"] += 1
    rows.append([NAME[v], c["writer answer not JSON"], c["check: only levels no sample is at"],
                 c["check: other"], c["crash"]])
table(["Chain", "Writer's answer not JSON", "Format check: only a level no sample is at",
       "Format check: other", "Crash"], rows)

p("Unsettled reasons (as run: the two asks disagree on a factor or on a sample's level):\n")
for v in VARIANTS:
    c = Counter()
    for r in R[v].values():
        for x in r.get("unsettled_reasons") or []:
            c[re.sub(r"factor \S+:", "factor X:", re.sub(r"^\d+ ", "N ", x))] += 1
    p(f"- {NAME[v]}: " + "; ".join(f"{k} ({n})" for k, n in c.most_common()))
p("")

# Alternative ending rules
p("## The same readings under other unsettled rules (count of studies, of those not refused)\n")
rows = []
for v in VARIANTS:
    ok = [r for r in R[v].values() if r["ending"] != "refused"]
    asrun = sum(1 for r in ok if r.get("unsettled_reasons"))
    any_dis = sum(1 for r in ok if r.get("disagree"))
    lvl_p = sum(1 for r in ok if any((c["p"] is not None and c["p"] < 0.8) for c in r.get("claims", [])
                                     if c["label"] in (r.get("factor_out") or {}) and not c["shared"]
                                     or c["label"].startswith("factor:") and c["p"] is not None and c["p"] < 0.8))
    fac_p = sum(1 for r in ok if any((f.get("p_designed") or 0) < 0.9 and f.get("kept") for f in r.get("factors", [])))
    any_p = sum(1 for r in ok if any((c["p"] is not None and c["p"] < 0.5) for c in r.get("claims", [])))
    rows.append([NAME[v], len(ok), asrun, any_dis, lvl_p, fac_p, any_p])
table(["Chain", "Not refused", "As run: asks disagree on a factor or a level",
       "A per-sample pick on any label changes with options reversed", "A factor or level claim with p < 0.8",
       "A kept factor with p(varied) < 0.9", "Any claim with p < 0.5"], rows)

# ------------------------------------------------------------------ ask twice
p("## Asking twice\n")
rows = []
for v in VARIANTS:
    n_q = n_d = n_l = n_ld = 0
    fd = bd = nf = 0
    for r in R[v].values():
        if r["ending"] == "refused" and not r.get("values"):
            continue
        vals = r.get("values") or {}
        for g, labs in vals.items():
            n_q += len(labs)
        dis = r.get("disagree") or []
        n_d += len(dis)
        n_ld += sum(1 for d in dis if d[1].startswith("lvl"))
        for f in r.get("factors", []):
            nf += 1
            fd += bool(f.get("designed_disagree"))
            bd += bool(f.get("baseline_disagree"))
    rows.append([NAME[v], nf, fd, bd, n_q, n_d, n_ld])
table(["Chain", "Factors listed", "Varied on purpose: plain and negated asks contradict",
       "Baseline differs with options reversed", "Per-sample claims", "Per-sample picks that change with options reversed",
       "of which a factor level"], rows)

# ------------------------------------------------------------------ probabilities
p("## How the probabilities spread (every claim of every written or refused reading)\n")
edges = ["<0.1", "0.1", "0.2", "0.3", "0.4", "0.5", "0.6", "0.7", "0.8", "0.9", "0.95", "0.99+"]
rows = []
for v in VARIANTS:
    for mark in ("quoted", "inferred", "unknown", "na"):
        ps = [c["p"] for r in R[v].values() for c in r.get("claims", []) if c["mark"] == mark and c["p"] is not None]
        rows.append([NAME[v], mark, len(ps)] + hist(ps))
    nn = sum(1 for r in R[v].values() for c in r.get("claims", []) if c["p"] is None)
    rows.append([NAME[v], "p: null (code's own call)", nn] + [""] * 12)
table(["Chain", "Backing", "Claims"] + edges, rows)

p("Jev's p(varied on purpose) for each listed factor:\n")
rows = []
for v in VARIANTS:
    ps = [f["p_designed"] for r in R[v].values() for f in r.get("factors", []) if f.get("p_designed") is not None]
    rows.append([NAME[v], len(ps)] + hist(ps))
table(["Chain", "Factors"] + edges, rows)

# ------------------------------------------------------------------ quotes
p("## Quotes: found by code, backed by Jev\n")
rows = []
for v in VARIANTS:
    st = Counter()
    backs = Counter()
    by_src = defaultdict(lambda: [0, 0, 0])
    for r in R[v].values():
        for q in r.get("quotes", []):
            s = q["status"]
            st[s] += 1
            if s in ("named", "same_source_other_field", "other_record", "elsewhere"):
                src = q.get("source") or q.get("record") or ""
                kind = ("paper: " + (q.get("field") if q.get("field") in ("abstract", "title") else "full text")) \
                    if str(src).startswith("pmid:") else "record"
                by_src[kind][0] += 1
                if (q.get("backs_p") or 0) >= 0.5:
                    by_src[kind][1] += 1
                    backs["yes"] += 1
            if s == "loose_only":
                by_src["loose only (" + ("paper" if q.get("paper") else "record") + ")"][2] += 1
    given = sum(n for k, n in st.items() if k != "none")
    found = st["named"] + st["same_source_other_field"] + st["other_record"] + st["elsewhere"]
    rows.append([NAME[v], given, st["named"], st["same_source_other_field"] + st["other_record"] + st["elsewhere"],
                 st["loose_only"], st["not_found"], found, backs["yes"],
                 "; ".join(f"{k} {a[0]} found / {a[1]} backed" if not k.startswith("loose") else f"{k} {a[2]}"
                           for k, a in sorted(by_src.items()))])
table(["Chain", "Quotes given", "Found in the named field", "Found elsewhere", "Only loosely (case, hyphen)",
       "Not found", "Found exactly", "Backed (Jev p >= 0.5)", "By source"], rows)

# ------------------------------------------------------------------ ontology
p("## Ontology picks\n")
rows = []
for v in VARIANTS:
    c = Counter()
    for r in R[v].values():
        for o in r.get("ontology", []):
            c["asked"] += 1
            if not o["candidates"]:
                c["no candidates"] += 1
                continue
            has_exact = any(x["exact"] for x in o["candidates"])
            c["exact hit" if has_exact else "plain hits only"] += 1
            pk = o.get("pick")
            if pk == "none":
                c["none (exact hit)" if has_exact else "none (plain only)"] += 1
            elif pk:
                c["picked (exact hit)" if has_exact else "picked (plain only)"] += 1
                c["picked p>=0.8"] += (o.get("p") or 0) >= 0.8
                if has_exact and not any(x["id"] == pk and x["exact"] for x in o["candidates"]):
                    c["picked a non-exact while an exact existed"] += 1
            if "probe_pick" in o:
                c["probe asked"] += 1
                c["probe: none"] += o["probe_pick"] == "none"
    rows.append([NAME[v], c["asked"], c["no candidates"], c["exact hit"], c["picked (exact hit)"], c["none (exact hit)"],
                 c["picked a non-exact while an exact existed"], c["plain hits only"], c["picked (plain only)"],
                 c["none (plain only)"], c["probe asked"], c["probe: none"]])
table(["Chain", "Label texts asked", "No candidates", "An exact hit among them", "Picked a term", "Picked none",
       "Picked a non-exact term over an exact one", "Plain hits only", "Picked a term", "Picked none",
       "Probe: asked again without the pick", "Probe: picked none"], rows)

p("Probe picks that were not none (the pick removed, Jev chose another candidate), thinking + full text chain:\n")
seen = set()
for r in R["flash_think_full"].values():
    for o in r.get("ontology", []):
        if "probe_pick" in o and o["probe_pick"] != "none":
            lab = {x["id"]: x["label"] for x in o["candidates"]}
            key = (o["label"], o["text"])
            if key in seen:
                continue
            seen.add(key)
            p(f"- {o['label']} \"{o['text'][:60]}\": first {o['pick']} ({lab.get(o['pick'])}), then "
              f"{o['probe_pick']} ({lab.get(o['probe_pick'])}) at p {o.get('probe_p')}")
p("")

by_vocab = defaultdict(Counter)
for v in VARIANTS:
    for r in R[v].values():
        for o in r.get("ontology", []):
            voc = ",".join(o["vocab"]) if isinstance(o["vocab"], list) else o["vocab"]
            by_vocab[voc]["asked"] += 1
            by_vocab[voc]["term"] += bool(o.get("pick") and o["pick"] != "none" and (o.get("p") or 0) >= 0.8)
            by_vocab[voc]["none"] += o.get("pick") == "none"
            by_vocab[voc]["nocand"] += not o["candidates"]
p("By vocabulary, all chains: " + "; ".join(f"{k}: {c['asked']} asked, {c['term']} term written, {c['none']} none, "
                                             f"{c['nocand']} no candidates" for k, c in sorted(by_vocab.items())))
p("")

# ------------------------------------------------------------------ chains differ
p("## Where the chains differ from each other\n")


def norm(t):
    return re.sub(r"[^a-z0-9]+", " ", str(t).lower()).strip()


labels18 = ["assay", "target", "control_type", "species", "sex", "age", "development_stage", "tissue",
            "cell_type", "cell_line", "disease", "genotype", "strain", "treatment", "dose", "duration"]
rows = []
for v in VARIANTS:
    rs = R[v]
    nf = [len(r.get("factor_out") or {}) for r in rs.values() if r["ending"] != "refused"]
    unk = Counter()
    tot = 0
    for r in rs.values():
        for g, labs in (r.get("values") or {}).items():
            for lb in labels18:
                if lb in labs:
                    tot += 1
                    unk[labs[lb][0]] += labs[lb][0] in ("<unknown>", "<na>")
    extra = [len([lb for lb in next(iter((r.get("values") or {"": {}}).values()), {}) if lb not in labels18
                  and lb not in ("biosample", "donor") and lb not in (r.get("factor_out") or {})])
             for r in rs.values() if r.get("values")]
    rows.append([NAME[v], f"{sum(nf)} ({Counter(nf).most_common(3)})", unk["<unknown>"], unk["<na>"], tot,
                 sum(extra)])
table(["Chain", "Factors written (studies by count)", "Sample-label claims unknown", "na", "of",
       "Study-own labels written"], rows)

p("Pairwise: share of sample-label values (16 labels, text compared loosely) that differ, and studies whose "
  "factor labels differ:\n")
rows = []
for i, a in enumerate(VARIANTS):
    for b in VARIANTS[i + 1:]:
        same = diff = fdiff = both = 0
        for g in IDS:
            ra, rb = R[a].get(g), R[b].get(g)
            if not ra or not rb or not ra.get("values") or not rb.get("values"):
                continue
            both += 1
            if set((ra.get("factor_out") or {})) != set((rb.get("factor_out") or {})):
                fdiff += 1
            for s, labs in ra["values"].items():
                for lb in labels18:
                    x, y = labs.get(lb, ("?",))[0], rb["values"].get(s, {}).get(lb, ("?",))[0]
                    if norm(x) == norm(y):
                        same += 1
                    else:
                        diff += 1
        rows.append([NAME[a], NAME[b], both, f"{diff}/{same + diff} ({100 * diff / max(1, same + diff):.0f}%)",
                     fdiff])
table(["Chain A", "Chain B", "Studies both wrote", "Sample-label values that differ", "Studies whose factors differ"],
      rows)

p("Per label, share of sample values that differ between Flash thinking with full text and the code lists, and "
  "between the two Flash chains with full text (thinking off vs on):\n")
rows = []
for lb in labels18:
    out = []
    for a, b in (("flash_think_full", "code"), ("flash_full", "flash_think_full"), ("flash", "flash_full")):
        same = diff = 0
        for g in IDS:
            ra, rb = R[a].get(g), R[b].get(g)
            if not ra or not rb or not ra.get("values") or not rb.get("values"):
                continue
            for s, labs in ra["values"].items():
                x, y = labs.get(lb, ("?",))[0], rb["values"].get(s, {}).get(lb, ("?",))[0]
                same += norm(x) == norm(y)
                diff += norm(x) != norm(y)
        out.append(f"{100 * diff / max(1, same + diff):.0f}%")
    rows.append([lb] + out)
table(["Label", "thinking+full vs code", "Flash full: thinking off vs on", "Flash: abstracts vs full text"], rows)

p("Per label, how a sample's claim changes from chain A to chain B: unknown in A and a value in B "
  "(gained), a value in A and unknown in B (lost), a value in both with different words (reworded or "
  "different), na in one and a value in the other:\n")
rows = []
for a, b in (("flash", "flash_full"), ("flash_think", "flash_think_full"), ("code", "flash_think"),
             ("flash", "flash_think")):
    for lb in labels18:
        c = Counter()
        for g in IDS:
            ra, rb = R[a].get(g), R[b].get(g)
            if not ra or not rb or not ra.get("values") or not rb.get("values"):
                continue
            for s_, labs in ra["values"].items():
                x = labs.get(lb, ("?",))[0]
                y = rb["values"].get(s_, {}).get(lb, ("?",))[0]
                xv, yv = x not in ("<unknown>", "<na>"), y not in ("<unknown>", "<na>")
                if x == "<unknown>" and yv:
                    c["gained"] += 1
                elif xv and y == "<unknown>":
                    c["lost"] += 1
                elif xv and yv and norm(x) != norm(y):
                    c["differ"] += 1
                elif (x == "<na>") != (y == "<na>") and (xv or yv):
                    c["na vs value"] += 1
                c["n"] += 1
        if c["gained"] or c["lost"] or c["differ"] or c["na vs value"]:
            rows.append([f"{NAME[a]} -> {NAME[b]}", lb, c["n"], c["gained"], c["lost"], c["differ"],
                         c["na vs value"]])
table(["A -> B", "Label", "Sample claims compared", "Gained a value", "Lost a value",
       "Both valued, words differ", "na in one, value in other"], rows)

# ------------------------------------------------------------------ vs the five examples
p("## Against the five example readings (differences only; the examples are not a gold standard)\n")
for g in EXAMPLES:
    ex = yaml.safe_load((SET / g / "reading.yaml").read_text())
    rows_ex = {s: {**ex["shared"], **own} for s, own in ex["samples"].items()}
    exf = {k: v["levels"] for k, v in ex["factors"].items()}
    p(f"**{g}**: example factors " + ", ".join(f"{k} {v}" for k, v in exf.items()))
    rows = []
    for v in VARIANTS:
        r = R[v].get(g)
        if not r:
            continue
        fo = r.get("factor_out") or {}
        if not r.get("values"):
            rows.append([NAME[v], r["ending"], "-", "-", "-"])
            continue
        diff_labels = Counter()
        n = 0
        for s, labs in r["values"].items():
            for lb in labels18:
                e = rows_ex.get(s, {}).get(lb)
                if e is None:
                    continue
                et = e.get("text") or ("<na>" if "na" in e else "<unknown>" if "unknown" in e else e.get("id"))
                n += 1
                if norm(et) != norm(labs.get(lb, ("?",))[0]):
                    diff_labels[lb] += 1
        rows.append([NAME[v], r["ending"], ", ".join(f"{k} {x['levels']}" for k, x in fo.items()) or "none",
                     f"{sum(diff_labels.values())}/{n}",
                     ", ".join(f"{k} {c}" for k, c in diff_labels.most_common())])
    table(["Chain", "Ending", "Factors written", "Sample-label texts that differ", "Where (label, samples)"], rows)

# ------------------------------------------------------------------ format gaps
p("## Format gaps over the 50\n")


def code_gaps(g):
    m = MAT[g]
    out = set()
    sers = {s["id"] for s in m["series"] if s["held"] and not s.get("superseries_of")}
    in_ser = Counter(x for s in m["samples"] for x in s["fields"].get("Sample_series_id", []) if x in sers)
    if len(in_ser) >= 2:
        out.add("series_membership")
    inst = {tuple(s["fields"].get("Sample_instrument_model", [])) for s in m["samples"]}
    if len(inst) >= 2:
        out.add("sequencer")
    if any(p_["full_kind"] == "pdf" for p_ in m["papers"]):
        out.add("pdf_full_text")
    strat = {x for s in m["samples"] for x in s["fields"].get("Sample_library_strategy", [])}
    out.add("term_missing: assay")
    if strat & {"ChIP-Seq"}:
        out.add("term_missing: target")
    tax = {x for s in m["samples"] for x in s["fields"].get("Sample_taxid_ch1", [])}
    if tax & {"10116"}:
        out.add("term_missing: rat stage")
    if tax & {"7227"}:
        out.add("term_missing: fly stage (FBdv is in OLS4 but the format names no fly list)")
    if any(re.search(r"batch|lane|run", ln.split(":")[0], re.I) for s in m["samples"]
           for ln in s["fields"].get("Sample_characteristics_ch1", [])):
        out.add("batch")
    return out


def reading_gaps(r):
    out = set()
    if not r or not r.get("values"):
        return out
    vals = r["values"]
    fo = r.get("factor_out") or {}
    for lb in fo:
        if any(labs.get(lb, (None, None))[1] is None for labs in vals.values()):
            out.add("factor_over_part")
    labs = list(fo)
    if len(labs) >= 2:
        combos = Counter(tuple(vals[s].get(lb, (None, None))[1] for lb in labs) for s in vals)
        full = 1
        for lb in labs:
            full *= len(fo[lb]["levels"])
        if len(combos) < full:
            out.add("crossed_not_fully")
    n = len(vals)
    for lb in labels18:
        texts = Counter(labs_.get(lb, ("?",))[0] for labs_ in vals.values())
        if len(texts) > 1 and any(c >= 2 for c in texts.values()) and lb not in fo:
            out.add("group_smaller_than_study")
    try:
        doc = yaml.safe_load((OUT / r["variant"] / f"{r['study']}.reading.yaml").read_text())
    except FileNotFoundError:
        doc = None
    if doc:
        notes = Counter()
        for s, row in doc["samples"].items():
            for lb, c in row.items():
                for k in ("inferred", "unknown", "na"):
                    if k in c:
                        notes[(lb, c[k])] += 1
        if any(n_ >= 2 for n_ in notes.values()):
            out.add("text_said_once")
        if any("value" in c and "level" in c for row in doc["samples"].values() for c in row.values()) or \
                any("value" in c and "level" in c for c in doc["shared"].values()):
            out.add("two_beliefs_one_claim (a level and an exact value)")
        bios = Counter(row["biosample"]["id"] for row in doc["samples"].values() if "biosample" in row)
        if any(c >= 2 for c in bios.values()):
            out.add("two_beliefs_one_claim (shared material and control tie)")
    if r.get("donor_kind", [None])[0] == "pool":
        out.add("pooled_material")
    return out


code_c, read_c = Counter(), Counter()
for g in IDS:
    code_c.update(code_gaps(g))
    read_c.update(reading_gaps(R["flash_think_full"].get(g) or R["flash_think"].get(g)))
probe = defaultdict(int)
for g in IDS:
    r = R["flash_think"].get(g) or R["code"].get(g)
    for k, x in (r or {}).get("gap_probes", {}).items():
        probe[k] += (x or 0) >= 0.5
writer = defaultdict(Counter)
for v in ("flash", "flash_full", "flash_think", "flash_think_full"):
    for g, r in R[v].items():
        kinds = {str(x.get("kind")) for x in (r.get("lists_summary") or {}).get("writer_gaps") or []}
        for k in kinds:
            writer[v][k] += 1
p("Counted by code from the records and the paper files (studies of 50):\n")
table(["Gap", "Studies"], sorted(code_c.items(), key=lambda x: -x[1]))
p("Counted by code from the written readings (thinking + full text where held, else thinking + abstracts; "
  "studies with a reading):\n")
table(["Gap", "Studies"], sorted(read_c.items(), key=lambda x: -x[1]))
p("Jev's yes (p >= 0.5) to a yes/no probe over the records and abstracts (studies of 50):\n")
table(["Probe", "Studies"], sorted(probe.items(), key=lambda x: -x[1]))
p("The writer's own format_gaps list (studies naming each kind):\n")
kinds = sorted({k for v in writer for k in writer[v]})
table(["Kind"] + [NAME[v] for v in writer], [[k] + [writer[v][k] for v in writer] for k in kinds])

# ------------------------------------------------------------------ assays
p("## By assay\n")
rows = []
by = defaultdict(list)
for g in IDS:
    strat = sorted({x for s in MAT[g]["samples"] for x in s["fields"].get("Sample_library_strategy", [])})
    by[" + ".join(strat)].append(g)
for a, gs in sorted(by.items(), key=lambda x: -len(x[1])):
    row = [a, len(gs)]
    for v in VARIANTS:
        e = Counter(R[v][g]["ending"] for g in gs if g in R[v])
        row.append(f"{e['settled']}/{e['unsettled']}/{e['refused']}")
    rows.append(row)
table(["Assays in the study", "Studies"] + [f"{NAME[v]} (settled/unsettled/refused)" for v in VARIANTS], rows)

p("Assay text written per study for the assays other than plain RNA-Seq (each chain):\n")
for g in IDS:
    strat = {x for s in MAT[g]["samples"] for x in s["fields"].get("Sample_library_strategy", [])}
    if strat == {"RNA-Seq"}:
        continue
    cells = []
    for v in VARIANTS:
        r = R[v].get(g)
        if not r or not r.get("values"):
            cells.append(f"{NAME[v]}: {r['ending'] if r else '-'}")
            continue
        a = Counter(labs.get("assay", ("?",))[0] for labs in r["values"].values())
        cells.append(f"{NAME[v]}: " + ", ".join(f"{k} x{n}" for k, n in a.items()))
    p(f"- {g} ({', '.join(sorted(strat))}): " + "; ".join(cells))
p("")

p("Pull-down studies: target and control libraries as written (thinking + full text, else thinking):\n")
for g in IDS:
    strat = {x for s in MAT[g]["samples"] for x in s["fields"].get("Sample_library_strategy", [])}
    if not strat & {"ChIP-Seq"}:
        continue
    for v in VARIANTS:
        r = R[v].get(g)
        if not r or not r.get("values"):
            p(f"- {g} {NAME[v]}: no reading ({r['ending'] if r else 'missing'})")
            continue
        tg = Counter(labs.get("target", ("?",))[0] for labs in r["values"].values())
        ct = Counter(labs.get("control_type", ("?",))[0] for labs in r["values"].values())
        bio = len({labs.get("biosample", ("?",))[0] for labs in r["values"].values()})
        p(f"- {g} {NAME[v]}: target {dict(tg)}; control_type {dict(ct)}; factors {list(r.get('factor_out') or {})}; "
          f"{bio} biosample ids for {len(r['values'])} samples")
p("")


# ------------------------------------------------------------------ per-study table (separate file)
def per_study():
    lines = ["# Cost and time per study per chain\n",
             "Generated by `summarize.py`. Cost is the gateway's metered first cost (Jev + DeepSeek). "
             "Writer s is the DeepSeek call; Jev s is the summed seconds of the study's Jev requests "
             "(per-sample requests ran 8 at a time, so wall time is lower). Ending: S settled, U unsettled, "
             "R refused.\n",
             "| Study | Samples | " + " | ".join(NAME[v] for v in VARIANTS) + " |",
             "|---|---|" + "---|" * len(VARIANTS)]
    for g in IDS:
        cells = []
        for v in VARIANTS:
            r = R[v].get(g)
            if not r:
                cells.append("not run (no full text)" if v.endswith("_full") else "-")
                continue
            cells.append(f"{r['ending'][0].upper()} ${cost(r):.4f}, {secs(r, 'deepseek'):.0f}+{secs(r, 'jev'):.0f} s")
        lines.append(f"| {g} | {len(MAT[g]['samples'])} | " + " | ".join(cells) + " |")
    (HERE / "per_study.md").write_text("\n".join(lines) + "\n")


per_study()
