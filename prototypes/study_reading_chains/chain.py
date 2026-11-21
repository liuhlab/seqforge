# PROTOTYPE, throwaway (issue #629): one study, one chain -> a reading file or a refusal.
#
#   0. lists   chain "code": code drafts the open lists from GEO's typed sample fields, and Jev maps
#              each characteristics key to a label (a closed question).
#              chains "flash"/"flash_think": DeepSeek Flash writes the lists, thinking off / on,
#              reading records + abstracts, or records + full text.
#   A. Jev     study-level: is each listed factor varied on purpose (asked plain and negated), its
#              baseline (options in order and reversed), ordered or not, na-or-unknown for a label
#              with no value, the donor kind, and yes/no probes for the format's known gaps.
#   B. Jev     per sample: its level on each factor and its value on each label, from the listed
#              options plus "unknown" and "na"; asked twice, options in order and reversed.
#   C. code    finds every quote in its record or paper; Jev judges whether each found passage backs
#              its claim; Jev answers same-material for candidate pull-down/control pairs.
#   D. Jev     picks an ontology term among OLS4/Cellosaurus candidates (+ "none"); then the same
#              question again with its pick removed, where "none" is the right answer.
#   E. code    writes the reading, runs the format check (refused if it fails), decides the ending.
import copy
import json
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))

import yaml  # noqa: E402
from labllm import Model  # noqa: E402

import material as M  # noqa: E402
import ontology as O  # noqa: E402
from seqforge.study.claims import load as load_yaml  # noqa: E402
from seqforge.study.reading import check_reading  # noqa: E402

JEV = Model("typesafe", "jev-1.14.0")
WRITER = "deepseek-flash"
LOGS = HERE / "_logs"
LABELS16 = ["assay", "target", "control_type", "species", "sex", "age", "development_stage",
            "tissue", "cell_type", "cell_line", "disease", "genotype", "strain", "treatment",
            "dose", "duration"]
NUMERIC = {"age", "dose", "duration"}
LABEL_DEF = {
    "assay": "the kind of sequencing experiment, such as RNA-Seq, ChIP-Seq, ATAC-seq, bisulfite-seq",
    "target": "what a pull-down enriches: a histone mark or a protein; na for an assay with no "
              "pull-down and for a control library",
    "control_type": "the kind of background-control library, such as input or IgG; na for every "
                    "other library (an untreated or wild-type comparison group is NOT a control)",
    "species": "the species",
    "sex": "the sex: female, male, hermaphrodite or mixed",
    "age": "the age, with a number and a unit",
    "development_stage": "the stage of life, such as embryo, larval stage, adult",
    "tissue": "the tissue or organ",
    "cell_type": "the cell type",
    "cell_line": "the cell line",
    "disease": "the disease or disease model, or healthy",
    "genotype": "the genotype",
    "strain": "the strain",
    "treatment": "what was applied to the sample (a drug, diet, vector, infection, procedure)",
    "dose": "how much of the treatment, with a number and a unit",
    "duration": "how long the treatment itself was applied, with a number and a unit (a time "
                "from the end of a treatment to harvest is not a duration)",
}
NA_EXAMPLE = {
    "target": "an assay with no pull-down, or a control library",
    "control_type": "a library that is not a background control",
    "cell_line": "tissue taken from an animal",
    "cell_type": "whole tissue with no cell type isolated",
    "treatment": "no treatment applied",
    "dose": "no treatment", "duration": "no treatment",
    "disease": "", "genotype": "", "strain": "a species with no strains, such as human",
}
GAP_KINDS = ["sources_disagree", "two_beliefs_one_claim", "untreated_or_vehicle",
             "several_compounds", "route_or_side", "dose_per_unit", "duration_without_number",
             "time_from_event", "group_smaller_than_study", "series_membership",
             "factor_over_part", "crossed_not_fully", "pooled_material", "held_constant",
             "batch", "sequencer", "term_missing", "pdf_quote", "one_control_many_materials",
             "baseline_as_control", "archive_biosample_differs", "other"]
# Yes/no probes Jev answers per study, over records + abstracts (format gaps of issue #603).
GAP_PROBES = {
    "sources_disagree": "Do two places in this text disagree about a fact of the samples, such as "
                        "a strain, an age, a time or a dose given differently in two records or in "
                        "a record and the paper?",
    "untreated_or_vehicle": "Does the study have an untreated, mock or vehicle-treated group of "
                            "samples?",
    "several_compounds": "Was any sample treated with two or more compounds or agents at once?",
    "route_or_side": "Does the text give a route of delivery (injection site, oral, "
                     "intraventricular) or a body side (left, right) for a treatment or tissue?",
    "dose_per_unit": "Is a dose given per side, per animal, per kg or per well rather than as one "
                     "plain amount?",
    "duration_without_number": "Is a treatment length given without a number, such as overnight "
                               "or until harvest?",
    "time_from_event": "Is a time measured from an event other than the start of a treatment, "
                       "such as days after injection, after birth or after infection?",
    "pooled_material": "Is any sequenced library made from pooled material of many animals or "
                       "people?",
    "held_constant": "Does the text name a condition held the same for every sample, such as a "
                     "time of day, a temperature, a diet or a kit?",
    "batch": "Do the samples fall into batches, runs or lanes that the text names?",
    "sequencer": "Were the samples sequenced on two or more sequencer models?",
    "vector": "Was a gene-therapy vector, virus or engineered construct applied to the samples?",
    "one_control_many_materials": "Does one control library (such as an input) serve pull-down "
                                  "libraries made from different material?",
    "baseline_as_control": "Is a comparison group used as the background control of a pull-down, "
                           "with no input library deposited?",
    "single_cell": "Is this a single-cell or single-nucleus sequencing study?",
    "mixed_assays": "Does the study sequence two or more different kinds of library (such as "
                    "RNA-seq and ChIP-seq)?",
}

_lock = threading.Lock()
REGISTRY = {}


# ------------------------------------------------------------------ small helpers
def jsave(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False, default=str))


def jload(path):
    return json.loads(path.read_text()) if path.exists() else None


