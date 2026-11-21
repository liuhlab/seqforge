# PROTOTYPE, throwaway (issue #629): what a study reading reads, from the toy store.
#
# A study = the Series in the standard set's bounds.yaml, their sequenced samples (Sample_type SRA;
# a microarray sample is counted and dropped), and the bounds' papers: the abstract, and the full
# text (article XML, else PDF) where the paper store holds it. GEO supplementary files and paper
# supplements are never read. The material is cached under _cache/material/ (git-ignored: it holds
# paper text, which is private).
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SET = ROOT / "evals" / "standard-set"
CACHE = HERE / "_cache" / "material"
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

SERIES_FIELDS = ["Series_title", "Series_type", "Series_summary", "Series_overall_design",
                 "Series_relation"]
SAMPLE_FIELDS = ["Sample_title", "Sample_source_name_ch1", "Sample_organism_ch1",
                 "Sample_characteristics_ch1", "Sample_molecule_ch1", "Sample_description",
                 "Sample_library_strategy", "Sample_library_source", "Sample_library_selection",
                 "Sample_instrument_model", "Sample_treatment_protocol_ch1",
                 "Sample_growth_protocol_ch1", "Sample_extract_protocol_ch1", "Sample_taxid_ch1",
                 "Sample_type", "Sample_relation", "Sample_series_id"]
FULLTEXT_CAP = 150_000  # characters of article body passed on


def study_ids():
    return sorted(d.name for d in SET.iterdir() if d.is_dir() and (d / "bounds.yaml").exists())


def _xml_text(path_or_file):
    """Article XML (JATS) body as (section, text) paragraphs; references and back matter dropped."""
    tree = ET.parse(path_or_file)
    root = tree.getroot()
    for el in root.iter():  # strip namespaces
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    out = []
    body = root.find(".//body")
    if body is None:
        return out

    def walk(node, trail):
        title = node.find("title")
        here = trail + ([" ".join("".join(title.itertext()).split())] if title is not None else [])
        for child in node:
            if child.tag == "sec":
                walk(child, here)
            elif child.tag in ("p", "fig", "table-wrap", "list", "disp-quote"):
                txt = " ".join("".join(child.itertext()).split())
                if txt:
                    out.append((" > ".join(here) or "Body", txt))

    walk(body, [])
    return out


def _pdf_text(path):
    import fitz  # PyMuPDF

    doc = fitz.open(path)
    pages = [p.get_text() for p in doc]
    text = "\n".join(pages)
    # Drop the reference list: the last "References" heading onward.
    m = list(re.finditer(r"\n\s*(References|REFERENCES|Literature Cited|Bibliography)\s*\n", text))
    if m and m[-1].start() > len(text) * 0.5:
        text = text[: m[-1].start()]
    return text


def _norm_ws(s):
    return " ".join(s.split())


