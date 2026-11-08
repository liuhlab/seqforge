# PROTOTYPE, throwaway: fetch a GEO Series' text over public NCBI HTTP, follow SubSeries,
# cache the raw SOFT under runs/<GSE>/raw/, and parse it to runs/<GSE>/study.json.
import re
import sys
import time
import urllib.request

from common import run_dir, save

GEO = "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={acc}&targ={targ}&form=text&view=brief"


def soft(gse, acc, targ):
    f = run_dir(gse) / "raw" / f"{acc}.{targ}.soft"
    if not f.exists():
        url = GEO.format(acc=acc, targ=targ)
        for i in range(4):
            try:
                with urllib.request.urlopen(url, timeout=60) as r:
                    txt = r.read().decode(errors="replace")
                # drop submitter contact lines (names, addresses, emails): not needed, and the branch is public
                f.write_text("\n".join(x for x in txt.splitlines() if "_contact_" not in x.split(" = ")[0]) + "\n")
                break
            except Exception:
                time.sleep(2 + 3 * i)
        else:
            raise SystemExit(f"could not fetch {acc} {targ}")
        time.sleep(0.4)
    return f.read_text(errors="replace")


def records(text):
    """Split SOFT into [(kind, acc, {key: [values]})]."""
    out, cur = [], None
    for line in text.splitlines():
        if line.startswith("^"):
            kind, _, acc = line[1:].partition(" = ")
            cur = (kind.strip(), acc.strip(), {})
            out.append(cur)
        elif line.startswith("!") and cur is not None:
            k, _, v = line[1:].partition(" = ")
            cur[2].setdefault(k.strip(), []).append(v.strip())
    return out


def one(d, k):
    v = d.get(k, [])
    return " ".join(v) if v else None


def fetch(gse):
    series_text, samples, sub_seen = {}, {}, []
    todo, superseries = [gse], None
    bioproject, pmid = set(), set()
    while todo:
        acc = todo.pop(0)
        if acc in series_text:
            continue
        recs = [r for r in records(soft(gse, acc, "gse")) if r[0] == "SERIES"]
        if not recs:
            raise SystemExit(f"no SERIES record for {acc}")
        d = recs[0][2]
        subs = []
        for rel in d.get("Series_relation", []):
            if rel.startswith("SuperSeries of:"):
                subs.append(rel.split(":", 1)[1].strip())
            if rel.startswith("BioProject:"):
                bioproject.add(rel.rsplit("/", 1)[-1])
        pmid.update(d.get("Series_pubmed_id", []))
        series_text[acc] = {
            "title": one(d, "Series_title"),
            "summary": d.get("Series_summary", []),
            "overall_design": d.get("Series_overall_design", []),
            "type": d.get("Series_type", []),
            "sample_ids": d.get("Series_sample_id", []),
            "subseries": subs,
        }
        if subs:
            if acc == gse:
                superseries = gse
            todo.extend(subs)
            continue
        sub_seen.append(acc)
        for kind, gsm, s in records(soft(gse, acc, "gsm")):
            if kind != "SAMPLE" or gsm in samples:
                continue
            chars = []
            for c in s.get("Sample_characteristics_ch1", []):
                k, sep, v = c.partition(":")
                chars.append([k.strip(), v.strip()] if sep else ["", c.strip()])
            samples[gsm] = {
                "gsm": gsm,
                "series": [x for x in s.get("Sample_series_id", [])],
                "title": one(s, "Sample_title"),
                "source": one(s, "Sample_source_name_ch1"),
                "organism": one(s, "Sample_organism_ch1"),
                "taxid": one(s, "Sample_taxid_ch1"),
                "characteristics": chars,
                "library_strategy": one(s, "Sample_library_strategy"),
                "library_source": one(s, "Sample_library_source"),
                "library_selection": one(s, "Sample_library_selection"),
                "molecule": one(s, "Sample_molecule_ch1"),
                "description": s.get("Sample_description", []),
                "protocols": {
                    "growth": one(s, "Sample_growth_protocol_ch1"),
                    "treatment": one(s, "Sample_treatment_protocol_ch1"),
                    "extract": one(s, "Sample_extract_protocol_ch1"),
                },
                "data_processing": s.get("Sample_data_processing", []),
            }
    study = {
        "gse": gse,
        "series": [gse] + [s for s in series_text if s != gse],
        "superseries": superseries,
        "bioproject": sorted(bioproject),
        "pmid": sorted(pmid),
        "series_text": series_text,
        "samples": [samples[k] for k in sorted(samples)],
    }
    save(gse, "study.json", study)
    return study


def corpus(gse):
    """All fetched raw text, for grepping quotes back."""
    return "\n".join(f.read_text(errors="replace") for f in sorted((run_dir(gse) / "raw").glob("*.soft")))


if __name__ == "__main__":
    s = fetch(sys.argv[1])
    print(s["gse"], "series", s["series"], "samples", len(s["samples"]))