class Calls:
    """Model calls for one study-chain: cached in _logs, costs and seconds recorded per step."""

    def __init__(self, meters, gid, chain):
        self.meters, self.gid, self.chain = meters, gid, chain
        self.dir = LOGS / chain / gid
        self.ledger = []
        REGISTRY[(chain, gid)] = self

    def jev(self, name, description, questions):
        f = self.dir / f"jev_{name}.json"
        got = jload(f)
        if got is None:
            t = time.time()
            r = self.meters["jev"].jev(JEV, description=description, questions=questions,
                                       tag=f"629/{self.chain}/{self.gid}/{name}")
            got = {"answers": r.answers, "usage": r.usage, "cost_usd": r.cost_usd,
                   "first_cost_usd": r.first_cost_usd, "cached": r.cached,
                   "seconds": round(time.time() - t, 3), "n_questions": len(questions)}
            jsave(f, got)
        with _lock:
            self.ledger.append({"model": "jev", "step": name.split("_")[0],
                                "cost": got["first_cost_usd"], "seconds": got["seconds"],
                                "tokens_in": got["usage"].get("input_tokens", 0)})
        return got["answers"] or {}

    def writer(self, name, system, user, thinking):
        f = self.dir / f"writer_{name}.json"
        got = jload(f)
        if got is None:
            model = Model("deepseek", WRITER, thinking=thinking,
                          max_output_tokens=40000 if thinking else 16000)
            t = time.time()
            r = self.meters["deepseek"].chat(model, system=system, user=user,
                                             schema={"type": "object"},
                                             tag=f"629/{self.chain}/{self.gid}/{name}")
            got = {"text": r.text, "usage": r.usage, "cost_usd": r.cost_usd,
                   "first_cost_usd": r.first_cost_usd, "cached": r.cached,
                   "seconds": round(time.time() - t, 3)}
            jsave(f, got)
        with _lock:
            self.ledger.append({"model": "deepseek", "step": "writer",
                                "cost": got["first_cost_usd"], "seconds": got["seconds"],
                                "tokens_in": got["usage"].get("prompt_tokens", 0),
                                "tokens_out": got["usage"].get("completion_tokens", 0)})
        return got