def build(gid, store, papers):
    bounds = yaml.safe_load((SET / gid / "bounds.yaml").read_text())
    series_ids = [s["id"] for s in bounds["series"]]
    recs = store.open_many(series_ids)
    series, sample_ids = [], []
    for g in series_ids:
        r = recs.get(g)
        if r is None or isinstance(r, Exception):
            series.append({"id": g, "held": False})
            continue
        fields = {f: [str(v) for v in r.field(f) if str(v).strip()] for f in SERIES_FIELDS}
        subs = sorted({ln.other for ln in r.links if ln.relation == "SuperSeries of"})
        series.append({"id": g, "held": True, "fields": fields, "superseries_of": subs})
        for ln in r.links:
            if ln.relation == "sample" and ln.other not in sample_ids:
                sample_ids.append(ln.other)
    srecs = store.open_many(sorted(sample_ids, key=lambda s: int(s[3:])))
    samples, dropped = [], []
    for gsm in sorted(sample_ids, key=lambda s: int(s[3:])):
        r = srecs.get(gsm)
        if r is None or isinstance(r, Exception):
            dropped.append({"id": gsm, "why": "not held"})
            continue
        f = {k: [str(v) for v in r.field(k) if str(v).strip()] for k in SAMPLE_FIELDS}
        stype = (f.get("Sample_type") or ["?"])[0]
        if stype != "SRA":
            dropped.append({"id": gsm, "why": f"Sample_type {stype}: not sequenced"})
            continue
        samples.append({"id": gsm, "fields": f})
    pmids = [p.split(":")[1] for p in bounds["papers"]]
    pinfo = papers.papers(pmids) if (papers is not None and pmids) else {}
    paper_rows = []
    for pm in pmids:
        p = pinfo.get(pm)
        info = getattr(p, "info", None)
        row = {"id": f"pmid:{pm}", "title": getattr(info, "title", None),
               "abstract": getattr(info, "abstract", None), "full_kind": None, "full": None}
        if p is not None:
            held = {f.kind: f for f in p.files if f.path is not None and f.kind in ("xml", "pdf")}
            try:
                if "xml" in held:
                    with held["xml"].open() as fh:
                        paras = _xml_text(fh)
                    if paras:
                        row["full_kind"] = "xml"
                        row["full"] = paras
                if row["full"] is None and "pdf" in held:
                    txt = _pdf_text(str(held["pdf"].path))
                    if txt.strip():
                        row["full_kind"] = "pdf"
                        row["full"] = [("Full text (PDF)", txt)]
            except Exception as e:  # noqa: BLE001 - prototype: record and go on
                row["full_error"] = repr(e)
        paper_rows.append(row)
    return {"id": gid, "series": series, "samples": samples, "dropped": dropped,
            "papers": paper_rows}


def load(gid):
    f = CACHE / f"{gid}.json"
    if f.exists():
        return json.loads(f.read_text())
    raise FileNotFoundError(f"run `python material.py` first: {f}")


def main():
    import labdata

    from seqforge.study.group import paper_reader

    CACHE.mkdir(parents=True, exist_ok=True)
    store, papers = labdata.open_store(), paper_reader()
    for gid in study_ids():
        f = CACHE / f"{gid}.json"
        if f.exists():
            continue
        m = build(gid, store, papers)
        f.write_text(json.dumps(m, ensure_ascii=False))
        n_full = sum(1 for p in m["papers"] if p["full"])
        print(gid, len(m["samples"]), "dropped", len(m["dropped"]), "full", n_full,
              [p["full_kind"] for p in m["papers"]])


# ------------------------------------------------------------------ text views and quote finding
def records_text(m, with_protocols=True):
    """The GEO records as the writer reads them: every line is `FIELD: value`, so a quote can name
    its field. Protocol fields the same in every sample are shown once, under EACH_SAMPLE."""
    out = []
    for s in m["series"]:
        out.append(f"=== GEO Series {s['id']} ===")
        if not s["held"]:
            out.append("(not held)")
            continue
        for k, vs in s["fields"].items():
            for v in vs:
                out.append(f"{k}: {v}")
        if s.get("superseries_of"):
            out.append(f"(SuperSeries of {', '.join(s['superseries_of'])})")
    samples = m["samples"]
    common = {}
    if with_protocols and len(samples) > 1:
        for k in ("Sample_treatment_protocol_ch1", "Sample_growth_protocol_ch1",
                  "Sample_extract_protocol_ch1", "Sample_organism_ch1", "Sample_molecule_ch1",
                  "Sample_instrument_model", "Sample_library_source", "Sample_library_selection"):
            vals = {tuple(s["fields"].get(k, [])) for s in samples}
            if len(vals) == 1 and next(iter(vals)):
                common[k] = list(next(iter(vals)))
    out.append(f"\n=== Sequenced samples: {len(samples)} ===")
    if common:
        out.append("EACH_SAMPLE (every sample record holds these lines):")
        for k, vs in common.items():
            for v in vs:
                out.append(f"  {k}: {v[:1500]}")
    for s in samples:
        out.append(f"--- {s['id']} ---")
        for k in SAMPLE_FIELDS:
            if k in common or k in ("Sample_type", "Sample_relation", "Sample_taxid_ch1"):
                continue
            if not with_protocols and k.endswith("protocol_ch1"):
                continue
            for v in s["fields"].get(k, []):
                out.append(f"{k}: {v[:1500]}")
    if m["dropped"]:
        out.append(f"(left out, not sequenced or not held: {len(m['dropped'])} samples)")
    return "\n".join(out)


