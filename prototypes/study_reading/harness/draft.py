# PROTOTYPE, throwaway: draft the open lists (factors + levels with quotes, donors, biosample
# sharing, assay kinds, constant attributes). Code drafts them from GEO's typed characteristics when
# those vary across samples; otherwise DeepSeek writes them. `--writer always` runs DeepSeek too, for
# comparison. Every quote must grep back to the fetched text.
import re
import sys

from common import deepseek, load, save
from fetch import corpus


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip().lower()


def code_draft(study):
    keys = {}
    for s in study["samples"]:
        for k, v in s["characteristics"]:
            keys.setdefault(k, {}).setdefault(v, []).append(s["gsm"])
    varying, constant = [], []
    for k, vals in keys.items():
        entry = {"name": k, "levels": [{"value": v, "quote": f"{k}: {v}" if k else v} for v in vals]}
        (varying if len(vals) > 1 else constant).append(entry)
    return {"source": "code", "candidate_keys": varying, "constant": constant}


def study_text(study, with_samples=True, with_paper=False):
    lines = []
    if with_paper:
        from common import run_dir
        txt = "\n".join((run_dir(study["gse"]) / "raw" / f).read_text() for f in study.get("paper_files", [])
                        if (run_dir(study["gse"]) / "raw" / f).exists())
        if txt:
            lines.append(txt)
    for acc, t in study["series_text"].items():
        lines.append(f"Series {acc}: {t['title']}")
        lines += [f"Summary: {x}" for x in t["summary"]]
        lines += [f"Overall design: {x}" for x in t["overall_design"]]
    protos = {}
    for s in study["samples"]:
        for k, v in s["protocols"].items():
            if v:
                protos.setdefault(k, set()).add(v)
    for k, vs in protos.items():
        for v in sorted(vs):
            lines.append(f"{k} protocol: {v}")
    if with_samples:
        for s in study["samples"]:
            ch = "; ".join(f"{k}: {v}" if k else v for k, v in s["characteristics"])
            lines.append(
                f"{s['gsm']} | title: {s['title']} | source: {s['source']} | organism: {s['organism']}"
                f" | characteristics: {ch} | library strategy: {s['library_strategy']}"
                f" | molecule: {s['molecule']}"
            )
    return "\n".join(lines)


SYSTEM = """You read a GEO study and write the OPEN LISTS a later step will use to label every sample.
Return JSON only. Every "quote" must be copied VERBATIM (exact characters) from the text you were given,
short (2-12 words), and must show the value. Do not label individual samples, except in biosample_sharing.

{
 "factors": [            // what the investigators varied ON PURPOSE to compare (genotype, treatment, time, age, dose...)
   {"name": "...", "ordered": false,   // ordered=true for a time, age or dose series
    "levels": [{"value": "...", "quote": "..."}]}
 ],
 "targets": [{"name": "...", "quote": "..."}],   // antibody targets of ChIP-seq, CUT&RUN or CUT&Tag (a histone mark, a protein); [] if none
 "controls": [{"kind": "input|IgG|non-targeting|other", "quote": "..."}],  // background-control LIBRARIES (ChIP input, IgG pull-down...); [] if none
 "donors": [{"id": "...", "quote": "..."}],   // every individual person, animal or cell line the material came from, by the id
                                              // the text uses (look in sample titles as well as characteristics); [] if no text tells them apart.
                                              // A cell line is a donor: when each genotype or condition comes from one line or patient, list each line
 "biosample_sharing": [{"gsms": ["GSM..."], "quote": "..."}],  // ONLY groups of several libraries made from ONE material prep
                                                                // (one tissue, nuclei or cell preparation split into several libraries); else []
 "assays": [{"kind": "RNA-seq|ChIP-seq|ATAC-seq|CUT&RUN|CUT&Tag|scRNA-seq|CRISPR screen|CLIP|Ribo-seq|other", "quote": "..."}],
 "attributes": [{"name": "species|tissue|cell type|sex|age|strain|...", "value": "...", "quote": "..."}]  // descriptive, not compared; one entry per distinct value.
                     // If one field packs several attributes (for example "condition: M, 45y" holds sex and age), list each attribute on its own
}
Do not list a donor, batch, replicate number, assay, antibody target or input/control status as a factor."""


def deepseek_draft(gse, study):
    out, meta = deepseek(gse, SYSTEM, study_text(study, with_paper=True), tag="writer")
    out["source"] = "deepseek"
    out["_meta"] = meta
    return out


def check_quotes(gse, lists):
    # the raw SOFT plus the rendered text the writer saw ("library strategy: RNA-Seq" is the harness's own label)
    text = norm(corpus(gse)) + "\n" + norm(study_text(load(gse, "study.json"), with_paper=True))
    bad, n = [], 0

    def chk(where, q):
        nonlocal n
        n += 1
        if not q or norm(q) not in text:
            bad.append({"where": where, "quote": q})

    for f in lists.get("factors", []) + lists.get("candidate_keys", []):
        for lv in f["levels"]:
            chk(f"factor {f['name']}={lv['value']}", lv.get("quote"))
    for k in ("donors", "biosample_sharing", "assays", "attributes", "targets", "controls"):
        for x in lists.get(k, []):
            chk(f"{k} {x.get('id') or x.get('kind') or x.get('name') or x.get('gsms')}", x.get("quote"))
    return {"quotes": n, "failed": bad}


def draft(gse, writer="only"):  # v1: DeepSeek writes the lists (the split under test); "auto" is the old code-first path
    study = load(gse, "study.json")
    code = code_draft(study)
    out = {"code": code, "code_quote_check": check_quotes(gse, code)}
    if writer in ("always", "only") or (writer == "auto" and not code["candidate_keys"]):
        ds = deepseek_draft(gse, study)
        out["deepseek"] = ds
        out["deepseek_quote_check"] = check_quotes(gse, ds)
    out["used"] = "code" if code["candidate_keys"] and writer != "only" else "deepseek"
    save(gse, "lists.json", out)
    return out


if __name__ == "__main__":
    import json

    r = draft(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "auto")
    print(json.dumps({k: v for k, v in r.items() if k.endswith("check") or k == "used"}, indent=1))
    if "deepseek" in r:
        print(json.dumps({k: v for k, v in r["deepseek"].items()}, indent=1, ensure_ascii=False)[:3000])