def parse_json(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        m = re.search(r"\{.*\}", text or "", re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                return None
    return None


_NUM = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|to|–)?\s*(\d+(?:\.\d+)?)?\s*[-\s]?([A-Za-zµμ]+)?")
_UNITS = {"d": "day", "day": "day", "days": "day", "dpf": "day", "w": "week", "wk": "week",
          "week": "week", "weeks": "week", "wks": "week", "m": "month", "mo": "month",
          "month": "month", "months": "month", "mos": "month", "y": "year", "yr": "year",
          "year": "year", "years": "year", "yrs": "year", "h": "hour", "hr": "hour",
          "hour": "hour", "hours": "hour", "hrs": "hour", "min": "minute", "minutes": "minute",
          "mg/kg": "mg/kg", "um": "micromolar", "µm": "micromolar", "μm": "micromolar",
          "nm": "nanomolar", "mm": "millimolar", "mg": "mg", "ug": "microgram"}


def parse_number(text):
    """Code's own value/unit parse for an age, dose or duration text (chain "code")."""
    m = _NUM.search(text or "")
    if not m:
        return None, None
    lo = float(m.group(1))
    hi = float(m.group(2)) if m.group(2) else None
    unit = _UNITS.get((m.group(3) or "").lower())
    if unit is None:
        return None, None
    val = [lo, hi] if hi is not None else lo
    if isinstance(val, float) and val.is_integer():
        val = int(val)
    return val, unit


# ------------------------------------------------------------------ 0. lists: code
def char_table(m):
    """key -> {sample: value} from Sample_characteristics_ch1 `key: value` lines, plus the source
    name as its own key."""
    keys = defaultdict(dict)
    for s in m["samples"]:
        for line in s["fields"].get("Sample_characteristics_ch1", []):
            if ":" in line:
                k, v = line.split(":", 1)
                keys[k.strip()][s["id"]] = (v.strip(), line)
            else:
                keys["(characteristic)"][s["id"]] = (line.strip(), line)
        for v in s["fields"].get("Sample_source_name_ch1", []):
            keys["source name"][s["id"]] = (v, v)
    return keys


KEY_OPTIONS = {**{lb: LABEL_DEF[lb] for lb in LABELS16 if lb not in ("species", "assay")},
               "donor": "names the individual animal or person a sample came from",
               "replicate_or_batch": "a replicate number, batch, lane or library id",
               "own_attribute": "another attribute of the samples, none of the labels above"}


def code_lists(m, calls):
    """Chain "code": the lists from GEO's typed fields. Jev maps each key to a label."""
    keys = char_table(m)
    desc = M.records_text(m, with_protocols=False)[:60000]
    qs = {}
    names = list(keys)
    for i, k in enumerate(names):
        vals = list(dict.fromkeys(v for v, _ in keys[k].values()))[:12]
        qs[f"k{i}"] = {"type": "choice",
                       "instructions": f"The sample characteristic \"{k}\" takes the values: "
                                       f"{'; '.join(vals)}. Which label does it give?",
                       "criteria": dict(KEY_OPTIONS)}
    ans = calls.jev("keymap_0", desc, qs) if qs else {}
    keymap = {}
    for i, k in enumerate(names):
        a = ans.get(f"k{i}") or {}
        keymap[k] = {"label": a.get("choice", "own_attribute"),
                     "p": (a.get("probabilities") or {}).get(a.get("choice"), None)}
    lists = {"factors": [], "labels": {lb: {"values": [], "gap": None} for lb in LABELS16},
             "extra_labels": {}, "donors": {"kind": None, "ids": [], "why": ""},
             "shared_material": [], "format_gaps": [], "keymap": keymap}

    def q_for(sample, field, text):
        return {"source": sample, "field": field, "text": text}

    # Typed fields straight from the records.
    for lb, field in (("species", "Sample_organism_ch1"), ("assay", "Sample_library_strategy")):
        seen = {}
        for s in m["samples"]:
            for v in s["fields"].get(field, []):
                seen.setdefault(v, s["id"])
        for v, g in seen.items():
            lists["labels"][lb]["values"].append(
                {"text": v, "value": None, "unit": None, "quote": q_for(g, field, v),
                 "how": f"the record's {field}"})
    for k, km in keymap.items():
        lb = km["label"]
        holders = keys[k]
        distinct = {}
        for g, (v, line) in holders.items():
            distinct.setdefault(v, (g, line))
        field = "Sample_source_name_ch1" if k == "source name" else "Sample_characteristics_ch1"
        entries = []
        for v, (g, line) in distinct.items():
            val, unit = parse_number(v) if lb in NUMERIC else (None, None)
            entries.append({"text": v, "value": val, "unit": unit, "quote": q_for(g, field, line),
                            "how": f"the record's characteristic \"{k}\""})
        if lb == "donor":
            lists["donors"] = {"kind": "listed", "ids": [{"id": e["text"], "quote": e["quote"]}
                                                         for e in entries], "why": k}
            continue
        if lb == "replicate_or_batch":
            continue
        name = lb if lb in LABELS16 else re.sub(r"[^a-z0-9]+", "_", k.lower()).strip("_") or "attr"
        bucket = lists["labels"] if lb in LABELS16 else lists["extra_labels"]
        bucket.setdefault(name, {"values": [], "gap": None})
        have = {e["text"] for e in bucket[name]["values"]}
        bucket[name]["values"] += [e for e in entries if e["text"] not in have]
        if len(distinct) > 1 and len(holders) == len(m["samples"]):
            lists["factors"].append({
                "label": name, "how": f"the characteristic \"{k}\" varies between samples",
                "levels": [{"name": e["text"][:60], "text": e["text"], "value": e["value"],
                            "unit": e["unit"], "quote": e["quote"]} for e in entries],
                "baseline": None, "ordered": None, "quote": None})
    # A factor label's values are its levels: drop the duplicate value list.
    for f in lists["factors"]:
        b = lists["labels"] if f["label"] in LABELS16 else lists["extra_labels"]
        if f["label"] in b:
            b[f["label"]]["values"] = []
    return lists


# ------------------------------------------------------------------ 0. lists: the writer
WRITER_SYSTEM = """You read the GEO records of one study (and its paper) and write the open lists
that a later step uses to describe every sequenced sample. You do not say which sample has which
value: a later step decides that from your lists. Write only what the text supports.

Every sequenced sample will get these labels:
""" + "\n".join(f"- {lb}: {d}" for lb, d in LABEL_DEF.items()) + """

Three points of meaning:
- control_type is only for background-control libraries (input, IgG). An untreated or wild-type
  group that the study compares the others with is a baseline, not a control.
- duration is the length of the treatment itself. Any other time (from the end of a treatment to
  harvest, days after injection) is an attribute of the study's own: put it in extra_labels.
- A pull-down library and its control made from the same material are listed together under
  shared_material.

Answer with ONE JSON object:
{
 "factors": [  // the labels the study varied ON PURPOSE to compare, e.g. treatment, genotype, age
   {"label": "one of the labels above, or a short snake_case name of the study's own",
    "levels": [{"name": "short unique name", "text": "the source's own words for this level",
                "value": number or [low, high] or null, "unit": "one word or null",
                "quote": Q or null}],
    "baseline": "the level name the study compares the others with, or null",
    "ordered": true or false, "quote": Q or null, "how": "at most 15 words"}],
 "labels": {   // EVERY one of the 16 labels above, as a key
   "<label>": {"values": [{"text": "the source's own words", "value": number or [low, high] or null,
                           "unit": "one word or null", "quote": Q or null, "how": "at most 15 words"}],
               "gap": null or {"kind": "na" or "unknown", "why": "at most 15 words"}}},
 "extra_labels": { "<snake_case name>": same shape as a label },  // attributes of the study's own
 "donors": {"kind": "one_each" | "pool" | "shared" | "cell_line" | "unknown",
            "ids": [{"id": "an individual's name as the text writes it", "quote": Q or null}],
            "why": "at most 15 words"},
 "shared_material": [{"samples": ["GSM..."], "why": "at most 15 words", "quote": Q or null}],
 "format_gaps": [{"kind": "<one of: """ + ", ".join(GAP_KINDS) + """>",
                  "note": "at most 25 words"}]
}
Q = {"source": "a GSE or GSM id, or each_sample, or pmid:<number>",
     "field": "for a record: the field name exactly as written before the colon, such as
               Sample_characteristics_ch1; for a paper: title, abstract, or the section heading
               shown in square brackets",
     "text": "words copied exactly from that one field, one continuous passage of at most 40 words"}

Rules:
- A label the study varied is a factor; for that label leave "values" empty, the levels say it.
- List every distinct value a label takes among the sequenced samples. A label with one value for
  every sample has one entry. A label with no value has empty values and a gap: "na" when it does
  not apply (the cell line of tissue taken from an animal), "unknown" when it applies but nothing
  read says it.
- Copy every quote exactly: never join pieces, never fix spelling, never add words. Prefer the
  words of a GEO record over the paper when both say it. "each_sample" means every sample record
  holds the words in that field.
- Only sequenced samples count. GEO supplementary files are not available: a value found only
  there is unknown.
- format_gaps lists what a reading of this study cannot hold well (sources that disagree, a dose
  per side, pooled animals, a factor over part of the samples, batches, a term with no ontology).
"""


def writer_lists(m, calls, thinking, full):
    user = M.records_text(m) + "\n\n" + M.papers_text(m, full)
    got = calls.writer(f"{'think' if thinking else 'plain'}_{'full' if full else 'abs'}",
                       WRITER_SYSTEM, user, thinking)
    ans = parse_json(got["text"])
    if not isinstance(ans, dict):
        return None, "the writer's answer is not a JSON object"
    lists = {"factors": [], "labels": {}, "extra_labels": {}, "donors": {}, "shared_material": [],
             "format_gaps": []}
    for f in ans.get("factors") or []:
        if not isinstance(f, dict) or not f.get("label") or not isinstance(f.get("levels"), list):
            continue
        lv = [x for x in f["levels"] if isinstance(x, dict) and x.get("name") is not None]
        for x in lv:
            x["name"] = str(x["name"])
            x["text"] = str(x.get("text") or x["name"])
        lists["factors"].append({"label": re.sub(r"[^a-z0-9_]+", "_", str(f["label"]).lower()),
                                 "levels": lv, "baseline": f.get("baseline"),
                                 "ordered": f.get("ordered"), "quote": f.get("quote"),
                                 "how": f.get("how") or ""})

    def norm_label(d):
        vals = []
        for v in (d or {}).get("values") or []:
            if isinstance(v, dict) and (v.get("text") or v.get("value") is not None):
                v["text"] = str(v.get("text") or v.get("value"))
                vals.append(v)
        gap = (d or {}).get("gap")
        return {"values": vals, "gap": gap if isinstance(gap, dict) else None}

    labels = ans.get("labels") or {}
    for lb in LABELS16:
        lists["labels"][lb] = norm_label(labels.get(lb))
    for k, d in (ans.get("extra_labels") or {}).items():
        name = re.sub(r"[^a-z0-9_]+", "_", str(k).lower()).strip("_")
        if name and name not in LABELS16 and name not in ("biosample", "donor"):
            lists["extra_labels"][name] = norm_label(d)
    lists["donors"] = ans.get("donors") if isinstance(ans.get("donors"), dict) else {}
    lists["shared_material"] = [x for x in ans.get("shared_material") or [] if isinstance(x, dict)]
    lists["format_gaps"] = [x for x in ans.get("format_gaps") or [] if isinstance(x, dict)]
    # A factor label's values are its levels (the writer was told so; enforce it).
    for f in lists["factors"]:
        b = lists["labels"] if f["label"] in LABELS16 else lists["extra_labels"]
        b.setdefault(f["label"], {"values": [], "gap": None})["values"] = []
    return lists, None


# ------------------------------------------------------------------ A. Jev, study level
def study_desc(m):
    return (M.records_text(m, with_protocols=True)[:70000] + "\n\n" + M.papers_text(m, False))[:90000]


def study_questions(lists):
    qs, meta = {}, {}
    for i, f in enumerate(lists["factors"]):
        names = [lv["name"] for lv in f["levels"]]
        texts = "; ".join(f"{lv['name']} ({lv.get('text')})" for lv in f["levels"])[:1500]
        qs[f"f{i}_designed"] = {"type": "noul", "instructions":
                                f"Did the study vary {f['label']} on purpose between its sequenced "
                                f"samples, to compare these levels: {texts}?"}
        qs[f"f{i}_designed_neg"] = {"type": "noul", "instructions":
                                    f"Is {f['label']} (values: {texts}) only a description, a "
                                    "replicate or batch label, or a restatement of another factor, "
                                    "rather than something the study varied on purpose to compare?"}
        crit = {f"L{j}": f"{n}" for j, n in enumerate(names)}
        crit["no_reference"] = "the study compares the levels with each other; none is a reference"
        qs[f"f{i}_baseline"] = {"type": "choice", "instructions":
                                f"Which level of {f['label']} is the reference the study compares "
                                "the others with (untreated, wild type, control, young, time zero)?",
                                "criteria": crit}
        qs[f"f{i}_baseline_rev"] = {"type": "choice", "instructions": qs[f"f{i}_baseline"][
            "instructions"], "criteria": dict(reversed(list(crit.items())))}
        qs[f"f{i}_ordered"] = {"type": "noul", "instructions":
                               f"Do the levels of {f['label']} ({texts}) run along a scale, such as "
                               "a time course, a dose series or a series of ages?"}
    for lb, d in {**lists["labels"], **lists["extra_labels"]}.items():
        if d["values"] or any(f["label"] == lb for f in lists["factors"]):
            continue
        ex = NA_EXAMPLE.get(lb, "")
        qs[f"gap_{lb}"] = {"type": "choice", "instructions":
                           f"For the samples of this study, what about their {lb.replace('_', ' ')}"
                           f" ({LABEL_DEF.get(lb, 'an attribute of the study')})?",
                           "criteria": {"na": f"it does not apply to these samples{': ' + ex if ex else ''}",
                                        "unknown": "it applies, but nothing in the text says it"}}
    qs["donor_kind"] = {"type": "choice", "instructions":
                        "Where did each sequenced library's material come from?",
                        "criteria": {"one_each": "each sample from its own individual animal or person",
                                     "shared": "some samples share one individual",
                                     "pool": "each library pools many individuals",
                                     "cell_line": "a cell line or culture, no individual",
                                     "cannot_tell": "the text does not say"}}
    for k, text in GAP_PROBES.items():
        qs[f"gap_probe_{k}"] = {"type": "noul", "instructions": text}
    return qs


# ------------------------------------------------------------------ B. Jev, per sample
def glossary(lists):
    out = ["Values listed for this study, each with the words that say it:"]
    for i, f in enumerate(lists["factors"]):
        out.append(f"Factor {f['label']}:")
        for lv in f["levels"]:
            q = (lv.get("quote") or {}).get("text") if isinstance(lv.get("quote"), dict) else None
            out.append(f"  - {lv['name']}: {lv.get('text')}" + (f' ("{q[:200]}")' if q else ""))
    for lb, d in {**lists["labels"], **lists["extra_labels"]}.items():
        if d["values"]:
            vs = []
            for v in d["values"]:
                q = (v.get("quote") or {}).get("text") if isinstance(v.get("quote"), dict) else None
                vs.append(v["text"] + (f' ("{q[:150]}")' if q and q != v["text"] else ""))
            out.append(f"{lb}: " + " | ".join(vs))
    return "\n".join(out)


def sample_record(m, s):
    out = [f"`sample` is {s['id']}."]
    for k in M.SAMPLE_FIELDS:
        if k in ("Sample_type", "Sample_relation", "Sample_taxid_ch1"):
            continue
        for v in s["fields"].get(k, []):
            out.append(f"{k}: {v[:700]}")
    return "\n".join(out)


def sample_questions(lists, reverse=False):
    qs = {}

    def order(d):
        return dict(reversed(list(d.items()))) if reverse else d

    for i, f in enumerate(lists.get("_factors_kept", [])):
        crit = {f"L{j}": f"`sample` has {f['label']} = {lv['text']}" for j, lv in enumerate(f["levels"])}
        crit["unknown"] = f"`sample`'s text does not say its {f['label']}"
        crit["na"] = f"{f['label']} does not apply to `sample`"
        qs[f"lvl{i}"] = {"type": "choice", "instructions": f"Which {f['label']} does `sample` have?",
                         "criteria": order(crit)}
    factor_labels = {f["label"] for f in lists.get("_factors_kept", [])}
    for lb, d in {**lists["labels"], **lists["extra_labels"]}.items():
        if not d["values"] or lb in factor_labels:
            continue
        crit = {f"v{j}": f"{lb.replace('_', ' ')} is {v['text']}" for j, v in enumerate(d["values"][:25])}
        crit["unknown"] = f"`sample`'s text and the listed words do not say its {lb.replace('_', ' ')}"
        ex = NA_EXAMPLE.get(lb, "")
        crit["na"] = f"{lb.replace('_', ' ')} does not apply to `sample`" + (f" ({ex})" if ex else "")
        qs[f"lab_{lb}"] = {"type": "choice", "instructions":
                           f"What is the {lb.replace('_', ' ')} of `sample` "
                           f"({LABEL_DEF.get(lb, 'an attribute of the study')})?",
                           "criteria": order(crit)}
    ids = [str(x.get("id")) for x in (lists.get("donors") or {}).get("ids") or [] if isinstance(x, dict)]
    if len(ids) >= 2:
        crit = {f"d{j}": f"`sample` came from {i}" for j, i in enumerate(ids[:40])}
        crit["unknown"] = "the text does not say which individual"
        qs["donor"] = {"type": "choice", "instructions": "Which individual did `sample` come from?",
                       "criteria": order(crit)}
    return qs


def pick(a):
    """(choice, p) of a choice answer, or (None, None)."""
    if not a:
        return None, None
    c = a.get("choice")
    return c, (a.get("probabilities") or {}).get(c)


# ------------------------------------------------------------------ the whole chain for one study
def run_study(gid, chain, meters, read_full=False, pool=None):
    """chain in {"code", "flash", "flash_think"}. Returns the result dict (no paper text)."""
    t0 = time.time()
    m = M.load(gid)
    calls = Calls(meters, gid, f"{chain}{'_full' if read_full else ''}")
    res = {"study": gid, "chain": chain, "read_full": read_full, "n_samples": len(m["samples"]),
           "dropped_samples": len(m["dropped"]), "has_full_text": M.has_full(m)}
    sample_ids = [s["id"] for s in m["samples"]]
    # 0. lists
    if chain == "code":
        lists = code_lists(m, calls)
        err = None
    else:
        lists, err = writer_lists(m, calls, thinking=(chain == "flash_think"), full=read_full)
    if lists is None:
        res.update(ending="refused", reason=err, ledger=calls.ledger, seconds=time.time() - t0)
        return res, None
    # A. study level
    sq = study_questions(lists)
    sa = calls.jev("study_0", study_desc(m), sq)
    kept = []
    fstats = []
    for i, f in enumerate(lists["factors"]):
        p_d = (sa.get(f"f{i}_designed") or {}).get("noul")
        p_n = (sa.get(f"f{i}_designed_neg") or {}).get("noul")
        b1, pb1 = pick(sa.get(f"f{i}_baseline"))
        b2, pb2 = pick(sa.get(f"f{i}_baseline_rev"))
        p_o = (sa.get(f"f{i}_ordered") or {}).get("noul")
        st = {"label": f["label"], "levels": [lv["name"] for lv in f["levels"]],
              "p_designed": p_d, "p_designed_neg": p_n, "baseline": b1, "p_baseline": pb1,
              "baseline_rev": b2, "p_baseline_rev": pb2, "p_ordered": p_o,
              "writer_baseline": f.get("baseline"), "writer_ordered": f.get("ordered")}
        st["designed_disagree"] = (p_d is not None and p_n is not None
                                   and (p_d >= 0.5) == (p_n >= 0.5))
        st["baseline_disagree"] = b1 != b2
        st["kept"] = bool(p_d is not None and p_d >= 0.5)
        fstats.append(st)
        if st["kept"]:
            f2 = copy.deepcopy(f)
            f2["_i"] = i
            kept.append(f2)
    # Two kept factors on one label: keep the more certain one (the format keys factors by label).
    by_label = {}
    for f in kept:
        cur = by_label.get(f["label"])
        if cur is None or fstats[f["_i"]]["p_designed"] > fstats[cur["_i"]]["p_designed"]:
            by_label[f["label"]] = f
    lists["_factors_kept"] = list(by_label.values())
    res["factors"] = fstats
    # A listed factor Jev did not keep stays a plain label: its levels become the label's values.
    kept_labels = {f["label"] for f in lists["_factors_kept"]}
    for f in lists["factors"]:
        if f["label"] in kept_labels:
            continue
        b = lists["labels"] if f["label"] in LABELS16 else lists["extra_labels"]
        d = b.setdefault(f["label"], {"values": [], "gap": None})
        have = {v["text"] for v in d["values"]}
        for lv in f["levels"]:
            if lv["text"] not in have:
                d["values"].append({"text": lv["text"], "value": lv.get("value"),
                                    "unit": lv.get("unit"), "quote": lv.get("quote"),
                                    "how": f.get("how") or ""})
                have.add(lv["text"])
    # B. per sample, twice (options in order, then reversed)
    desc_tail = ("\n\nStudy: " + " ".join(
        (s["fields"].get("Series_title") or [""])[0] for s in m["series"] if s["held"])
        + "\nDesign: " + " ".join(
            " ".join(s["fields"].get("Series_overall_design") or []) for s in m["series"]
            if s["held"])[:1500] + "\n\n" + glossary(lists))[:20000]
    q1, q2 = sample_questions(lists), sample_questions(lists, reverse=True)
    jobs = [(s, rev) for s in m["samples"] for rev in (False, True)]

    def ask(job):
        s, rev = job
        return (s["id"], rev), calls.jev(f"sample_{s['id']}_{'rev' if rev else 'fwd'}",
                                         sample_record(m, s) + desc_tail, q2 if rev else q1)

    per = dict((pool.map(ask, jobs) if pool else map(ask, jobs)))
    # C. quotes: find, then Jev backing; same-material pairs
    idx = M.source_index(m, read_full)
    found = []  # (key, candidate dict, label, find result)
    cand_index = {}
    for f in lists["_factors_kept"]:
        for j, lv in enumerate(f["levels"]):
            cand_index[(f["label"], f"L{j}")] = lv
        cand_index[(f["label"], "_factor")] = f
    for lb, d in {**lists["labels"], **lists["extra_labels"]}.items():
        for j, v in enumerate(d["values"][:25]):
            cand_index[(lb, f"v{j}")] = v
    finds = {}
    for key, c in cand_index.items():
        finds[key] = M.find_quote(c.get("quote"), idx, sample_ids)
    back_q = {}
    for n, (key, fr) in enumerate(finds.items()):
        if fr["status"] in ("none", "not_found", "loose_only"):
            continue
        lb, which = key
        c = cand_index[key]
        claim = (f"the study varied {lb} on purpose between its samples" if which == "_factor"
                 else f"{lb.replace('_', ' ')} is {c.get('text')} for the samples this passage "
                      "describes")
        back_q[f"b{n}"] = {"type": "noul", "instructions":
                           f"Passage from {fr.get('source') or fr.get('record')}, {fr['field']}: \"{fr['quote'][:600]}\"\n"
                           f"Claim: {claim}.\nDoes the passage say so? Yes if it states it or "
                           "clearly implies it; no if it is about something else."}
    # candidate same-material pairs: a control library and signals at the same factor levels
    picks1 = {g: {k: pick(per[(g, False)].get(k)) for k in q1} for g in sample_ids}
    picks2 = {g: {k: pick(per[(g, True)].get(k)) for k in q1} for g in sample_ids}
    ctl = [g for g in sample_ids if (picks1[g].get("lab_control_type") or (None,))[0]
           not in (None, "na", "unknown")]
    pairs = set()
    # A pull-down and its control differ in target and control type by design: match on the
    # other factors only.
    lvl_keys = [k for k in q1 if k.startswith("lvl") and lists["_factors_kept"][int(k[3:])]["label"]
                not in ("target", "control_type", "assay")]
    for c in ctl:
        for g in sample_ids:
            if g != c and g not in ctl and all(picks1[g][k][0] == picks1[c][k][0] for k in lvl_keys):
                pairs.add(tuple(sorted((c, g))))
    for grp in lists.get("shared_material") or []:
        gs = [x for x in grp.get("samples") or [] if x in sample_ids]
        for a in gs:
            for b in gs:
                if a < b:
                    pairs.add((a, b))
    pairs = sorted(pairs)[:200]
    titles = {s["id"]: (s["fields"].get("Sample_title") or [""])[0] for s in m["samples"]}
    for k, (a, b) in enumerate(pairs):
        back_q[f"pair{k}"] = {"type": "noul", "instructions":
                              f"Were libraries {a} (\"{titles[a]}\") and {b} (\"{titles[b]}\") made "
                              "from the same piece of material, such as one chromatin preparation "
                              "that gave a pull-down and its input? Answer no unless a text says or "
                              "clearly implies they share a preparation."}
    import hashlib
    bkey = hashlib.sha1(json.dumps(back_q, sort_keys=True).encode()).hexdigest()[:10]
    ba = calls.jev(f"back_{bkey}", "Checking quoted passages of one study's records and paper against "
                   "claims about its samples.\n\n" + study_desc(m)[:40000], back_q) if back_q else {}
    # D. ontology picks, and the "none" probe
    taxid = None
    for s in m["samples"]:
        taxid = taxid or (s["fields"].get("Sample_taxid_ch1") or [None])[0]
    texts = defaultdict(set)
    for g in sample_ids:
        for k, (c, p) in picks1[g].items():
            if k.startswith("lab_") and c and c.startswith("v"):
                lb = k[4:]
                texts[lb].add(cand_index[(lb, c)]["text"])
            if k.startswith("lvl") and c and c.startswith("L"):
                f = lists["_factors_kept"][int(k[3:])]
                if f["label"] in O.VOCAB:
                    texts[f["label"]].add(cand_index[(f["label"], c)]["text"])
    onto_q, onto_meta = {}, {}
    wanted = [(lb, t) for lb in sorted(texts) for t in sorted(texts[lb])]
    with ThreadPoolExecutor(3) as ox:  # the limiter in ontology.py keeps it at 3 per second
        looked = dict(zip(wanted, ox.map(lambda x: O.candidates(x[0], x[1], taxid), wanted)))
    for lb in sorted(texts):
        for t in sorted(texts[lb]):
            cands, voc = looked[(lb, t)]
            if not voc:
                continue
            k = f"t{len(onto_meta)}"
            onto_meta[k] = {"label": lb, "text": t, "vocab": voc, "candidates": cands}
            if not cands:
                continue
            crit = {c["id"]: c["label"] or c["id"] for c in cands}
            crit["none"] = "none of these terms names it"
            onto_q[k] = {"type": "choice", "instructions":
                         f"Which ontology term names the {lb.replace('_', ' ')} \"{t}\"?",
                         "criteria": crit}
    oa = calls.jev("onto_0", f"Ontology terms for the samples of a study. {glossary(lists)[:6000]}",
                   onto_q) if onto_q else {}
    probe_q = {}
    for k, q in onto_q.items():
        c, p = pick(oa.get(k))
        onto_meta[k]["pick"], onto_meta[k]["p"] = c, p
        if c and c != "none" and p is not None and p >= 0.5:
            lab = q["criteria"][c].lower()
            crit = {i: lab2 for i, lab2 in q["criteria"].items()
                    if i != c and i != "none" and lab2.lower() != lab}
            if crit:
                crit["none"] = "none of these terms names it"
                probe_q[k] = {"type": "choice", "instructions": q["instructions"], "criteria": crit}
    pa = calls.jev("ontoprobe_0", f"Ontology terms for the samples of a study. "
                   f"{glossary(lists)[:6000]}", probe_q) if probe_q else {}
    for k in probe_q:
        c, p = pick(pa.get(k))
        onto_meta[k]["probe_pick"], onto_meta[k]["probe_p"] = c, p
    term_of = {}
    for k, om in onto_meta.items():
        if om.get("pick") and om["pick"] != "none" and (om.get("p") or 0) >= 0.8:
            term_of[(om["label"], om["text"])] = om["pick"]
    # E. assemble
    reading, info = assemble(gid, chain, read_full, m, lists, sa, picks1, picks2, cand_index,
                             finds, ba, back_q, pairs, term_of)
    text = yaml.safe_dump(reading, sort_keys=False, allow_unicode=True, width=100)
    data, load_problems = load_yaml(text)
    problems = load_problems or check_reading(data)
    res.update(info)
    res.update({
        "lists_summary": {"factors": len(lists["factors"]), "factors_kept": len(lists["_factors_kept"]),
                          "labels_with_values": sum(1 for d in lists["labels"].values() if d["values"]),
                          "extra_labels": len(lists["extra_labels"]),
                          "writer_gaps": lists.get("format_gaps"),
                          "donor_kind_writer": (lists.get("donors") or {}).get("kind"),
                          "keymap": lists.get("keymap")},
        "quotes": [{"key": f"{k[0]}/{k[1]}", **{x: v for x, v in fr.items() if x != "quote"},
                    "quote_len": len(fr.get("quote") or ""),
                    "backs_p": (ba.get(f"b{n}") or {}).get("noul")}
                   for n, (k, fr) in enumerate(finds.items())],
        "pairs": [{"a": a, "b": b, "p": (ba.get(f"pair{k}") or {}).get("noul")}
                  for k, (a, b) in enumerate(pairs)],
        "ontology": list(onto_meta.values()),
        "gap_probes": {k: (sa.get(f"gap_probe_{k}") or {}).get("noul") for k in GAP_PROBES},
        "donor_kind": pick(sa.get("donor_kind")),
        "problems": problems,
        "ledger": calls.ledger,
        "seconds": round(time.time() - t0, 2),
    })
    res["ending"] = "refused" if problems else ("unsettled" if info["unsettled_reasons"] else "settled")
    return res, (None if problems else text)


def assemble(gid, chain, read_full, m, lists, sa, picks1, picks2, cand_index, finds, ba, back_q,
             pairs, term_of):
    sample_ids = [s["id"] for s in m["samples"]]
    passages, pnum = {}, {}
    rec_idx = M.source_index(m, False)

    def passage_for(key):
        fr = finds.get(key)
        if not fr or fr["status"] in ("none", "not_found", "loose_only"):
            return None
        src = fr.get("record") or fr.get("source")
        field = fr["field"]
        if src == "each_sample":
            entry = {"record": "each_sample", "field": field, "quote": fr["quote"]}
        elif str(src).startswith("pmid:"):
            entry = {"paper": src, "section": field, "quote": " ".join(fr["quote"].split())}
        else:
            holders = [g for g in sample_ids
                       if any(fr["quote"] in v for v in rec_idx.get((g, field), []))]
            if src in sample_ids and len(holders) == len(sample_ids) and len(holders) > 1:
                entry = {"record": "each_sample", "field": field, "quote": fr["quote"]}
            elif src in sample_ids and len(holders) > 1:
                entry = {"record": holders, "field": field, "quote": fr["quote"]}
            else:
                entry = {"record": src, "field": field, "quote": fr["quote"]}
        sig = json.dumps(entry, sort_keys=True)
        if sig not in pnum:
            pnum[sig] = len(pnum) + 1
            passages[pnum[sig]] = entry
        return pnum[sig]

    def backing(key, how):
        n = passage_for(key)
        if n is None:
            return {"inferred": how or "picked by Jev from the values listed for this study"}
        idx = list(finds).index(key)
        bp = (ba.get(f"b{idx}") or {}).get("noul")
        if bp is not None and bp >= 0.5:
            return {"quoted": [n]}
        return {"inferred": how or "picked by Jev from the values listed for this study", "see": [n]}

    def value_claim(lb, key, c, p, level=None):
        cl = {"text": str(c.get("text"))}
        if lb in NUMERIC and c.get("value") is not None:
            v = c["value"]
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                cl["value"] = v
            elif isinstance(v, list) and len(v) == 2 and all(isinstance(x, (int, float)) for x in v):
                cl["value"] = v
            if c.get("unit") and "value" in cl:
                cl["unit"] = str(c["unit"])
        t = term_of.get((lb, cl["text"]))
        if lb == "species":
            tx = None
            for s in m["samples"]:
                tx = tx or (s["fields"].get("Sample_taxid_ch1") or [None])[0]
            if tx:
                t = f"NCBITaxon:{tx}"
        if lb == "sex" and cl["text"].strip().lower() in ("female", "male", "hermaphrodite", "mixed"):
            t = cl["text"].strip().lower()
        if t:
            cl["term"] = t
        if level is not None:
            cl["level"] = level
        cl.update(backing(key, c.get("how")))
        cl["p"] = None if p is None else round(float(p), 3)
        return cl

    rows = {g: {} for g in sample_ids}
    disagree = []  # (sample, question) where the reversed ask picked otherwise
    for g in sample_ids:
        for k, (c, p) in picks1[g].items():
            c2 = picks2[g].get(k, (None, None))[0]
            if c2 != c:
                disagree.append((g, k, c, c2))
            if k.startswith("lvl"):
                f = lists["_factors_kept"][int(k[3:])]
                lb = f["label"]
                if c and c.startswith("L"):
                    lv = f["levels"][int(c[1:])]
                    rows[g][lb] = value_claim(lb, (lb, c), lv, p, level=lv["name"])
                elif c == "na":
                    rows[g][lb] = {"na": f"{lb.replace('_', ' ')} does not apply to this sample",
                                   "p": None if p is None else round(p, 3)}
                else:
                    rows[g][lb] = {"unknown": f"the record of this sample does not say its "
                                              f"{lb.replace('_', ' ')}", "p": None if p is None else round(p, 3)}
            elif k.startswith("lab_"):
                lb = k[4:]
                if c and c.startswith("v"):
                    rows[g][lb] = value_claim(lb, (lb, c), cand_index[(lb, c)], p)
                elif c == "na":
                    ex = NA_EXAMPLE.get(lb) or "it does not apply to this sample"
                    rows[g][lb] = {"na": ex, "p": None if p is None else round(p, 3)}
                else:
                    d = {**lists["labels"], **lists["extra_labels"]}.get(lb, {})
                    why = ((d.get("gap") or {}).get("why") if isinstance(d.get("gap"), dict) else None)
                    rows[g][lb] = {"unknown": why or f"what was read does not say its "
                                                     f"{lb.replace('_', ' ')}",
                                   "p": None if p is None else round(p, 3)}
    # Labels with no listed value: Jev's study-level na / unknown.
    for lb, d in {**lists["labels"], **lists["extra_labels"]}.items():
        if lb in rows[sample_ids[0]] or any(f["label"] == lb for f in lists["_factors_kept"]):
            continue
        c, p = pick(sa.get(f"gap_{lb}"))
        why = (d.get("gap") or {}).get("why") if isinstance(d.get("gap"), dict) else None
        if lb == "species":
            c = "species_missing"
        for g in sample_ids:
            if c == "na":
                rows[g][lb] = {"na": why or NA_EXAMPLE.get(lb) or "does not apply to these samples",
                               "p": None if p is None else round(p, 3)}
            else:
                rows[g][lb] = {"unknown": why or "nothing read says it",
                               "p": None if p is None else round(p, 3)}
    # Donor and biosample.
    dk, dp = pick(sa.get("donor_kind"))
    parent = {g: g for g in sample_ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    pair_p = {}
    for k, (a, b) in enumerate(pairs):
        p = (ba.get(f"pair{k}") or {}).get("noul")
        pair_p[(a, b)] = p
        if p is not None and p >= 0.5:
            parent[find(a)] = find(b)
    groups = defaultdict(list)
    for g in sample_ids:
        groups[find(g)].append(g)
    bid = {}
    for n, (root, gs) in enumerate(sorted(groups.items(), key=lambda x: sample_ids.index(x[1][0]))):
        for g in gs:
            bid[g] = (f"B{n + 1}", gs)
    ids_list = [str(x.get("id")) for x in (lists.get("donors") or {}).get("ids") or [] if isinstance(x, dict)]
    for n, g in enumerate(sample_ids):
        b, gs = bid[g]
        if len(gs) > 1:
            ps = [pair_p.get(tuple(sorted((g, o)))) for o in gs if o != g]
            ps = [x for x in ps if x is not None]
            rows[g]["biosample"] = {"id": b, "inferred": "Jev judged these libraries made from one "
                                    "preparation", "p": round(max(ps), 3) if ps else None}
        else:
            rows[g]["biosample"] = {"id": b, "inferred": "no text says this library shares its "
                                    "material with another", "p": None}
        dpick = picks1[g].get("donor", (None, None))
        if dpick[0] and dpick[0].startswith("d"):
            rows[g]["donor"] = {"id": f"D_{ids_list[int(dpick[0][1:])]}"[:40],
                                "inferred": "Jev picked the individual from the listed names",
                                "p": None if dpick[1] is None else round(dpick[1], 3)}
        elif dk == "one_each":
            rows[g]["donor"] = {"id": f"D{n + 1}", "inferred": "each sample is from its own "
                                "individual", "p": None if dp is None else round(dp, 3)}
        elif dk in ("pool", "cell_line"):
            rows[g]["donor"] = {"na": "a pool of many individuals" if dk == "pool" else
                                "a cell line or culture, no single individual",
                                "p": None if dp is None else round(dp, 3)}
        else:
            rows[g]["donor"] = {"unknown": "nothing read says which individual a library came from",
                                "p": None if dp is None else round(dp, 3)}
    # Shared: a label whose claim is the same on every sample (p aside) goes under shared, with the
    # smallest p of the samples.
    shared = {}
    all_labels = list(dict.fromkeys(lb for g in sample_ids for lb in rows[g]))
    for lb in all_labels:
        if lb == "biosample":
            continue
        cs = [rows[g].get(lb) for g in sample_ids]
        if any(c is None for c in cs):
            continue
        strip = [{k: v for k, v in c.items() if k != "p"} for c in cs]
        if all(s == strip[0] for s in strip) and len(sample_ids) > 1:
            ps = [c.get("p") for c in cs if c.get("p") is not None]
            shared[lb] = {**strip[0], "p": min(ps) if ps else None}
            for g in sample_ids:
                rows[g].pop(lb)
    factors = {}
    for f in lists["_factors_kept"]:
        st = res_f = None
        i = f["_i"]
        p_d = (sa.get(f"f{i}_designed") or {}).get("noul")
        b, _ = pick(sa.get(f"f{i}_baseline"))
        names = [lv["name"] for lv in f["levels"]]
        base = names[int(b[1:])] if b and b.startswith("L") else None
        p_o = (sa.get(f"f{i}_ordered") or {}).get("noul")
        fc = {"levels": names, "baseline": base, "ordered": bool(p_o is not None and p_o >= 0.5)}
        fc.update(backing((f["label"], "_factor"), f.get("how") or "the levels are listed for this study"))
        fc["p"] = None if p_d is None else round(p_d, 3)
        factors[f["label"]] = fc
        del st, res_f
    read = ["records", "abstract"] + (["full_text"] if read_full else [])
    if chain == "code":
        models = {"reading": "typesafe/jev-1.14.0", "probabilities": "typesafe/jev-1.14.0"}
        detail = ("Code drafted the lists from GEO's typed sample fields; Jev answered every "
                  "closed question.")
    else:
        models = {"reading": f"deepseek/{WRITER}{' (thinking)' if chain == 'flash_think' else ''}",
                  "probabilities": "typesafe/jev-1.14.0"}
        detail = ("DeepSeek Flash wrote the lists from the records and the "
                  + ("full text" if read_full else "abstracts") + "; Jev answered every closed question.")
    reading = {"format": 1, "study": gid, "models": models, "read": read, "read_detail": detail,
               "passages": passages, "factors": factors, "shared": shared,
               "samples": {g: rows[g] for g in sample_ids}}
    # Ending: unsettled when the two asks disagree on a factor (designed, baseline) or on any
    # sample's level of a kept factor.
    reasons = []
    for f in lists["_factors_kept"]:
        i = f["_i"]
        p_d = (sa.get(f"f{i}_designed") or {}).get("noul")
        p_n = (sa.get(f"f{i}_designed_neg") or {}).get("noul")
        if p_d is not None and p_n is not None and (p_d >= 0.5) == (p_n >= 0.5):
            reasons.append(f"factor {f['label']}: plain and negated asks disagree")
        b1, _ = pick(sa.get(f"f{i}_baseline"))
        b2, _ = pick(sa.get(f"f{i}_baseline_rev"))
        if b1 != b2:
            reasons.append(f"factor {f['label']}: baseline differs when options are reversed")
    lvl_dis = [d for d in disagree if d[1].startswith("lvl")]
    if lvl_dis:
        reasons.append(f"{len(lvl_dis)} sample levels differ when options are reversed")
    # every claim's p, for the spread
    ps = []
    for where, claims in [("shared", shared.items())] + [(g, rows[g].items()) for g in sample_ids]:
        for lb, c in claims:
            mark = next((k for k in ("quoted", "inferred", "unknown", "na") if k in c), None)
            ps.append({"label": lb, "mark": mark, "p": c.get("p"), "shared": where == "shared"})
    for lb, fc in factors.items():
        ps.append({"label": f"factor:{lb}", "mark": "quoted" if "quoted" in fc else "inferred",
                   "p": fc["p"], "shared": True})
    per_sample_vals = {g: {lb: (c.get("text") or c.get("id") or ("<na>" if "na" in c else "<unknown>"),
                                c.get("level"))
                           for lb, c in {**shared, **rows[g]}.items()} for g in sample_ids}
    info = {"unsettled_reasons": reasons, "disagree": disagree, "claims": ps,
            "values": per_sample_vals, "factor_out": {k: {x: v for x, v in fc.items()}
                                                      for k, fc in factors.items()},
            "n_passages": len(passages), "n_shared": len(shared),
            "n_row_claims": sum(len(r) for r in rows.values())}
    return reading, info