def papers_text(m, full):
    out = []
    for p in m["papers"]:
        out.append(f"=== Paper {p['id']} ===")
        out.append(f"Title: {p['title'] or '(not available)'}")
        if p["abstract"]:
            out.append(f"Abstract: {p['abstract']}")
        if full and p["full"]:
            out.append(f"Full text ({p['full_kind']}), each paragraph headed by its section:")
            n = 0
            for sec, txt in p["full"]:
                piece = f"[{sec}] {txt}"
                if n + len(piece) > FULLTEXT_CAP:
                    out.append("[... full text cut here ...]")
                    break
                out.append(piece)
                n += len(piece)
    if not m["papers"]:
        out.append("No paper belongs to this study.")
    return "\n".join(out)


def has_full(m):
    return any(p["full"] for p in m["papers"])


_TRANS = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"',
                        "–": "-", "—": "-", " ": " ", " ": " "})


def _loose(s):
    s = s.translate(_TRANS).lower()
    s = re.sub(r"-\s+", "-", s)  # PDF hyphenation across a line break
    return " ".join(s.split())


def source_index(m, full):
    """Every place a quote may come from: (source, field) -> list of texts (a field may repeat)."""
    idx = {}
    for s in m["series"]:
        if s["held"]:
            for k, vs in s["fields"].items():
                idx[(s["id"], k)] = vs
    for s in m["samples"]:
        for k, vs in s["fields"].items():
            idx[(s["id"], k)] = vs
    for p in m["papers"]:
        if p["title"]:
            idx[(p["id"], "title")] = [p["title"]]
        if p["abstract"]:
            idx[(p["id"], "abstract")] = [p["abstract"]]
        if full and p["full"]:
            for sec, txt in p["full"]:
                idx.setdefault((p["id"], sec), []).append(txt)
    return idx


def find_quote(quote, idx, sample_ids):
    """Where a quote is: exact (records: char for char; papers: after collapsing white space), or
    only after loose matching (case, quote marks, PDF hyphenation). Returns a dict."""
    text = (quote or {}).get("text") if isinstance(quote, dict) else None
    if not text or not str(text).strip():
        return {"status": "none"}
    text = str(text).strip()
    src = str(quote.get("source", "")).strip()
    fld = str(quote.get("field", "")).strip()
    if src.lower() in ("each_sample", "each sample", "every sample"):
        holders = [g for g in sample_ids if any(text in v for v in idx.get((g, fld), []))]
        if holders and len(holders) == len(sample_ids):
            return {"status": "named", "record": "each_sample", "field": fld, "exact": True,
                    "quote": text}
    named = (src, fld)
    paper = src.startswith("pmid:")

    def exact_in(key):
        vs = idx.get(key, [])
        if key[0].startswith("pmid:"):
            t = _norm_ws(text)
            return any(t in _norm_ws(v) for v in vs)
        return any(text in v for v in vs)

    def loose_in(key):
        t = _loose(text)
        return any(t in _loose(v) for v in idx.get(key, []))

    if named in idx and exact_in(named):
        return {"status": "named", "source": src, "field": fld, "exact": True, "quote": text}
    # A paper quote with a wrong section name: look in the same paper.
    same = [k for k in idx if k[0] == src and k != named]
    for k in same:
        if exact_in(k):
            return {"status": "same_source_other_field", "source": src, "field": k[1],
                    "exact": True, "quote": text}
    # Same field, other records (e.g. the quote is held by several samples).
    holders = [k for k in idx if k[1] == fld and k[0] != src and exact_in(k)]
    if holders:
        return {"status": "other_record", "source": holders[0][0], "field": fld,
                "holders": sorted({h[0] for h in holders}), "exact": True, "quote": text}
    for k in idx:
        if exact_in(k):
            return {"status": "elsewhere", "source": k[0], "field": k[1], "exact": True,
                    "quote": text}
    for k in ([named] if named in idx else []) + same + [k for k in idx if k not in same]:
        if loose_in(k):
            return {"status": "loose_only", "source": k[0], "field": k[1], "exact": False,
                    "quote": text, "paper": k[0].startswith("pmid:")}
    return {"status": "not_found", "paper": paper}


if __name__ == "__main__":
    main()
